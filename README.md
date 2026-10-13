# TeleDrive

**Telegram personal backup uploader** — Docker Web UI, Windows/Linux desktop app and an automation CLI. This fork adds multi-account scheduling and SQLAlchemy-backed persistent state.

> Telegram Saved Messages and ordinary Telegram cloud chats are **not end-to-end encrypted**. Encrypt sensitive files before uploading when confidentiality from Telegram is required.

## Quick start: Docker Web UI

The server/NAS deployment has a browser dashboard, upload queue, history, account management, Telegram login prompts and logs.

~~~bash
git clone -b feature/docker-webui-multiaccount https://github.com/babico/TeleDrive.git
cd TeleDrive
mkdir -p config data uploads
cp .env.example .env
cp examples/config.docker.yaml config/config.yaml
chmod 600 .env config/config.yaml
~~~

Edit **.env** and set a unique **TELEDRIVE_WEB_PASSWORD** (minimum 16 characters). Set **PUID** and **PGID** to the host user owning the mounted folders if they differ from 1000. Then:

~~~bash
docker compose up -d --build
docker compose logs -f teledrive
~~~

Open **http://127.0.0.1:8080** on the Docker host. The port binds to loopback by default. For remote administration use an SSH tunnel, VPN or HTTPS reverse proxy. Never expose HTTP Basic authentication over untrusted plaintext HTTP.

In **Settings**, enter the Telegram API ID/hash from [my.telegram.org](https://my.telegram.org/apps), each enabled account's phone number and upload target, then select a strategy. Choose **Run batch**; the browser prompts for login codes and 2FA passwords as needed.

Docker mounts: **./config → /config** (settings), **./data → /data** (sessions, auth handoff, default SQLite DB), and **./uploads → /uploads:ro** (source files). To upload a different host directory, change only the host-side path of the **/uploads** bind mount.

The Docker config example is [examples/config.docker.yaml](examples/config.docker.yaml). No desktop environment, X11 or PySide6 is needed inside Docker.

## OpenAPI / Web API reference

The Docker Web UI publishes **authenticated interactive API documentation** at:

- **/docs** — Swagger UI
- **/redoc** — ReDoc
- **/openapi.json** — OpenAPI JSON specification

Example (local host):

~~~bash
curl -u admin:YOUR_PASSWORD http://127.0.0.1:8080/openapi.json
~~~

These endpoints require the same HTTP Basic credentials as the administration UI. On requests that mutate state (`POST`, `PUT`), also send `X-TeleDrive-CSRF: 1`. The API should be accessed over an HTTPS reverse proxy or VPN when remote.

## Multi-account strategies

| Strategy | Behavior |
| --- | --- |
| **single** | Use the first enabled Telegram account |
| **round_robin** | Rotate the preferred account between successive files, with fallback |
| **failover** | Try accounts in configured priority order |
| **parallel** | Concurrent account-affine workers, with fallback |

Each account has a separate session file and optional API/target overrides. FloodWait and cooldown state are account-specific, and upload history records the sending account. Disabled accounts remain in settings without connecting.

**Saved Messages caution:** the target **me** points to a different private Saved Messages area for every account. If you want one destination, use a shared private channel and grant posting access to all accounts.

Telegram limits still apply. Oversized files can be split into lossless parts before uploading.

## Database: synchronous SQLAlchemy dialects

Configure **state.database_url** in YAML or the Web UI. SQLite works without a database server; PostgreSQL and MariaDB/MySQL drivers are included in the Web image. Other synchronous SQLAlchemy dialects can be used when the dialect, DBAPI driver and any required OS libraries are installed. There is no hardcoded database vendor whitelist, but not every third-party dialect is necessarily tested or compatible.

~~~yaml
state:
  database_url: "sqlite:////data/state.db"
~~~

Other database URL examples:

~~~text
postgresql+psycopg://user:password@db:5432/teledrive
mysql+pymysql://user:password@db:3306/teledrive
oracle+oracledb://user:password@db:1521/?service_name=FREEPDB1
mssql+pyodbc://user:password@db/teledrive?driver=ODBC+Driver+18+for+SQL+Server
~~~

To install another trusted dialect/driver at Docker image build time, set **TELEDRIVE_DB_EXTRA_PACKAGES** in **.env**, then rebuild:

~~~dotenv
TELEDRIVE_DB_EXTRA_PACKAGES=oracledb
~~~

~~~bash
docker compose build --no-cache
docker compose up -d
~~~

Some drivers, especially ODBC variants, also require system libraries in a derived Docker image. For remote databases, use TLS and grant only the permissions TeleDrive needs.

## Desktop app (Windows/Linux)

A PySide6 desktop UI remains available, including multiple Telegram accounts, strategy selection, database URL settings, account-scoped login/2FA and SQLAlchemy-backed queue/history. The desktop example is [examples/config.desktop.yaml](examples/config.desktop.yaml); the application creates its own configuration under the user's application data directory.

~~~bash
python -m pip install -r requirements.txt
python -m app.main
~~~

Desktop data lives in **%LOCALAPPDATA%/Telegram Auto Upload** on Windows or **~/.local/share/Telegram Auto Upload** on Linux (unless **XDG_DATA_HOME** is set).

Build helpers are in **scripts/**:

~~~powershell
# Windows: portable executable, then Inno Setup installer
./scripts/build.ps1
./scripts/build-installer.ps1
~~~

~~~bash
# Linux: PyInstaller package in dist-installer/
bash scripts/build-linux.sh
~~~

The release workflow uses these scripts for version tags. Upstream's existing binary releases do **not** necessarily include this fork's Docker or multi-account features.

### Desktop screenshots

![Desktop dashboard](website/public/images/dashboard.png)
![Desktop upload queue](website/public/images/upload-queue.png)
![Desktop settings](website/public/images/settings.png)

## CLI and automation

The scan/upload backend also runs without a UI. Copy the desktop example to a writable config and edit its source path and Telegram credentials first.

~~~bash
cp examples/config.desktop.yaml local-config.yaml
python -m teledrive.uploader --config local-config.yaml --scan-only
python -m teledrive.uploader --config local-config.yaml --run-once
~~~

The Web UI's automatic batch scheduler is configured in **Settings → Automation**. Avoid running multiple client processes against the same Telegram session; state leases alone cannot make shared session files safe.

## Telegram setup

1. Log into [my.telegram.org](https://my.telegram.org) with your normal Telegram account, then create credentials under **API development tools**. No BotFather token is needed.
2. Set the destination to **me** (your Saved Messages), a public **@channel**, or the numeric chat ID for a channel/group to which the account can post.
3. Authorize each account with its Telegram login code and, if required, 2FA password.
4. Scan a small folder and test one batch before enabling scheduling.

Keep source files stable during uploads; back up your Telethon sessions and state DB before migrating hosts.

## Security

The Web UI is password-protected by default. Container defaults include loopback binding, non-root execution, dropped Linux capabilities, a read-only root filesystem and read-only upload source. Telegram sessions and auth handoff files have restrictive permissions; distributed leases and per-file claims help prevent duplicate uploads.

See [SECURITY.md](SECURITY.md) for the threat model, TLS/host allowlists, secret-file authentication and database-driver trust. Default options are documented in [.env.example](.env.example) and [docker-compose.yml](docker-compose.yml).

## Website and development

The Astro marketing website under **website/** is separate from the Docker administration UI:

~~~bash
cd website
npm ci
npm run dev
npm run build
~~~

| Path | Purpose |
| --- | --- |
| **teledrive/uploader.py** | Telegram engine, scanning, account strategies |
| **teledrive/db.py** | SQLAlchemy state, migrations, leases and claims |
| **teledrive/desktop.py** | Desktop executable launcher and frozen CLI dispatcher |
| **web/** | FastAPI admin Web UI and static assets |
| **app/** | PySide6 desktop application |
| **scripts/** | Windows/Linux desktop packaging scripts |
| **examples/** | Docker and desktop sample config files |
| **tests/** | Regression/security tests |
| **website/** | Standalone Astro marketing site |
| **installer/** | Windows Inno Setup assets |

Development checks:

~~~bash
python -m pip install -r requirements-web.txt pytest
python -m pytest -q
python -m compileall -q teledrive web app tests
docker compose config --quiet
~~~

Security CI also builds the image, exercises the live Web API/authentication, and performs dependency and image scans.

## Telegram-only object storage: Phase 1 foundation

A new `teledrive.storage` module provides immutable object manifests and
Telegram-backed chunk I/O. Payload bytes are stored **only in Telegram**;
SQLAlchemy stores message locations, offsets, SHA-256 checksums and upload
status. Local scratch files are temporary and deleted after use.

Implemented in this phase:

- Bounded-memory chunk upload with a per-chunk DB journal.
- Fail-closed object visibility: unfinished uploads cannot be read.
- Telegram message retrieval that refreshes expired media references.
- Checksum-verified download, half-open byte ranges, and atomic output files.
- Explicit interrupted/failed upload inspection for future recovery tooling.
- Deterministic tests for corruption, missing chunks, interrupted upload, source mutation,
  chunk boundaries, empty files and SQLite restart; live PostgreSQL/MySQL tests.

**Not yet implemented:** REST upload endpoints, the S3 gateway, API key management,
full crash/orphan reconciliation, S3 multipart semantics, object deletion/versioning,
or an automated migration of legacy `files` entries. This module is a foundation,
not a production-ready S3 service. The legacy folder uploader continues to work
independently until migration and shared job orchestration are added.

### Real Telegram E2E test (requires your dedicated test account)

The repository includes `.github/workflows/telegram-storage-e2e.yml`.
It is **manual only**, cannot execute without configured GitHub Secrets, and
does not run on pull requests or untrusted forks. GitHub requires manually
dispatchable workflows to exist on the repository's default branch; merge the
workflow before starting it via the Actions tab.

Configure under **Settings → Secrets and variables → Actions**:

| Secret | Value |
| --- | --- |
| `TELEDRIVE_E2E_API_ID` | Test account's Telegram API ID |
| `TELEDRIVE_E2E_API_HASH` | Test account's Telegram API hash |
| `TELEDRIVE_E2E_SESSION` | Authorized Telethon StringSession for a dedicated test account |

Optional **Actions variable**: `TELEDRIVE_E2E_TARGET`, defaults to `me`.
Use a dedicated account and a private test target. These values are **not**
needed for the regular Security, Desktop, and Database CI workflows.

To generate the StringSession, install Telethon on a trusted local machine
and run this Python snippet **locally** (never in GitHub Actions or a public issue):

~~~python
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

async def create_test_session():
    api_id = int(input("API ID: "))
    api_hash = input("API hash: ").strip()
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        print("Keep this secret! Telethon session:", client.session.save())

asyncio.run(create_test_session())
~~~

Move the printed session directly to the GitHub secret. A Telethon session
grants account-level access; never post it into chat, commits, screenshots
or CI logs. Revoke the test session in Telegram's Devices settings if exposed.

The E2E scenario uploads a 4,097-byte random file across three Telegram
messages, downloads and SHA-256-verifies it, tests a cross-chunk Range,
and deletes only the messages created by the run. It requires the test account
to have working Telegram access, which ordinary CI cannot simulate.

## Credits and license

Originally created by [Amjad P A](https://github.com/amjadlle/TeleDrive). This is the [babico/TeleDrive fork](https://github.com/babico/TeleDrive) with Docker and multi-account enhancements. Licensed under [MIT](LICENSE); see [CONTRIBUTING.md](CONTRIBUTING.md) for contributions.
