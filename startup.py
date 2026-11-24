"""Windows startup task and background watcher process management."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

from config import APP_DIR

TASK_NAME = "MCGDriveAutoBackup"
MUTEX_NAME = "Local\\MCGDriveBackupWatcherMutex"
CREATE_NO_WINDOW = 0x08000000


def get_pythonw_exe() -> str:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if pythonw.is_file():
        return str(pythonw)
    return str(exe)


def is_watcher_running() -> bool:
    """Check whether the background watcher holds its single-instance Win32 mutex."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    SYNCHRONIZE = 0x00100000
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def start_watcher_background() -> tuple[bool, str]:
    """Launch watcher.py silently in the background via pythonw.exe."""
    if is_watcher_running():
        return True, "Watcher is already running."

    pythonw = get_pythonw_exe()
    watcher_script = str(APP_DIR / "watcher.py")
    try:
        subprocess.Popen(
            [pythonw, watcher_script],
            cwd=str(APP_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )
        return True, "Background watcher started."
    except OSError as exc:
        return False, f"Failed to start watcher: {exc}"


def stop_watcher_processes() -> int:
    """Stop any running watcher.py Python processes."""
    ps = (
        "$procs = Get-CimInstance Win32_Process | Where-Object { "
        "$_.CommandLine -like '*mc-gdrive-backup*watcher.py*' }; "
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


def is_startup_installed() -> bool:
    startup_vbs = _startup_vbs_path()
    if startup_vbs.is_file():
        return True
    res = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME],
        capture_output=True,
        text=True,
        creationflags=CREATE_NO_WINDOW,
    )
    return res.returncode == 0


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


def install_startup() -> tuple[bool, str]:
    """Register auto-start on Windows logon (Task Scheduler with Startup VBS fallback)."""
    pythonw = get_pythonw_exe()
    watcher_script = str(APP_DIR / "watcher.py")
    task_cmd = f'"{pythonw}" "{watcher_script}"'

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

    # Fallback: hidden VBScript launcher in User Startup folder
    try:
        vbs = _startup_vbs_path()
        vbs.parent.mkdir(parents=True, exist_ok=True)
        vbs.write_text(
            'Set WshShell = CreateObject("WScript.Shell")\n'
            f'WshShell.Run """{pythonw}"" ""{watcher_script}""", 0, False\n',
            encoding="utf-8",
        )
        return True, f"Startup launcher created at {vbs}"
    except OSError as exc:
        err = (res.stderr or res.stdout).strip()
        return False, f"Could not enable auto-start: {err} / {exc}"


def uninstall_startup() -> tuple[bool, str]:
    subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True,
        text=True,
        creationflags=CREATE_NO_WINDOW,
    )
    vbs = _startup_vbs_path()
    if vbs.is_file():
        try:
            vbs.unlink()
        except OSError:
            pass
    return True, "Auto-start on Windows logon removed."
