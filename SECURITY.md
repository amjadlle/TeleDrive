# Security Policy

## Reporting a vulnerability

Please do not publish credentials, Telegram session files, or exploitable details in
a public issue. Report security concerns privately through the repository's GitHub
security contact/private vulnerability reporting when available.

When reporting an issue, include the affected version, operating system, steps to
reproduce, and the potential impact. Remove personal data and secrets from logs.

Never share your Telegram API hash, login code, two-step password, database
credentials, Web UI password, or session file.

## Trust model

TeleDrive is a single-administrator application. The Web UI provides administrative
control over Telegram accounts, upload targets, source paths, and the state database.

The Docker host/daemon, the host user controlling mounted TeleDrive data, the
configured database server, installed Python/database-driver packages, and Telegram
itself are trusted boundaries. TeleDrive does not try to sandbox a malicious host
administrator who can rewrite mounted files while the application is running.

Treat these as secrets:

- Telethon `*.session` files;
- `config.yaml`;
- Web UI password/password file;
- database connection URLs;
- Telegram login codes and 2FA passwords.

## Network exposure

Docker Compose binds the Web UI to `127.0.0.1` by default. For remote access, use
an HTTPS reverse proxy or VPN, configure `TELEDRIVE_ALLOWED_HOSTS`, use a unique
Web password of at least 16 characters, and restrict network access further where
practical. HTTP Basic authentication must not be exposed over untrusted plaintext
HTTP.

## Telegram confidentiality

Normal Telegram cloud chats/channels and Saved Messages are not Secret Chats.
TeleDrive is therefore not an end-to-end encrypted backup system. Encrypt sensitive
files before upload if your threat model requires confidentiality independent of
Telegram.

## Database and driver security

`state.database_url` accepts synchronous SQLAlchemy dialects and intentionally has
no vendor whitelist. Install only reviewed dialect/DBAPI packages, pin additional
driver versions where practical, enable the database vendor's TLS options for remote
connections, and scope the database identity to the TeleDrive schema/database.

Initial schema creation/migration requires DDL permissions. Those permissions may be
reduced after migrations if your operating process supports that.

## Runtime defenses

The Docker deployment defaults to non-root execution, dropped Linux capabilities,
`no-new-privileges`, a read-only root filesystem, read-only `/uploads`, restrictive
secret-file permissions, storage path allowlists, symlink/non-regular-file rejection,
a distributed uploader lease, atomic per-file claims, Web authentication rate
limiting, Host validation, CSRF protection, security headers, and credential
redaction.

## CI

Security CI performs compilation, regression tests, `pip check`, Bandit,
`pip-audit`, Docker build, and Trivy scanning for fixable HIGH/CRITICAL image
vulnerabilities. GitHub Actions are pinned to immutable commit SHAs and Dependabot
monitors Python, Docker, and Actions dependencies.
