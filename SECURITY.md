# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in **mcBackup**, please report it responsibly:

- Open a private security advisory on GitHub at [tex1ure/mcBackup/security/advisories](https://github.com/tex1ure/mcBackup/security/advisories), or
- Open an issue with minimal public details and request a private follow-up.

## Security Practices

- **OAuth & Credentials:** mcBackup never stores or handles your Google password or OAuth tokens directly. Authentication is delegated to `rclone`, which stores your encrypted/local token in your OS user config (`rclone.conf`).
- **Local-Only Execution:** The background watcher (`watcher.py`) and GUI (`app.py`) run strictly locally on your machine with no external telemetry servers.
