"""Single-file installer for mcBackup (extracts app, installs rclone if missing, creates shortcuts, starts app)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

CREATE_NO_WINDOW = 0x08000000

PAYLOAD_FILES = [
    "app.py",
    "backup_engine.py",
    "config.py",
    "lock_detect.py",
    "startup.py",
    "watcher.py",
    "worlds.py",
    "Start Backup Manager.bat",
    "README.md",
]


def _bundle_root() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")) / "payload"
    return Path(__file__).resolve().parent


def _default_install_dir() -> Path:
    if Path("A:/").exists():
        return Path("A:/minecraft/mc-gdrive-backup")
    local = os.environ.get("LOCALAPPDATA", str(Path.home()))
    return Path(local) / "mcBackup"


def _find_rclone() -> str | None:
    on_path = shutil.which("rclone")
    if on_path:
        return on_path
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        pkgs = Path(local) / "Microsoft" / "WinGet" / "Packages"
        if pkgs.is_dir():
            for p in pkgs.glob("Rclone.Rclone*/**/rclone.exe"):
                return str(p)
    return None


def _find_real_python() -> tuple[str, str] | None:
    """Locate a real python.exe + pythonw.exe installation (ignoring Windows Store stub)."""
    candidates: list[Path] = []
    for cmd in ("python", "py"):
        found = shutil.which(cmd)
        if found:
            candidates.append(Path(found))

    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        for base in (
            Path(local) / "Programs" / "Python",
            Path(local) / "Python",
        ):
            if base.is_dir():
                candidates.extend(sorted(base.glob("Python*/python.exe"), reverse=True))
                candidates.extend(sorted(base.glob("pythoncore-*/python.exe"), reverse=True))

    for prog_env in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(prog_env, "")
        if base:
            candidates.extend(sorted(Path(base).glob("Python*/python.exe"), reverse=True))

    seen: set[str] = set()
    for exe in candidates:
        key = str(exe).lower()
        if key in seen or not exe.is_file():
            continue
        seen.add(key)
        try:
            probe = subprocess.run(
                [str(exe), "-c", "import tkinter; print('OK')"],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            if probe.returncode == 0 and "OK" in probe.stdout:
                pythonw = exe.with_name("pythonw.exe")
                return str(exe), str(pythonw if pythonw.is_file() else exe)
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _create_desktop_shortcut(install_dir: Path) -> None:
    bat = install_dir / "Start Backup Manager.bat"
    ico = install_dir / "assets" / "logo.ico"
    ps = (
        '$desktop = [Environment]::GetFolderPath("Desktop");'
        '$lnk = Join-Path $desktop "mcBackup.lnk";'
        '$wsh = New-Object -ComObject WScript.Shell;'
        '$sc = $wsh.CreateShortcut($lnk);'
        f'$sc.TargetPath = "{bat}";'
        f'$sc.WorkingDirectory = "{install_dir}";'
        f'$sc.IconLocation = "{ico}";'
        '$sc.Description = "mcBackup - Minecraft Google Drive Auto-Backup";'
        '$sc.Save()'
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )


class InstallerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("mcBackup Setup")
        self.root.geometry("480x240")
        self.root.resizable(False, False)

        ico = _bundle_root() / "assets" / "logo.ico"
        if ico.is_file():
            try:
                self.root.iconbitmap(str(ico))
            except tk.TclError:
                pass

        self.status_var = tk.StringVar(value="Ready to install mcBackup.")
        self.install_dir = _default_install_dir()

        ttk.Label(root, text="mcBackup Installer", font=("Segoe UI", 14, "bold")).pack(pady=(18, 6))
        ttk.Label(root, text=f"Install location: {self.install_dir}").pack(pady=4)
        self.progress = ttk.Progressbar(root, mode="indeterminate", length=380)
        self.progress.pack(pady=14)
        ttk.Label(root, textvariable=self.status_var).pack(pady=4)

        self.btn = ttk.Button(root, text="Install & Start mcBackup", command=self.start_install)
        self.btn.pack(pady=10)

    def set_status(self, msg: str) -> None:
        self.root.after(0, lambda: self.status_var.set(msg))

    def start_install(self) -> None:
        self.btn.configure(state="disabled")
        self.progress.start(12)
        threading.Thread(target=self._run_install, daemon=True).start()

    def _run_install(self) -> None:
        try:
            src_root = _bundle_root()
            self.install_dir.mkdir(parents=True, exist_ok=True)
            (self.install_dir / "assets").mkdir(parents=True, exist_ok=True)

            self.set_status("Copying mcBackup files...")
            for rel in PAYLOAD_FILES:
                src = src_root / rel
                if src.is_file():
                    shutil.copy2(src, self.install_dir / rel)
            for asset in ("logo.png", "logo.ico"):
                src = src_root / "assets" / asset
                if src.is_file():
                    shutil.copy2(src, self.install_dir / "assets" / asset)

            if not _find_rclone():
                self.set_status("Installing rclone via winget...")
                subprocess.run(
                    [
                        "winget",
                        "install",
                        "--id",
                        "Rclone.Rclone",
                        "--accept-package-agreements",
                        "--accept-source-agreements",
                        "--silent",
                    ],
                    capture_output=True,
                    creationflags=CREATE_NO_WINDOW,
                )

            py_pair = _find_real_python()
            if not py_pair:
                self.set_status("Installing Python 3 via winget (first-time setup)...")
                subprocess.run(
                    [
                        "winget",
                        "install",
                        "--id",
                        "Python.Python.3.12",
                        "--accept-package-agreements",
                        "--accept-source-agreements",
                        "--silent",
                    ],
                    capture_output=True,
                    creationflags=CREATE_NO_WINDOW,
                )
                py_pair = _find_real_python()

            if not py_pair:
                raise RuntimeError(
                    "Python 3 with Tkinter could not be found or installed automatically. "
                    "Please install Python from https://www.python.org/downloads/ and re-run setup."
                )

            python_exe, pythonw_exe = py_pair

            self.set_status("Creating Desktop shortcut & startup task...")
            _create_desktop_shortcut(self.install_dir)
            subprocess.run(
                [python_exe, str(self.install_dir / "app.py"), "--install-startup"],
                cwd=str(self.install_dir),
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
            )

            self.set_status("Starting mcBackup...")
            subprocess.Popen(
                [pythonw_exe, str(self.install_dir / "app.py")],
                cwd=str(self.install_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            )
            self.root.after(800, self.root.destroy)
        except Exception as exc:
            self.root.after(0, lambda: self._on_error(str(exc)))

    def _on_error(self, err: str) -> None:
        self.progress.stop()
        self.btn.configure(state="normal")
        self.status_var.set("Installation failed.")
        messagebox.showerror("mcBackup Setup Error", err)


if __name__ == "__main__":
    tk_root = tk.Tk()
    InstallerApp(tk_root)
    tk_root.mainloop()
