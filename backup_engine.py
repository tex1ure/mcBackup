"""Core backup engine: zip world, upload via rclone, prune retention, retry queue."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from config import (
    BackupConfig,
    LOGS_DIR,
    find_rclone,
    load_config,
    load_state,
    save_state,
)
from lock_detect import compute_world_fingerprint, is_world_locked

CREATE_NO_WINDOW = 0x08000000


def get_logger(name: str = "mc_backup") -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(LOGS_DIR / "backup.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def safe_world_slug(world_path: str | Path) -> str:
    name = Path(world_path).name
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" .")
    return cleaned or "MinecraftWorld"


def send_windows_notification(title: str, message: str) -> None:
    """Best-effort Windows toast notification without external packages."""
    safe_title = title.replace("'", "''")
    safe_msg = message.replace("'", "''")
    ps_script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$xml.LoadXml('<toast><visual><binding template=\"ToastText02\"><text id=\"1\">{safe_title}</text><text id=\"2\">{safe_msg}</text></binding></visual></toast>');"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);"
        "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe');"
        "$notifier.Show($toast);"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError:
        pass


def check_rclone_status(cfg: BackupConfig | None = None) -> tuple[bool, str]:
    """Return (ready, status_message) for rclone and the configured Google Drive remote."""
    cfg = cfg or load_config()
    rclone = find_rclone(cfg.rclone_path)
    if not rclone:
        return False, "rclone not installed"

    try:
        res = subprocess.run(
            [rclone, "listremotes"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"rclone error: {exc}"

    if res.returncode != 0:
        return False, (res.stderr or res.stdout).strip() or "rclone failed"

    remotes = [line.strip().rstrip(":") for line in res.stdout.splitlines() if line.strip()]
    if cfg.rclone_remote not in remotes:
        return False, f"Remote '{cfg.rclone_remote}' not configured yet"

    try:
        probe = subprocess.run(
            [rclone, "about", f"{cfg.rclone_remote}:", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
        if probe.returncode == 0:
            return True, f"Connected ({cfg.rclone_remote}:)"
        err = (probe.stderr or probe.stdout).strip()
        return False, f"Auth check failed: {err[:140]}"
    except subprocess.TimeoutExpired:
        return False, "Timed out checking Google Drive (offline?)"
    except OSError as exc:
        return False, f"rclone error: {exc}"


@dataclass
class BackupResult:
    world_path: str
    zip_path: str
    uploaded: bool
    message: str


def _create_world_zip(world_dir: Path, dest_zip: Path, logger: logging.Logger) -> None:
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = dest_zip.with_suffix(".zip.tmp")
    if tmp_zip.exists():
        tmp_zip.unlink()

    with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dirpath, _, filenames in os.walk(world_dir):
            for fname in filenames:
                if fname.endswith(".tmp"):
                    continue
                full = Path(dirpath) / fname
                rel = full.relative_to(world_dir)
                try:
                    zf.write(full, arcname=str(rel))
                except PermissionError:
                    if fname == "session.lock":
                        logger.debug("Skipped locked session.lock while zipping %s", world_dir)
                        continue
                    raise

    os.replace(tmp_zip, dest_zip)


def _upload_zip_to_drive(
    rclone: str,
    local_zip: Path,
    remote_name: str,
    remote_dir: str,
    logger: logging.Logger,
) -> tuple[bool, str]:
    remote_target = f"{remote_name}:{remote_dir.strip('/')}/{local_zip.parent.name}/{local_zip.name}"
    try:
        res = subprocess.run(
            [
                rclone,
                "copyto",
                str(local_zip),
                remote_target,
                "--retries",
                "3",
                "--low-level-retries",
                "10",
            ],
            capture_output=True,
            text=True,
            timeout=600,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode == 0:
            logger.info("Uploaded to Google Drive: %s", remote_target)
            return True, remote_target
        err = (res.stderr or res.stdout).strip() or f"exit code {res.returncode}"
        logger.warning("Upload failed for %s: %s", local_zip.name, err)
        return False, err
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Upload error for %s: %s", local_zip.name, exc)
        return False, str(exc)


def _prune_local_backups(
    world_backup_dir: Path,
    max_keep: int,
    pending_paths: set[str],
    logger: logging.Logger,
) -> None:
    if max_keep <= 0 or not world_backup_dir.is_dir():
        return
    zips = sorted(world_backup_dir.glob("*.zip"), key=lambda p: p.name)
    excess = len(zips) - max_keep
    if excess <= 0:
        return
    for old_zip in zips[:excess]:
        if str(old_zip.resolve()) in pending_paths:
            continue
        try:
            old_zip.unlink()
            logger.info("Pruned old local backup: %s", old_zip.name)
        except OSError as exc:
            logger.warning("Failed pruning local %s: %s", old_zip.name, exc)


def _prune_remote_backups(
    rclone: str,
    remote_name: str,
    remote_folder: str,
    world_slug: str,
    max_keep: int,
    logger: logging.Logger,
) -> None:
    if max_keep <= 0:
        return
    remote_dir = f"{remote_name}:{remote_folder.strip('/')}/{world_slug}"
    try:
        res = subprocess.run(
            [rclone, "lsf", "--files-only", remote_dir],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode != 0:
            return
        files = sorted(
            line.strip()
            for line in res.stdout.splitlines()
            if line.strip().lower().endswith(".zip")
        )
        excess = len(files) - max_keep
        if excess <= 0:
            return
        for old_name in files[:excess]:
            del_res = subprocess.run(
                [rclone, "deletefile", f"{remote_dir}/{old_name}"],
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=CREATE_NO_WINDOW,
            )
            if del_res.returncode == 0:
                logger.info("Pruned old Drive backup: %s/%s", world_slug, old_name)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Remote prune failed for %s: %s", world_slug, exc)


def flush_pending_uploads(cfg: BackupConfig | None = None, logger: logging.Logger | None = None) -> int:
    """Retry any local zip backups that haven't been uploaded to Google Drive yet."""
    cfg = cfg or load_config()
    logger = logger or get_logger()
    rclone = find_rclone(cfg.rclone_path)
    if not rclone:
        return 0

    state = load_state()
    pending: list[dict] = state.get("pending_uploads", [])
    if not pending:
        return 0

    remaining: list[dict] = []
    uploaded_count = 0
    for item in pending:
        local_zip = Path(item.get("zip_path", ""))
        if not local_zip.is_file():
            continue
        ok, _ = _upload_zip_to_drive(
            rclone,
            local_zip,
            cfg.rclone_remote,
            cfg.gdrive_folder,
            logger,
        )
        if ok:
            uploaded_count += 1
            slug = item.get("world_slug") or local_zip.parent.name
            _prune_remote_backups(
                rclone,
                cfg.rclone_remote,
                cfg.gdrive_folder,
                slug,
                cfg.max_remote_backups,
                logger,
            )
        else:
            remaining.append(item)

    state["pending_uploads"] = remaining
    save_state(state)
    return uploaded_count


def backup_world(
    world_path: str | Path,
    cfg: BackupConfig | None = None,
    *,
    reason: str = "manual",
    force: bool = False,
    skip_upload: bool = False,
    progress_cb: Callable[[str], None] | None = None,
) -> BackupResult:
    """Create a timestamped zip backup of a world, upload to Drive, and prune old copies."""
    cfg = cfg or load_config()
    logger = get_logger()
    wpath = Path(world_path).resolve()

    if not wpath.is_dir() or not (wpath / "level.dat").is_file():
        msg = f"Invalid Minecraft world folder: {wpath}"
        logger.error(msg)
        return BackupResult(str(wpath), "", False, msg)

    if is_world_locked(wpath):
        msg = f"World is currently open/locked by Minecraft: {wpath.name}"
        logger.warning(msg)
        return BackupResult(str(wpath), "", False, msg)

    slug = safe_world_slug(wpath)
    fingerprint = compute_world_fingerprint(wpath)
    state = load_state()
    world_key = str(wpath).lower()
    prev_fp = state.get("worlds", {}).get(world_key, {}).get("last_fingerprint", "")

    if not force and prev_fp and fingerprint == prev_fp:
        msg = f"No changes since last backup ({wpath.name})"
        logger.info(msg)
        return BackupResult(str(wpath), "", True, msg)

    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dest_dir = cfg.resolved_backups_dir() / slug
    zip_path = dest_dir / f"{slug}_{stamp}.zip"

    if progress_cb:
        progress_cb(f"Zipping {wpath.name}...")
    logger.info("Starting backup (%s) for %s -> %s", reason, wpath, zip_path.name)

    try:
        _create_world_zip(wpath, zip_path, logger)
    except Exception as exc:
        msg = f"Failed to create zip for {wpath.name}: {exc}"
        logger.exception(msg)
        return BackupResult(str(wpath), "", False, msg)

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    state.setdefault("worlds", {})[world_key] = {
        "last_fingerprint": fingerprint,
        "last_backup_at": datetime.now().isoformat(timespec="seconds"),
        "last_zip": str(zip_path),
        "last_reason": reason,
    }

    uploaded = False
    upload_msg = "saved locally (upload skipped)"
    rclone = find_rclone(cfg.rclone_path)

    if not skip_upload:
        if rclone:
            if progress_cb:
                progress_cb(f"Uploading {zip_path.name} ({size_mb:.1f} MB) to Google Drive...")
            uploaded, detail = _upload_zip_to_drive(
                rclone,
                zip_path,
                cfg.rclone_remote,
                cfg.gdrive_folder,
                logger,
            )
            if uploaded:
                upload_msg = f"uploaded to {detail}"
                _prune_remote_backups(
                    rclone,
                    cfg.rclone_remote,
                    cfg.gdrive_folder,
                    slug,
                    cfg.max_remote_backups,
                    logger,
                )
            else:
                upload_msg = f"saved locally; Drive upload queued ({detail})"
                state.setdefault("pending_uploads", []).append(
                    {"zip_path": str(zip_path), "world_slug": slug, "created_at": stamp}
                )
        else:
            upload_msg = "saved locally; rclone not found"
            state.setdefault("pending_uploads", []).append(
                {"zip_path": str(zip_path), "world_slug": slug, "created_at": stamp}
            )
    else:
        state.setdefault("pending_uploads", []).append(
            {"zip_path": str(zip_path), "world_slug": slug, "created_at": stamp}
        )

    save_state(state)

    pending_set = {
        str(Path(item["zip_path"]).resolve())
        for item in state.get("pending_uploads", [])
        if item.get("zip_path")
    }
    _prune_local_backups(dest_dir, cfg.max_local_backups, pending_set, logger)

    if not skip_upload and uploaded:
        flush_pending_uploads(cfg, logger)

    summary = f"Backed up {wpath.name} ({size_mb:.1f} MB) — {upload_msg}"
    logger.info(summary)
    if cfg.notify_on_backup:
        title = "Minecraft Backup Complete" if uploaded else "Minecraft Backup Saved Locally"
        send_windows_notification(title, f"{wpath.name} ({size_mb:.1f} MB) — {upload_msg}")

    return BackupResult(str(wpath), str(zip_path), uploaded, summary)
