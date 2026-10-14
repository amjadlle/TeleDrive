# TeleDrive Knowledge Base

> **Audience:** prospective users and new administrators.
> **Current repository:** [babico/TeleDrive](https://github.com/babico/TeleDrive).
> **Full instructions:** [README](https://github.com/babico/TeleDrive/blob/main/README.md). The marketing site is not the administrative WebUI.

## 1. What is TeleDrive?

TeleDrive is a Telegram-backed file uploader and object-storage application for Windows, Linux and server/NAS deployments. It provides a desktop application, a headless Telethon uploader and a Docker service with WebUI, REST v1 and a scoped S3-compatible API.

**Payloads live in Telegram; metadata lives in SQL.** This does not make Telegram an unlimited or guaranteed-availability storage service, nor an end-to-end encrypted backup. Telegram account restrictions, Bot API file-size limits, available channel permissions and rate limits still apply.

## 2. Choose an upload workflow

| Workflow | Credential | Use case |
| --- | --- | --- |
| Desktop or headless folder scanner | Telegram user session (MTProto) | Personal and scheduled folder uploads, multi-account strategies |
| Docker WebUI and REST v1 | Admin credentials or scoped REST Bearer token; storage Bot | API-driven object upload/readback |
| S3-compatible gateway | Scoped S3 SigV4 access key; storage Bot | Selected boto3/AWS CLI/rclone object operations |
| Durable local-file job | Authenticated admin API; storage Bot | Publish a file from the allowed mount into the **same** REST/S3 catalog |

The **Docker deployment runs one `teledrive` container**, exposing WebUI/REST at `127.0.0.1:8080` and S3 at `127.0.0.1:9000`. S3 does not need a second container or optional profile. The legacy user-account folder scanner and the shared Bot-backed REST/S3 catalog are **not automatically cross-indexed**.

## 3. Install or launch

### Docker server

~~~bash
git clone https://github.com/babico/TeleDrive.git
cd TeleDrive
mkdir -p config data uploads
cp .env.example .env
cp examples/config.docker.yaml config/config.yaml
# Set a unique long TELEDRIVE_WEB_PASSWORD and configure credentials securely.
docker compose up -d --build
~~~

Consult the [Docker quick start](https://github.com/babico/TeleDrive#1-quick-start-docker) for mounts, credentials and remote HTTPS access.

### Windows and Linux desktop

Download the **appropriate asset for your OS** from [GitHub Releases](https://github.com/babico/TeleDrive/releases); do not assume an old upstream v1.0.6 installer includes fork-specific Docker, API or recovery features. Alternatively, run the application from source:

~~~bash
python -m pip install -r requirements/desktop.txt
python -m app.main
~~~

On first launch, enter your Telegram `api_id` / `api_hash` from [my.telegram.org](https://my.telegram.org/apps), authorize the user account using its login code and optional 2FA password, choose the source and destination, and test one batch before enabling automatic scheduling.

## 4. Common questions

**Do I need BotFather?** Not for the **user-account folder uploader**. The REST/S3 object service uses the official Telegram Bot API and **does** require a Bot token and posting permission in a private storage channel.

**Does a file copied into the Docker mount upload automatically?** No. Submit an authenticated durable upload job. The background job runner processes explicitly queued files by default in Compose.

**Can I use multiple Telegram user accounts?** Yes, with separately authorized sessions and single/round-robin/failover/parallel strategies for the original uploader. Do not share a writable session file between worker processes.

**Can I recover deleted Telegram messages from SQL?** No. SQL records message identities and hashes; it is not a replica of Telegram payload bytes. [Backup and Restore](https://github.com/babico/TeleDrive/blob/main/docs/BACKUP_RESTORE.md) explains the limitations.

**Is every AWS S3 feature available?** No. TeleDrive implements a tested subset. Consult [Project Status](https://github.com/babico/TeleDrive/blob/main/docs/IMPLEMENTATION_STATUS.md) for explicit unsupported behavior.

**Is Telegram storage private and end-to-end encrypted?** Ordinary cloud chats and Saved Messages are not end-to-end encrypted. Encrypt sensitive content separately.

## 5. Support, licensing and attribution

For source, updates and versioned binaries, use [babico/TeleDrive](https://github.com/babico/TeleDrive) and [GitHub Releases](https://github.com/babico/TeleDrive/releases). To report a security issue, read the [Security Policy](https://github.com/babico/TeleDrive/blob/main/docs/SECURITY.md) before posting.

The original project was created by [Amjad P A](https://github.com/amjadlle/TeleDrive). This repository is a fork with additional Docker, API, multi-account and recovery work; see the repository's [MIT license](https://github.com/babico/TeleDrive/blob/main/LICENSE) for licensing and attribution.
