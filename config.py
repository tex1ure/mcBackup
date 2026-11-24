"""Configuration and state persistence for Minecraft Google Drive Auto-Backup."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
LOGS_DIR = DATA_DIR / "logs"
DEFAULT_BACKUPS_DIR = DATA_DIR / "backups"


@dataclass
class BackupConfig:
    selected_worlds: list[str] = field(default_factory=list)
    custom_search_roots: list[str] = field(default_factory=list)
    rclone_remote: str = "gdrive"
    gdrive_folder: str = "MinecraftBackups"
    backups_dir: str = str(DEFAULT_BACKUPS_DIR)
    max_local_backups: int = 15
    max_remote_backups: int = 15
    poll_interval_sec: float = 1.5
    settle_delay_sec: float = 2.0
    backup_on_startup_if_changed: bool = True
    notify_on_backup: bool = True
    rclone_path: str = ""

    def resolved_backups_dir(self) -> Path:
        return Path(self.backups_dir).expanduser().resolve()


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)


def find_rclone(configured_path: str = "") -> str | None:
    """Locate rclone.exe from config, PATH, or WinGet packages directory."""
    if configured_path and Path(configured_path).is_file():
        return configured_path

    on_path = shutil.which("rclone")
    if on_path:
        return on_path

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = []
    if local_app_data:
        winget_pkgs = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        if winget_pkgs.is_dir():
            candidates.extend(winget_pkgs.glob("Rclone.Rclone*/**/rclone.exe"))
        winget_links = Path(local_app_data) / "Microsoft" / "WinGet" / "Links" / "rclone.exe"
        candidates.append(winget_links)

    for prog_env in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(prog_env, "")
        if base:
            candidates.append(Path(base) / "rclone" / "rclone.exe")

    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


def load_config() -> BackupConfig:
    ensure_data_dirs()
    if not CONFIG_PATH.is_file():
        cfg = BackupConfig()
        rclone = find_rclone()
        if rclone:
            cfg.rclone_path = rclone
        save_config(cfg)
        return cfg

    try:
        raw: dict[str, Any] = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return BackupConfig()

    defaults = asdict(BackupConfig())
    merged = {k: raw.get(k, v) for k, v in defaults.items()}
    cfg = BackupConfig(**merged)
    if not cfg.rclone_path or not Path(cfg.rclone_path).is_file():
        found = find_rclone()
        if found:
            cfg.rclone_path = found
    return cfg


def save_config(cfg: BackupConfig) -> None:
    ensure_data_dirs()
    tmp_path = CONFIG_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
    os.replace(tmp_path, CONFIG_PATH)


def load_state() -> dict[str, Any]:
    ensure_data_dirs()
    if not STATE_PATH.is_file():
        return {"worlds": {}, "pending_uploads": []}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"worlds": {}, "pending_uploads": []}
        data.setdefault("worlds", {})
        data.setdefault("pending_uploads", [])
        return data
    except (OSError, json.JSONDecodeError):
        return {"worlds": {}, "pending_uploads": []}


def save_state(state: dict[str, Any]) -> None:
    ensure_data_dirs()
    tmp_path = STATE_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp_path, STATE_PATH)
