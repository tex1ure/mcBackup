"""Cross-platform startup task and background watcher process management."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from config import APP_DIR, DATA_DIR

TASK_NAME = "MCGDriveAutoBackup"
MUTEX_NAME = "Local\\MCGDriveBackupWatcherMutex"
PID_FILE = DATA_DIR / "watcher.pid"
CREATE_NO_WINDOW = 0x08000000


def _win_flags() -> int:
    return CREATE_NO_WINDOW if sys.platform == "win32" else 0


def get_pythonw_exe() -> str:
    exe = Path(sys.executable)
    if sys.platform == "win32":
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
    return str(exe)


def is_watcher_running() -> bool:
    """Check whether the background watcher is currently active."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False

    if not PID_FILE.is_file():
        return False
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True
    except (ValueError, OSError):
        return False


def start_watcher_background() -> tuple[bool, str]:
    """Launch watcher.py silently in the background."""
    if is_watcher_running():
        return True, "Watcher is already running."

    py_exe = get_pythonw_exe()
    watcher_script = str(APP_DIR / "watcher.py")
    try:
        kwargs: dict = {
            "cwd": str(APP_DIR),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([py_exe, watcher_script], **kwargs)
        return True, "Background watcher started."
    except OSError as exc:
        return False, f"Failed to start watcher: {exc}"


def stop_watcher_processes() -> int:
    """Stop any running watcher.py processes."""
    if sys.platform == "win32":
        ps = (
            "$procs = Get-CimInstance Win32_Process | Where-Object { "
            "$_.CommandLine -like '*mc-gdrive-backup*watcher.py*' -or "
            "$_.CommandLine -like '*mcBackup*watcher.py*' }; "
            "$count = ($procs | Measure-Object).Count; "
            "$procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; "
            "Write-Output $count"
        )
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            return int(res.stdout.strip() or "0")
        except Exception:
            return 0

    if not PID_FILE.is_file():
        return 0
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGTERM)
        PID_FILE.unlink(missing_ok=True)
        return 1
    except (ValueError, OSError):
        return 0


def _startup_vbs_path() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "MCGDriveAutoBackup.vbs"
    )


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.mcbackup.watcher.plist"


def _linux_service_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "mcbackup.service"


def is_startup_installed() -> bool:
    if sys.platform == "win32":
        if _startup_vbs_path().is_file():
            return True
        res = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME],
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        return res.returncode == 0
    if sys.platform == "darwin":
        return _macos_plist_path().is_file()
    return _linux_service_path().is_file()


def install_startup() -> tuple[bool, str]:
    """Register auto-start on login for Windows, macOS, or Linux."""
    py_exe = get_pythonw_exe()
    watcher_script = str(APP_DIR / "watcher.py")

    if sys.platform == "win32":
        task_cmd = f'"{py_exe}" "{watcher_script}"'
        res = subprocess.run(
            [
                "schtasks",
                "/Create",
                "/TN",
                TASK_NAME,
                "/TR",
                task_cmd,
                "/SC",
                "ONLOGON",
                "/RL",
                "LIMITED",
                "/F",
            ],
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode == 0:
            return True, f"Scheduled Task '{TASK_NAME}' registered for Windows logon."
        try:
            vbs = _startup_vbs_path()
            vbs.parent.mkdir(parents=True, exist_ok=True)
            vbs.write_text(
                'Set WshShell = CreateObject("WScript.Shell")\n'
                f'WshShell.Run """{py_exe}"" ""{watcher_script}""", 0, False\n',
                encoding="utf-8",
            )
            return True, f"Startup launcher created at {vbs}"
        except OSError as exc:
            err = (res.stderr or res.stdout).strip()
            return False, f"Could not enable auto-start: {err} / {exc}"

    if sys.platform == "darwin":
        plist = _macos_plist_path()
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            "  <key>Label</key><string>com.mcbackup.watcher</string>\n"
            f"  <key>ProgramArguments</key><array><string>{py_exe}</string><string>{watcher_script}</string></array>\n"
            "  <key>RunAtLoad</key><true/>\n"
            "  <key>KeepAlive</key><true/>\n"
            "</dict></plist>\n",
            encoding="utf-8",
        )
        subprocess.run(["launchctl", "load", str(plist)], capture_output=True)
        return True, f"macOS LaunchAgent installed at {plist}"

    # Linux systemd user service
    svc = _linux_service_path()
    svc.parent.mkdir(parents=True, exist_ok=True)
    svc.write_text(
        "[Unit]\n"
        "Description=mcBackup Minecraft Google Drive Auto-Backup Watcher\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f'ExecStart="{py_exe}" "{watcher_script}"\n'
        f"WorkingDirectory={APP_DIR}\n"
        "Restart=on-failure\n\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "mcbackup.service"], capture_output=True)
    return True, f"Linux systemd user service installed at {svc}"


def uninstall_startup() -> tuple[bool, str]:
    if sys.platform == "win32":
        subprocess.run(
            ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        vbs = _startup_vbs_path()
        if vbs.is_file():
            vbs.unlink(missing_ok=True)
        return True, "Auto-start on Windows logon removed."

    if sys.platform == "darwin":
        plist = _macos_plist_path()
        if plist.is_file():
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
            plist.unlink(missing_ok=True)
        return True, "macOS LaunchAgent removed."

    svc = _linux_service_path()
    if svc.is_file():
        subprocess.run(["systemctl", "--user", "disable", "--now", "mcbackup.service"], capture_output=True)
        svc.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return True, "Linux systemd user service removed."
