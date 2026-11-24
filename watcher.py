"""Background daemon watching selected Minecraft worlds for Save & Quit / Exit / Shutdown."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

from backup_engine import (
    backup_world,
    flush_pending_uploads,
    get_logger,
)
from config import CONFIG_PATH, load_config
from lock_detect import is_world_locked
from startup import MUTEX_NAME, PID_FILE

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    WM_QUERYENDSESSION = 0x0011
    WM_ENDSESSION = 0x0016
    WM_DESTROY = 0x0002

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    ERROR_ALREADY_EXISTS = 183


class WorldWatcher:
    def __init__(self) -> None:
        self.logger = get_logger("mc_watcher")
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._was_locked: dict[str, bool] = {}
        self._last_cfg_mtime = 0.0
        self.cfg = load_config()

    def _reload_config_if_changed(self) -> None:
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            return
        if mtime != self._last_cfg_mtime:
            self._last_cfg_mtime = mtime
            self.cfg = load_config()
            self.logger.info(
                "Loaded config: watching %d world(s)",
                len(self.cfg.selected_worlds),
            )

    def handle_shutdown_event(self) -> None:
        """Called when OS signals logoff/shutdown: create local backups immediately."""
        self.logger.info("OS shutdown/logoff signal received — checking worlds.")
        with self._lock:
            cfg = load_config()
            for raw in cfg.selected_worlds:
                wp = Path(raw)
                if not wp.is_dir():
                    continue
                if not is_world_locked(wp):
                    backup_world(wp, cfg, reason="os_shutdown", skip_upload=True)
        self.stop_event.set()

    def run(self) -> None:
        self.logger.info("mcBackup Auto-Backup Watcher started.")
        self._reload_config_if_changed()

        try:
            flushed = flush_pending_uploads(self.cfg, self.logger)
            if flushed:
                self.logger.info("Uploaded %d pending backup(s) from queue.", flushed)
        except Exception:
            self.logger.exception("Error flushing pending uploads on startup")

        if self.cfg.backup_on_startup_if_changed:
            for raw in self.cfg.selected_worlds:
                wp = Path(raw)
                if wp.is_dir() and not is_world_locked(wp):
                    backup_world(wp, self.cfg, reason="startup_catchup")

        for raw in self.cfg.selected_worlds:
            self._was_locked[raw] = is_world_locked(raw)

        last_flush_check = time.monotonic()

        while not self.stop_event.is_set():
            self._reload_config_if_changed()
            poll = max(0.5, float(self.cfg.poll_interval_sec))

            for raw in list(self.cfg.selected_worlds):
                wp = Path(raw)
                if not wp.is_dir():
                    continue

                locked_now = is_world_locked(wp)
                locked_prev = self._was_locked.get(raw, False)

                if locked_now and not locked_prev:
                    self.logger.info("World opened in Minecraft: %s", wp.name)
                elif not locked_now and locked_prev:
                    self.logger.info(
                        "Save & Quit / Exit detected for %s — waiting %.1fs to settle...",
                        wp.name,
                        self.cfg.settle_delay_sec,
                    )
                    time.sleep(max(0.5, float(self.cfg.settle_delay_sec)))
                    if is_world_locked(wp):
                        self.logger.info("World %s was re-opened immediately; deferring backup.", wp.name)
                    else:
                        with self._lock:
                            backup_world(
                                wp,
                                self.cfg,
                                reason="save_and_quit",
                                force=True,
                            )

                self._was_locked[raw] = locked_now

            now = time.monotonic()
            if now - last_flush_check >= 120.0:
                last_flush_check = now
                try:
                    flush_pending_uploads(self.cfg, self.logger)
                except Exception:
                    self.logger.exception("Periodic upload flush error")

            self.stop_event.wait(poll)

        self.logger.info("Watcher stopped.")


def _run_win32_shutdown_listener(watcher: WorldWatcher) -> None:
    """Create a hidden Win32 window so WM_QUERYENDSESSION triggers a fast backup on shutdown."""
    try:
        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_longlong,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_longlong

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_QUERYENDSESSION:
                user32.ShutdownBlockReasonCreate(hwnd, "Finishing Minecraft world backup...")
                try:
                    watcher.handle_shutdown_event()
                finally:
                    user32.ShutdownBlockReasonDestroy(hwnd)
                return 1
            if msg == WM_ENDSESSION and wparam:
                watcher.stop_event.set()
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        proc_ref = WNDPROC(wnd_proc)
        hinst = kernel32.GetModuleHandleW(None)
        class_name = "MCGDriveBackupHiddenWindow"

        wc = WNDCLASSW()
        wc.lpfnWndProc = proc_ref
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        user32.RegisterClassW(ctypes.byref(wc))

        hwnd = user32.CreateWindowExW(
            0, class_name, "MCGDriveBackupListener", 0, 0, 0, 0, 0, None, None, hinst, None
        )
        msg = wintypes.MSG()
        while not watcher.stop_event.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), hwnd, 0, 0)
            if ret <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except Exception:
        pass


def main() -> int:
    mutex = None
    if sys.platform == "win32":
        mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            if mutex:
                kernel32.CloseHandle(mutex)
            return 0
    else:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        if PID_FILE.is_file():
            try:
                old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
                os.kill(old_pid, 0)
                return 0
            except (ValueError, OSError):
                pass
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    watcher = WorldWatcher()

    if sys.platform == "win32":
        listener_thread = threading.Thread(
            target=_run_win32_shutdown_listener,
            args=(watcher,),
            daemon=True,
        )
        listener_thread.start()
    else:
        def _on_posix_signal(_signum, _frame):
            watcher.handle_shutdown_event()

        signal.signal(signal.SIGTERM, _on_posix_signal)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, _on_posix_signal)

    try:
        watcher.run()
    except KeyboardInterrupt:
        watcher.stop_event.set()
    finally:
        if mutex:
            kernel32.CloseHandle(mutex)
        elif sys.platform != "win32":
            PID_FILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
