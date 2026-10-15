# Security Policy

> **Scope:** vulnerability reporting, trust boundaries, network exposure, secret handling, and security validation for the TeleDrive fork.
> **Do not report vulnerabilities or post credentials in public issues.**

## 1. Report a vulnerability privately

Use the repository's [GitHub Security tab](https://github.com/babico/TeleDrive/security) and private vulnerability reporting when available. Include the affected release or commit, operating system, minimal reproduction steps, expected/observed impact, and carefully redacted logs.

Do **not** submit active credentials, session files, database connection URLs, Telegram messages or exploitable details in a public issue. If private reporting is unavailable, contact the repository maintainer through an appropriately private channel rather than exposing an unpatched exploit.

## 2. Threat model and trusted components

TeleDrive is primarily a **single-administrator** application. The WebUI can manage Telegram accounts, target channels, local source paths, credential issuance and metadata state. Treat this authority as sensitive.

The host OS administrator, Docker daemon, host user managing mounted files, SQL server administrator, reviewed Python/database drivers and Telegram itself are **trusted boundaries**. TeleDrive does not sandbox a malicious host administrator who can modify mounts or binaries while the service runs.

Telegram Saved Messages and ordinary cloud channels are **not end-to-end encrypted**. Encrypt sensitive payloads separately if Telegram must not see their contents.

## 3. Secrets and credentials

Treat all of the following as secrets:

| Secret | Consequence if exposed |
| --- | --- |
| Telethon `*.session`, Telegram API hash, login codes and 2FA | User-account and channel access |
| Historical Bot token and private channel identifiers | Isolated historical recovery/test access; never used by the live unified runtime |
| `config.yaml`, `.env`, Web password | Administrative control and deployment information |
| SQL URL and SQL backups | Object manifests, references, jobs and potentially encrypted credential metadata |
| `config/s3-master.key` and S3 access keys | S3 key decryption and object API access |

Store secrets in protected volumes or a secrets manager with restrictive file permissions. Never commit them, print them to CI logs, or paste them into bug reports. When a secret is compromised, rotate/revoke it using the applicable Telegram, WebUI or S3 credential controls; review the dependent backups and sessions.

## 4. Network exposure

The default Docker Compose deployment uses **one container with two loopback-bound listeners**:

| Interface | Default host binding | Authentication |
| --- | --- | --- |
| WebUI / REST | `127.0.0.1:8080` | Basic admin and scoped REST Bearer tokens |
| S3 | `127.0.0.1:9000` | Scoped S3 SigV4 access/secret keys |

Do not expose either endpoint over untrusted plaintext HTTP. For remote access, use an HTTPS reverse proxy or VPN, set `TELEDRIVE_ALLOWED_HOSTS` appropriately, use a unique Web password of at least 16 characters, and restrict firewall access.

The unified server dispatches by its actual socket-local listener port, **not** by client-supplied Host or forwarding headers. REST Bearer tokens are not interchangeable with S3 SigV4 keys. S3 master-key material must not be embedded in URLs or requests.

## 5. Database, files and runtime defenses

`state.database_url` accepts synchronous SQLAlchemy dialects without a hardcoded vendor whitelist. Review and pin additional driver packages, use database TLS where available, and scope the SQL principal to the intended schema/database. Initial schema creation/migrations may need DDL privileges; reduce privileges after migrations when operationally feasible.

The Docker default includes non-root execution, dropped Linux capabilities, `no-new-privileges`, a read-only root filesystem, a read-only `/uploads` mount, protected secret files, source/session/log/SQL path allowlists, non-regular and symlink checks, upload leases and atomic claims, rate limiting, Host validation, CSRF protection, security headers and credential redaction.

A SQL snapshot alone cannot restore lost Telegram payloads or S3 master keys. Independent originals and rehearsed recovery are required. Physical Telegram garbage collection remains disabled pending the [production recovery and safety controls](IMPLEMENTATION_STATUS.md#41-physical-telegram-garbage-collection-disabled).

## 6. Security testing and release evidence

The [Security and Regression workflow](../.github/workflows/quality-security.yml) covers Python compilation/regression, `pip check`, Bandit, `pip-audit`, Docker image build, Trivy and application listener smoke tests. GitHub Actions are pinned to immutable revisions; Dependabot monitors Python, Docker and Actions dependencies.

A successful scanner run is **not** permission to expose the administration UI publicly or a substitute for owner-approved production penetration testing. Consult the [CI and Test Guide](CI_WORKFLOWS.md) and distinguish test-channel E2E from actual production data.
