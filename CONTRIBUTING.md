# Contributing to mcBackup

Thank you for your interest in contributing to **mcBackup**!

## How to Contribute

1. **Fork the repository** on GitHub.
2. **Create a feature branch** from `main`:
   ```bash
   git checkout -b feat/your-feature-name
   ```
3. **Make your changes** and ensure all unit tests pass:
   ```bash
   python -m unittest discover -s tests -v
   ```
4. **Commit** using clear, descriptive messages (e.g., `feat: ...`, `fix: ...`, `docs: ...`).
5. **Open a Pull Request** describing what you changed and why.

## Guidelines

- Keep external dependencies to zero for the core runtime (Python standard library + `rclone` CLI) so installers stay lightweight.
- Test cross-platform behavior where applicable (Windows, macOS, Linux).
- Do not commit personal `data/config.json`, `data/state.json`, or `rclone.conf` credentials.
