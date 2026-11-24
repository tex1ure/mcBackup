"""Interactive GUI and CLI manager for Minecraft Google Drive Auto-Backup."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from backup_engine import (
    backup_world,
    check_rclone_status,
    flush_pending_uploads,
    get_logger,
    list_remote_backups,
    list_remote_worlds,
    restore_world_from_drive,
    safe_world_slug,
)
from config import (
    APP_DIR,
    DEFAULT_BACKUPS_DIR,
    LOGS_DIR,
    BackupConfig,
    find_rclone,
    load_config,
    load_state,
    save_config,
)
from startup import (
    install_startup,
    is_startup_installed,
    is_watcher_running,
    start_watcher_background,
    stop_watcher_processes,
    uninstall_startup,
)
from worlds import DiscoveredWorld, discover_worlds, is_valid_world_dir


def connect_google_drive_interactive(cfg: BackupConfig) -> tuple[bool, str]:
    """Launch rclone OAuth setup so the user can sign in with Google in their browser."""
    rclone = find_rclone(cfg.rclone_path)
    if not rclone:
        return False, "rclone.exe not found. Please install rclone first."

    # `rclone config create <remote> drive` opens the local browser OAuth flow
    # automatically without needing a Google Cloud project.
    try:
        res = subprocess.run(
            [
                rclone,
                "config",
                "create",
                cfg.rclone_remote,
                "drive",
                "scope=drive",
                "config_is_local=true",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            **({"creationflags": 0x08000000} if sys.platform == "win32" else {}),
        )
        if res.returncode == 0:
            return check_rclone_status(cfg)
        err = (res.stderr or res.stdout).strip() or f"exit code {res.returncode}"
        return False, f"Google Drive sign-in failed: {err[:180]}"
    except subprocess.TimeoutExpired:
        return False, "Google Drive sign-in timed out (3 minutes). Please try again."
    except OSError as exc:
        return False, f"Failed to run rclone: {exc}"


class BackupManagerGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("mcBackup — Minecraft -> Google Drive Auto-Backup")
        self.root.geometry("940x680")
        self.root.minsize(820, 580)
        ico_path = APP_DIR / "assets" / "logo.ico"
        png_path = APP_DIR / "assets" / "logo.png"
        if sys.platform == "win32" and ico_path.is_file():
            try:
                self.root.iconbitmap(str(ico_path))
            except tk.TclError:
                pass
        elif png_path.is_file():
            try:
                self._icon_img = tk.PhotoImage(file=str(png_path))
                self.root.iconphoto(True, self._icon_img)
            except tk.TclError:
                pass

        self.cfg = load_config()
        self.worlds: list[DiscoveredWorld] = []
        self.world_vars: dict[str, tk.BooleanVar] = {}

        self.drive_status_var = tk.StringVar(value="Checking Google Drive...")
        self.watcher_status_var = tk.StringVar(value="Checking watcher...")
        self.startup_var = tk.BooleanVar(value=is_startup_installed())
        self.notify_var = tk.BooleanVar(value=self.cfg.notify_on_backup)
        self.remote_var = tk.StringVar(value=self.cfg.rclone_remote)
        self.folder_var = tk.StringVar(value=self.cfg.gdrive_folder)
        self.keep_var = tk.StringVar(value=str(self.cfg.max_remote_backups))
        self.action_status_var = tk.StringVar(value="Ready.")

        self._build_ui()
        self.refresh_worlds()
        self.refresh_status()
        self._schedule_log_poll()

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}

        # Top status bar
        status_frame = ttk.LabelFrame(self.root, text="System Status")
        status_frame.pack(fill="x", **pad)

        ttk.Label(status_frame, textvariable=self.drive_status_var, font=("Segoe UI", 10, "bold")).pack(
            side="left", padx=10, pady=8
        )
        ttk.Button(
            status_frame,
            text="Connect Google Drive",
            command=self.on_connect_gdrive,
        ).pack(side="left", padx=6, pady=6)

        ttk.Label(status_frame, textvariable=self.watcher_status_var, font=("Segoe UI", 10, "bold")).pack(
            side="right", padx=10, pady=8
        )

        # World selector
        worlds_frame = ttk.LabelFrame(
            self.root, text="Select Minecraft World(s) to Auto-Backup on Save & Quit / Exit / Shutdown"
        )
        worlds_frame.pack(fill="both", expand=True, **pad)

        btn_bar = ttk.Frame(worlds_frame)
        btn_bar.pack(fill="x", padx=8, pady=(6, 4))
        ttk.Button(btn_bar, text="Refresh Worlds", command=self.refresh_worlds).pack(side="left", padx=(0, 6))
        ttk.Button(btn_bar, text="Browse World Folder...", command=self.on_browse_world).pack(
            side="left", padx=6
        )
        ttk.Label(
            btn_bar,
            text="Click a row to toggle selection. Selected worlds back up automatically every time you Save & Quit.",
        ).pack(side="left", padx=12)

        cols = ("selected", "name", "source", "modified", "size", "state", "path")
        self.tree = ttk.Treeview(worlds_frame, columns=cols, show="headings", height=9)
        self.tree.heading("selected", text="Watch")
        self.tree.heading("name", text="World Name")
        self.tree.heading("source", text="Launcher / Source")
        self.tree.heading("modified", text="Last Played")
        self.tree.heading("size", text="Size")
        self.tree.heading("state", text="Status")
        self.tree.heading("path", text="Folder Path")

        self.tree.column("selected", width=65, anchor="center", stretch=False)
        self.tree.column("name", width=170)
        self.tree.column("source", width=140)
        self.tree.column("modified", width=125)
        self.tree.column("size", width=75, anchor="e", stretch=False)
        self.tree.column("state", width=85, anchor="center", stretch=False)
        self.tree.column("path", width=240)

        vsb = ttk.Scrollbar(worlds_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(0, 8))
        vsb.pack(side="right", fill="y", padx=(0, 8), pady=(0, 8))
        self.tree.bind("<ButtonRelease-1>", self.on_tree_click)

        # Settings
        cfg_frame = ttk.LabelFrame(self.root, text="Backup Settings")
        cfg_frame.pack(fill="x", **pad)

        row1 = ttk.Frame(cfg_frame)
        row1.pack(fill="x", padx=8, pady=6)
        ttk.Label(row1, text="rclone Remote:").pack(side="left")
        ttk.Entry(row1, textvariable=self.remote_var, width=12).pack(side="left", padx=(4, 14))
        ttk.Label(row1, text="Drive Folder:").pack(side="left")
        ttk.Entry(row1, textvariable=self.folder_var, width=22).pack(side="left", padx=(4, 14))
        ttk.Label(row1, text="Keep Latest N Backups:").pack(side="left")
        ttk.Entry(row1, textvariable=self.keep_var, width=6).pack(side="left", padx=(4, 14))
        ttk.Checkbutton(row1, text="Desktop Notifications", variable=self.notify_var).pack(side="left", padx=8)
        ttk.Checkbutton(
            row1,
            text="Start Watcher on Login",
            variable=self.startup_var,
            command=self.on_toggle_startup,
        ).pack(side="left", padx=8)

        # Action buttons
        act_frame = ttk.Frame(self.root)
        act_frame.pack(fill="x", **pad)

        ttk.Button(
            act_frame,
            text="Save & Start Background Watcher",
            command=self.on_save_and_start,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(act_frame, text="Backup Selected Now", command=self.on_backup_now).pack(side="left", padx=6)
        ttk.Button(act_frame, text="Restore from Drive...", command=self.on_open_restore).pack(side="left", padx=6)
        ttk.Button(act_frame, text="Stop Watcher", command=self.on_stop_watcher).pack(side="left", padx=6)
        ttk.Button(act_frame, text="Open Local Backups", command=self.on_open_backups).pack(
            side="right", padx=(6, 0)
        )

        ttk.Label(self.root, textvariable=self.action_status_var).pack(fill="x", padx=14, pady=(0, 4))

        # Live log viewer
        log_frame = ttk.LabelFrame(self.root, text="Recent Backup Log")
        log_frame.pack(fill="both", expand=False, padx=12, pady=(2, 10))
        self.log_text = tk.Text(log_frame, height=8, state="disabled", wrap="word", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

    def _selected_set(self) -> set[str]:
        return {str(Path(p).resolve()).lower() for p in self.cfg.selected_worlds}

    def refresh_worlds(self) -> None:
        self.cfg = load_config()
        self.worlds = discover_worlds(self.cfg.selected_worlds, self.cfg.custom_search_roots)
        selected_lower = self._selected_set()

        for item in self.tree.get_children():
            self.tree.delete(item)

        for w in self.worlds:
            is_sel = str(Path(w.path).resolve()).lower() in selected_lower
            mark = "[X]" if is_sel else "[ ]"
            state_txt = "IN GAME" if w.is_locked else "Ready"
            self.tree.insert(
                "",
                "end",
                iid=w.path,
                values=(
                    mark,
                    w.display_name,
                    w.source_label,
                    w.last_modified_str,
                    w.size_mb_str,
                    state_txt,
                    w.path,
                ),
            )

    def on_tree_click(self, event: tk.Event) -> None:
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        norm = str(Path(row_id).resolve()).lower()
        current = list(self.cfg.selected_worlds)
        existing_lower = {str(Path(p).resolve()).lower(): p for p in current}
        if norm in existing_lower:
            current = [p for p in current if str(Path(p).resolve()).lower() != norm]
        else:
            current.append(row_id)
        self.cfg.selected_worlds = current
        save_config(self.cfg)
        self.refresh_worlds()

    def on_browse_world(self) -> None:
        chosen = filedialog.askdirectory(title="Select a Minecraft World Folder (contains level.dat)")
        if not chosen:
            return
        p = Path(chosen)
        if not is_valid_world_dir(p):
            messagebox.showerror(
                "Invalid World Folder",
                f"'{chosen}' does not contain level.dat.\nPlease pick the world folder itself (inside 'saves').",
            )
            return
        resolved = str(p.resolve())
        norm = resolved.lower()
        if norm not in self._selected_set():
            self.cfg.selected_worlds.append(resolved)
        parent = str(p.resolve().parent)
        if parent not in self.cfg.custom_search_roots:
            self.cfg.custom_search_roots.append(parent)
        save_config(self.cfg)
        self.refresh_worlds()

    def _save_form_settings(self) -> bool:
        try:
            keep_n = max(1, int(self.keep_var.get().strip()))
        except ValueError:
            messagebox.showerror("Invalid Number", "Keep Latest N Backups must be a positive integer.")
            return False

        self.cfg.rclone_remote = self.remote_var.get().strip() or "gdrive"
        self.cfg.gdrive_folder = self.folder_var.get().strip() or "MinecraftBackups"
        self.cfg.max_local_backups = keep_n
        self.cfg.max_remote_backups = keep_n
        self.cfg.notify_on_backup = bool(self.notify_var.get())
        save_config(self.cfg)
        return True

    def on_save_and_start(self) -> None:
        if not self._save_form_settings():
            return
        if not self.cfg.selected_worlds:
            messagebox.showwarning("No World Selected", "Please select at least one Minecraft world first.")
            return
        if is_watcher_running():
            stop_watcher_processes()
        ok, msg = start_watcher_background()
        self.action_status_var.set(msg)
        self.refresh_status()

    def on_stop_watcher(self) -> None:
        count = stop_watcher_processes()
        self.action_status_var.set(f"Stopped {count} watcher process(es).")
        self.refresh_status()

    def on_toggle_startup(self) -> None:
        if self.startup_var.get():
            ok, msg = install_startup()
        else:
            ok, msg = uninstall_startup()
        self.action_status_var.set(msg)
        if not ok:
            self.startup_var.set(is_startup_installed())

    def on_connect_gdrive(self) -> None:
        if not self._save_form_settings():
            return
        rclone = find_rclone(self.cfg.rclone_path)
        if not rclone:
            hint = (
                "Install it with: winget install Rclone.Rclone"
                if sys.platform == "win32"
                else "Install it with: brew install rclone (macOS) or https://rclone.org/install/ (Linux)"
            )
            messagebox.showerror(
                "rclone Not Found",
                f"rclone was not found. {hint}",
            )
            return

        self.action_status_var.set("Opening browser for Google Drive sign-in...")

        def worker() -> None:
            ok, msg = connect_google_drive_interactive(self.cfg)
            self.root.after(0, lambda: self._after_connect(ok, msg))

        threading.Thread(target=worker, daemon=True).start()

    def _after_connect(self, ok: bool, msg: str) -> None:
        self.refresh_status()
        self.action_status_var.set(msg)
        if ok:
            messagebox.showinfo("Google Drive Connected", msg)
        else:
            messagebox.showwarning("Google Drive Setup", msg)

    def on_backup_now(self) -> None:
        if not self._save_form_settings():
            return
        if not self.cfg.selected_worlds:
            messagebox.showwarning("No World Selected", "Please select at least one world first.")
            return

        self.action_status_var.set("Starting manual backup...")

        def worker() -> None:
            results = []
            for wp in self.cfg.selected_worlds:
                res = backup_world(
                    wp,
                    self.cfg,
                    reason="manual",
                    force=True,
                    progress_cb=lambda m: self.root.after(0, lambda msg=m: self.action_status_var.set(msg)),
                )
                results.append(res.message)
            summary = " | ".join(results)
            self.root.after(0, lambda: self.action_status_var.set(summary))
            self.root.after(0, self.refresh_worlds)

        threading.Thread(target=worker, daemon=True).start()

    def on_open_restore(self) -> None:
        if not self._save_form_settings():
            return
        RestoreDialog(self.root, self.cfg, self.worlds, on_restored=self._after_restore)

    def _after_restore(self, msg: str) -> None:
        self.action_status_var.set(msg)
        self.refresh_worlds()

    def on_open_backups(self) -> None:
        bdir = self.cfg.resolved_backups_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(bdir))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(bdir)])
        else:
            subprocess.Popen(["xdg-open", str(bdir)])

    def refresh_status(self) -> None:
        def check() -> None:
            ok, msg = check_rclone_status(self.cfg)
            running = is_watcher_running()
            self.root.after(0, lambda: self._apply_status(ok, msg, running))

        threading.Thread(target=check, daemon=True).start()

    def _apply_status(self, drive_ok: bool, drive_msg: str, watcher_running: bool) -> None:
        prefix = "Google Drive: READY — " if drive_ok else "Google Drive: NOT READY — "
        self.drive_status_var.set(prefix + drive_msg)
        self.watcher_status_var.set(
            "Watcher: RUNNING" if watcher_running else "Watcher: STOPPED"
        )

    def _schedule_log_poll(self) -> None:
        log_file = LOGS_DIR / "backup.log"
        if log_file.is_file():
            try:
                lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
                content = "\n".join(lines)
                self.log_text.configure(state="normal")
                self.log_text.delete("1.0", "end")
                self.log_text.insert("1.0", content)
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
            except OSError:
                pass
        self.root.after(2000, self._schedule_log_poll)


class RestoreDialog(tk.Toplevel):
    """One-click Google Drive world restore dialog."""

    def __init__(
        self,
        parent: tk.Tk,
        cfg: BackupConfig,
        worlds: list[DiscoveredWorld],
        on_restored,
    ) -> None:
        super().__init__(parent)
        self.title("Restore World from Google Drive")
        self.geometry("660x470")
        self.minsize(560, 400)
        self.transient(parent)
        self.grab_set()

        self.cfg = cfg
        self.worlds = worlds
        self.on_restored = on_restored
        self.backups: list[str] = []

        self.world_slug_var = tk.StringVar()
        self.target_dir_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Loading worlds from Google Drive...")

        self._build_ui()
        self._load_remote_worlds()

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Drive World:").pack(side="left")
        self.world_combo = ttk.Combobox(top, textvariable=self.world_slug_var, state="readonly", width=32)
        self.world_combo.pack(side="left", padx=(6, 8), fill="x", expand=True)
        self.world_combo.bind("<<ComboboxSelected>>", lambda _e: self._load_backups_for_selected())
        self.refresh_btn = ttk.Button(top, text="Refresh", command=self._load_remote_worlds)
        self.refresh_btn.pack(side="left")

        list_frame = ttk.LabelFrame(self, text="Available Backups in Google Drive (Newest First)")
        list_frame.pack(fill="both", expand=True, **pad)
        self.listbox = tk.Listbox(list_frame, font=("Consolas", 10), activestyle="dotbox", exportselection=False)
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=vsb.set)
        self.listbox.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        vsb.pack(side="right", fill="y", padx=(0, 8), pady=8)

        dst_frame = ttk.LabelFrame(self, text="Restore Destination Folder")
        dst_frame.pack(fill="x", **pad)
        dst_row = ttk.Frame(dst_frame)
        dst_row.pack(fill="x", padx=8, pady=8)
        ttk.Entry(dst_row, textvariable=self.target_dir_var).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(dst_row, text="Browse...", command=self._browse_target).pack(side="left")

        ttk.Label(self, textvariable=self.status_var).pack(fill="x", padx=14, pady=(2, 4))

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", padx=12, pady=(4, 12))
        self.restore_btn = ttk.Button(
            btn_row,
            text="Restore Selected Backup",
            command=self._on_restore,
            state="disabled",
        )
        self.restore_btn.pack(side="left")
        ttk.Button(btn_row, text="Close", command=self.destroy).pack(side="right")

    def _guess_target_for_slug(self, slug: str) -> str:
        for wp in self.cfg.selected_worlds:
            if safe_world_slug(wp).lower() == slug.lower():
                return wp
        for w in self.worlds:
            if safe_world_slug(w.path).lower() == slug.lower():
                return w.path
        if self.worlds:
            return str(Path(self.worlds[0].path).parent / slug)
        return str(Path.home() / "mcBackup_restored" / slug)

    def _browse_target(self) -> None:
        chosen = filedialog.askdirectory(parent=self, title="Choose Destination World Folder")
        if chosen:
            self.target_dir_var.set(str(Path(chosen).resolve()))

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.refresh_btn.configure(state=state)
        self.world_combo.configure(state="disabled" if busy else "readonly")
        if busy or not self.backups:
            self.restore_btn.configure(state="disabled")
        else:
            self.restore_btn.configure(state="normal")

    def _load_remote_worlds(self) -> None:
        self._set_busy(True)
        self.status_var.set("Fetching world list from Google Drive...")

        def worker() -> None:
            ok, folders, err = list_remote_worlds(self.cfg)
            self.after(0, lambda: self._on_worlds_loaded(ok, folders, err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_worlds_loaded(self, ok: bool, folders: list[str], err: str) -> None:
        if not ok:
            self._set_busy(False)
            self.status_var.set(f"Error: {err}")
            return
        if not folders:
            self.world_combo["values"] = []
            self.backups = []
            self.listbox.delete(0, "end")
            self._set_busy(False)
            self.status_var.set("No backups found in Google Drive yet.")
            return

        self.world_combo["values"] = folders
        preferred = ""
        if self.cfg.selected_worlds:
            pref_slug = safe_world_slug(self.cfg.selected_worlds[0]).lower()
            for f in folders:
                if f.lower() == pref_slug:
                    preferred = f
                    break
        self.world_slug_var.set(preferred or folders[0])
        self._load_backups_for_selected()

    def _load_backups_for_selected(self) -> None:
        slug = self.world_slug_var.get().strip()
        if not slug:
            return
        self.target_dir_var.set(self._guess_target_for_slug(slug))
        self._set_busy(True)
        self.status_var.set(f"Listing backups for '{slug}'...")

        def worker() -> None:
            ok, zips, err = list_remote_backups(slug, self.cfg)
            self.after(0, lambda: self._on_backups_loaded(slug, ok, zips, err))

        threading.Thread(target=worker, daemon=True).start()

    def _on_backups_loaded(self, slug: str, ok: bool, zips: list[str], err: str) -> None:
        self.listbox.delete(0, "end")
        if not ok:
            self.backups = []
            self._set_busy(False)
            self.status_var.set(f"Error listing '{slug}': {err}")
            return
        self.backups = zips
        for z in zips:
            self.listbox.insert("end", z)
        if zips:
            self.listbox.selection_set(0)
            self.listbox.activate(0)
            self.status_var.set(f"Found {len(zips)} backup(s) for '{slug}'. Latest is selected.")
        else:
            self.status_var.set(f"No .zip backups found for '{slug}'.")
        self._set_busy(False)

    def _on_restore(self) -> None:
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showwarning("No Backup Selected", "Please choose a backup from the list.", parent=self)
            return
        slug = self.world_slug_var.get().strip()
        zip_name = self.backups[sel[0]]
        target = self.target_dir_var.get().strip()
        if not target:
            messagebox.showwarning("No Destination", "Please enter a destination folder.", parent=self)
            return

        confirmed = messagebox.askyesno(
            "Confirm World Restore",
            f"Restore backup:\n  {zip_name}\n\nInto folder:\n  {target}\n\n"
            "If this folder already exists, a safety snapshot will be saved before replacing it. Continue?",
            parent=self,
        )
        if not confirmed:
            return

        self._set_busy(True)
        self.status_var.set(f"Restoring {zip_name}...")

        def worker() -> None:
            res = restore_world_from_drive(
                slug,
                zip_name,
                target,
                self.cfg,
                progress_cb=lambda m: self.after(0, lambda msg=m: self.status_var.set(msg)),
            )
            self.after(0, lambda: self._after_restore_done(res))

        threading.Thread(target=worker, daemon=True).start()

    def _after_restore_done(self, res) -> None:
        self._set_busy(False)
        self.status_var.set(res.message)
        if res.success:
            self.on_restored(res.message)
            extra = f"\n\nPre-restore safety zip:\n{res.safety_zip_path}" if res.safety_zip_path else ""
            messagebox.showinfo("Restore Complete", f"{res.message}{extra}", parent=self)
            self.destroy()
        else:
            messagebox.showerror("Restore Failed", res.message, parent=self)


def run_cli(args: argparse.Namespace) -> int:
    cfg = load_config()

    if args.list_worlds:
        worlds = discover_worlds(cfg.selected_worlds, cfg.custom_search_roots)
        selected = {str(Path(p).resolve()).lower() for p in cfg.selected_worlds}
        for i, w in enumerate(worlds, start=1):
            mark = "*" if str(Path(w.path).resolve()).lower() in selected else " "
            lock = "LOCKED" if w.is_locked else "Ready"
            print(f"[{mark}] {i}. {w.display_name} | {w.source_label} | {w.last_modified_str} | {w.size_mb_str} | {lock}")
            print(f"    {w.path}")
        return 0

    if args.select:
        new_sel = []
        for raw in args.select:
            p = Path(raw).resolve()
            if not is_valid_world_dir(p):
                print(f"Error: invalid world folder (missing level.dat): {raw}", file=sys.stderr)
                return 1
            new_sel.append(str(p))
        cfg.selected_worlds = new_sel
        save_config(cfg)
        print(f"Selected {len(new_sel)} world(s).")

    if args.connect_gdrive:
        ok, msg = connect_google_drive_interactive(cfg)
        print(msg)
        return 0 if ok else 1

    if args.status:
        ok, drive_msg = check_rclone_status(cfg)
        state = load_state()
        print(f"Google Drive: {'READY' if ok else 'NOT READY'} - {drive_msg}")
        print(f"Watcher running: {is_watcher_running()}")
        print(f"Auto-start on login: {is_startup_installed()}")
        print(f"Selected worlds ({len(cfg.selected_worlds)}): {cfg.selected_worlds}")
        print(f"Pending uploads: {len(state.get('pending_uploads', []))}")
        return 0

    if args.install_startup:
        ok, msg = install_startup()
        print(msg)
        return 0 if ok else 1

    if args.uninstall_startup:
        ok, msg = uninstall_startup()
        print(msg)
        return 0 if ok else 1

    if args.backup_now:
        if not cfg.selected_worlds:
            print("No worlds selected. Pass --select <path> or open the GUI.", file=sys.stderr)
            return 1
        exit_code = 0
        for wp in cfg.selected_worlds:
            res = backup_world(wp, cfg, reason="cli_manual", force=True, skip_upload=args.local_only)
            print(res.message)
            if not res.zip_path:
                exit_code = 1
        return exit_code

    if args.flush_uploads:
        n = flush_pending_uploads(cfg, get_logger())
        print(f"Uploaded {n} queued backup(s).")
        return 0

    if args.start_watcher:
        ok, msg = start_watcher_background()
        print(msg)
        return 0 if ok else 1

    if args.stop_watcher:
        n = stop_watcher_processes()
        print(f"Stopped {n} watcher process(es).")
        return 0

    if args.list_remote is not None:
        if args.list_remote == "__ALL__":
            ok, folders, err = list_remote_worlds(cfg)
            if not ok:
                print(f"Error: {err}", file=sys.stderr)
                return 1
            for f in folders:
                print(f)
            return 0
        ok, zips, err = list_remote_backups(args.list_remote, cfg)
        if not ok:
            print(f"Error: {err}", file=sys.stderr)
            return 1
        for z in zips:
            print(z)
        return 0

    if args.restore:
        if not args.target:
            print("Error: --restore requires --target <world_folder>", file=sys.stderr)
            return 1
        zip_name = args.zip
        if not zip_name:
            ok, zips, err = list_remote_backups(args.restore, cfg)
            if not ok or not zips:
                print(f"Error finding backups for '{args.restore}': {err or 'none found'}", file=sys.stderr)
                return 1
            zip_name = zips[0]
        res = restore_world_from_drive(args.restore, zip_name, args.target, cfg, progress_cb=print)
        print(res.message)
        return 0 if res.success else 1

    # Default: launch GUI
    root = tk.Tk()
    BackupManagerGUI(root)
    root.mainloop()
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Minecraft -> Google Drive Auto-Backup Manager")
    p.add_argument("--list-worlds", action="store_true", help="List discovered Minecraft worlds")
    p.add_argument("--select", nargs="+", help="Set selected world folder path(s)")
    p.add_argument("--status", action="store_true", help="Print backup system status")
    p.add_argument("--connect-gdrive", action="store_true", help="Run interactive rclone Google Drive sign-in")
    p.add_argument("--backup-now", action="store_true", help="Immediately back up selected world(s)")
    p.add_argument("--local-only", action="store_true", help="With --backup-now, skip Drive upload")
    p.add_argument("--flush-uploads", action="store_true", help="Retry pending Google Drive uploads")
    p.add_argument("--start-watcher", action="store_true", help="Start background watcher")
    p.add_argument("--stop-watcher", action="store_true", help="Stop background watcher")
    p.add_argument("--install-startup", action="store_true", help="Enable watcher auto-start on Windows logon")
    p.add_argument("--uninstall-startup", action="store_true", help="Remove watcher auto-start")
    p.add_argument(
        "--list-remote",
        nargs="?",
        const="__ALL__",
        default=None,
        metavar="WORLD_SLUG",
        help="List worlds or backup zips in Google Drive",
    )
    p.add_argument("--restore", metavar="WORLD_SLUG", help="Restore a world from Google Drive")
    p.add_argument("--zip", metavar="ZIP_NAME", help="Specific backup zip filename to restore (defaults to latest)")
    p.add_argument("--target", metavar="PATH", help="Destination folder path for --restore")
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(run_cli(parse_args()))
