"""Automated tests for lock detection, world discovery, backup engine, and watcher transitions."""

from __future__ import annotations

import gzip
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config as cfg_mod  # noqa: E402
from backup_engine import backup_world  # noqa: E402
from config import BackupConfig, load_state  # noqa: E402
from lock_detect import compute_world_fingerprint, is_world_locked  # noqa: E402
from watcher import WorldWatcher  # noqa: E402
from worlds import inspect_world  # noqa: E402


def _make_nbt_level_dat(path: Path, level_name: str) -> None:
    """Write a minimal valid gzipped NBT level.dat with Data.LevelName."""
    name_bytes = level_name.encode("utf-8")
    payload = bytearray()
    payload.append(10)  # root Compound
    payload += struct.pack(">H", 0)  # empty root name
    payload.append(10)  # Data Compound
    payload += struct.pack(">H", 4) + b"Data"
    payload.append(8)  # String tag
    payload += struct.pack(">H", 9) + b"LevelName"
    payload += struct.pack(">H", len(name_bytes)) + name_bytes
    payload.append(0)  # End Data
    payload.append(0)  # End Root
    with gzip.open(path, "wb") as fh:
        fh.write(bytes(payload))


class TestMinecraftBackup(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mc_backup_test_"))
        self.world_dir = self.tmp / "saves" / "MyTestWorld"
        self.world_dir.mkdir(parents=True)
        _make_nbt_level_dat(self.world_dir / "level.dat", "Survival Base")
        (self.world_dir / "region").mkdir()
        (self.world_dir / "region" / "r.0.0.mca").write_bytes(b"chunkdata" * 100)
        (self.world_dir / "session.lock").write_bytes("☃".encode("utf-8"))

        self._orig_data = cfg_mod.DATA_DIR
        self._orig_cfg = cfg_mod.CONFIG_PATH
        self._orig_state = cfg_mod.STATE_PATH
        self._orig_logs = cfg_mod.LOGS_DIR

        cfg_mod.DATA_DIR = self.tmp / "data"
        cfg_mod.CONFIG_PATH = cfg_mod.DATA_DIR / "config.json"
        cfg_mod.STATE_PATH = cfg_mod.DATA_DIR / "state.json"
        cfg_mod.LOGS_DIR = cfg_mod.DATA_DIR / "logs"

        # Redirect backup_engine module references too
        import backup_engine

        backup_engine.LOGS_DIR = cfg_mod.LOGS_DIR

    def tearDown(self) -> None:
        cfg_mod.DATA_DIR = self._orig_data
        cfg_mod.CONFIG_PATH = self._orig_cfg
        cfg_mod.STATE_PATH = self._orig_state
        cfg_mod.LOGS_DIR = self._orig_logs
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_inspect_world_reads_nbt_level_name(self) -> None:
        info = inspect_world(self.world_dir, source_label="Test")
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.level_name, "Survival Base")
        self.assertEqual(info.folder_name, "MyTestWorld")
        self.assertFalse(info.is_locked)

    def test_lock_detection_across_processes(self) -> None:
        self.assertFalse(is_world_locked(self.world_dir))
        lock_file = self.world_dir / "session.lock"

        holder_code = (
            "import msvcrt, sys, time; "
            "f = open(sys.argv[1], 'r+b'); "
            "msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1); "
            "print('LOCKED', flush=True); "
            "time.sleep(10)"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", holder_code, str(lock_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            line = proc.stdout.readline().strip() if proc.stdout else ""
            self.assertEqual(line, "LOCKED")
            self.assertTrue(is_world_locked(self.world_dir))
        finally:
            proc.terminate()
            proc.wait(timeout=5)

        self.assertFalse(is_world_locked(self.world_dir))

    def test_backup_world_and_retention(self) -> None:
        cfg = BackupConfig(
            selected_worlds=[str(self.world_dir)],
            backups_dir=str(self.tmp / "backups"),
            max_local_backups=2,
            max_remote_backups=2,
            notify_on_backup=False,
        )
        cfg_mod.save_config(cfg)

        res1 = backup_world(self.world_dir, cfg, reason="test1", skip_upload=True)
        self.assertTrue(Path(res1.zip_path).is_file())

        # Unchanged world should skip when force=False
        res2 = backup_world(self.world_dir, cfg, reason="test2", force=False, skip_upload=True)
        self.assertEqual(res2.zip_path, "")

        # Modify region data -> fingerprint changes -> new backup created
        time.sleep(1.1)
        (self.world_dir / "region" / "r.0.0.mca").write_bytes(b"updatedchunkdata" * 100)
        fp1 = compute_world_fingerprint(self.world_dir)
        res3 = backup_world(self.world_dir, cfg, reason="test3", skip_upload=True)
        self.assertTrue(Path(res3.zip_path).is_file())
        self.assertEqual(
            load_state()["worlds"][str(self.world_dir.resolve()).lower()]["last_fingerprint"],
            fp1,
        )

    def test_watcher_triggers_backup_on_unlock(self) -> None:
        cfg = BackupConfig(
            selected_worlds=[str(self.world_dir)],
            backups_dir=str(self.tmp / "backups"),
            poll_interval_sec=0.3,
            settle_delay_sec=0.5,
            backup_on_startup_if_changed=False,
            notify_on_backup=False,
            rclone_path="__nonexistent__",
        )
        cfg_mod.save_config(cfg)

        lock_file = self.world_dir / "session.lock"
        holder_code = (
            "import msvcrt, sys, time; "
            "f = open(sys.argv[1], 'r+b'); "
            "msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1); "
            "print('LOCKED', flush=True); "
            "time.sleep(2.0)"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", holder_code, str(lock_file)],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(proc.stdout.readline().strip() if proc.stdout else "", "LOCKED")

        watcher = WorldWatcher()
        t = threading.Thread(target=watcher.run, daemon=True)
        t.start()

        # Wait until holder releases lock on exit
        proc.wait(timeout=10)

        # Wait for watcher to detect unlock + settle + create zip
        dest_dir = Path(cfg.backups_dir) / "MyTestWorld"
        deadline = time.monotonic() + 8.0
        zips: list[Path] = []
        while time.monotonic() < deadline:
            zips = list(dest_dir.glob("*.zip")) if dest_dir.is_dir() else []
            if zips:
                break
            time.sleep(0.2)

        watcher.stop_event.set()
        t.join(timeout=3)
        self.assertGreaterEqual(len(zips), 1)


if __name__ == "__main__":
    unittest.main()
