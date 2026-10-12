# Contributing to TeleDrive

TeleDrive includes a Docker Web UI, a PySide6 desktop client, and an Astro marketing website.

- Keep pull requests focused and explain changes and verification.
- Never commit Telegram sessions, API hashes, authentication codes, database credentials, env files or logs.
- Run Python regression tests: `python -m pytest -q`.
- Compile Python modules: `python -m compileall -q uploader.py db.py web app tests`.
- Validate Compose: `docker compose config --quiet`.
- For website changes: `cd website && npm ci && npm run build`.
- Update the **root README.md** for user-facing instructions; do not introduce additional README files.
- Send vulnerability reports privately as described in [SECURITY.md](SECURITY.md).

Packaging helpers are in **scripts/** and Docker Web UI sources in **web/**.
