<p align="center">
  <img src="assets/logo.png" alt="mcBackup Logo" width="220">
</p>

# mcBackup — Minecraft -> Google Drive Auto-Backup

Automatically creates timestamped `.zip` backups of your chosen Minecraft world(s) and uploads them to Google Drive via `rclone` after every **Save & Quit**, **Quit Game**, or **Windows Shutdown/Logoff**.

## How It Works

1. **Instant Save & Quit / Exit Detection (`watcher.py` + `lock_detect.py`)**:
   - While Minecraft has a world open, it holds an exclusive Windows file lock on `session.lock`.
   - The moment you click **Save and Quit to Title** or close Minecraft, `session.lock` is released.
   - The background watcher detects the transition (`LOCKED -> UNLOCKED`), waits 2 seconds for disk flushes to finish, zips the world atomically, uploads it to Google Drive, and prunes old archives beyond your retention limit (default: keep latest 15).
   - Also listens for `WM_QUERYENDSESSION` so if you turn off / shut down Windows, it snapshots a local backup immediately and uploads any pending backups automatically on next boot.
2. **Interactive World Selector (`app.py`)**:
   - Auto-discovers worlds from PolyMC, PrismLauncher, Vanilla `.minecraft`, Modrinth, CurseForge, Bedrock, and custom folders (`A:\minecraft\game\saves`, etc.).
   - Lets you pick/switch world(s), connect Google Drive with one click, trigger manual backups, and toggle auto-start on Windows login.

## Quick Start

Double-click `Start Backup Manager.bat` (or run `python app.py`):

1. Click **Connect Google Drive** to sign into your Google account in your browser (uses `rclone`).
2. Click the Minecraft world(s) you want to protect (e.g. `World` in PolyMC `26.3`).
3. Check **Start Watcher on Windows Login** if you want it running automatically on every boot.
4. Click **Save & Start Background Watcher**.

## CLI Usage

```powershell
python app.py --list-worlds
python app.py --select "C:\Users\abhir\AppData\Roaming\PolyMC\instances\26.3\.minecraft\saves\World"
python app.py --connect-gdrive
python app.py --backup-now
python app.py --install-startup
python app.py --start-watcher
python app.py --status
```
