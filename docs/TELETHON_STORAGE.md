# Telethon USER Storage — Installation, Recovery and Migration

> **Status:** development `main`, not the published v1.0.7 binaries.
> **Scope:** the single-container WebUI/REST/S3/local-job object engine. The
> separate historic folder uploader and archived Bot fixtures are not new
> storage backends. For owner-only production approval see
> [Project Status](IMPLEMENTATION_STATUS.md) and [Local Acceptance](LOCAL_AGENT_ACCEPTANCE.md).

## 1. One storage architecture

- The only live unified payload transport is **an authorized Telegram USER
  account over Telethon MTProto**, not a Telegram Bot and not Saved Messages.
- REST, S3 and explicitly queued local-file jobs publish into **the same SQL
  heads, object/version manifests and Telegram channel**. The Docker default
  uses one `teledrive` container with local ports 8080 and 9000.
- Choose a **dedicated private broadcast channel** where the authorized user
  has posting rights. The channel must be in the user's accessible dialogs;
  for numeric private IDs the runtime resolves within the first 5,000 dialogs.
- `TELEDRIVE_STORAGE_TRANSPORT=telethon` is the only accepted mode; omitting
  the variable also selects `telethon`. Explicit `bot` fails closed. Old
  `TELEDRIVE_STORAGE_BOT_*` values are ignored by the unified server.
- Telethon's `device_model` and `app_version` identify **TeleDrive Storage
  Client**. They are not an HTTP User-Agent and do not claim to be an official
  Telegram client. Respect Telegram flood waits and account usage limits.

## 2. Provision one authorized user session

Work on the owner-controlled host; the same identity must be able to post to
the dedicated channel. Never put phone codes, 2FA passwords or session bytes
in a Git repository, shell history, issue, CI log or the Docker environment.

```bash
git clone https://github.com/babico/TeleDrive.git
cd TeleDrive
mkdir -p config data uploads
cp .env.example .env
cp examples/config.yaml config/config.yaml
chmod 600 .env config/config.yaml
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt

# Set these privately in the current shell; the program prompts for sign-in.
export TELEDRIVE_STORAGE_MTPROTO_API_ID=YOUR_TELEGRAM_API_ID
export TELEDRIVE_STORAGE_MTPROTO_API_HASH=YOUR_TELEGRAM_API_HASH
python scripts/create_storage_user_session.py --output config/mtproto-user.session
chmod 600 config/mtproto-user.session
```

Obtain API credentials from [my.telegram.org/apps](https://my.telegram.org/apps).
Generate the session **before** starting a configured TeleDrive container;
the script refuses to overwrite an existing session. Put these keys in the
private `.env` (do not commit them):

```dotenv
TELEDRIVE_STORAGE_TRANSPORT=telethon
TELEDRIVE_STORAGE_MTPROTO_API_ID=YOUR_TELEGRAM_API_ID
TELEDRIVE_STORAGE_MTPROTO_API_HASH=YOUR_TELEGRAM_API_HASH
TELEDRIVE_STORAGE_MTPROTO_CHANNEL=YOUR_PRIVATE_CHANNEL_ID_OR_USERNAME
TELEDRIVE_STORAGE_MTPROTO_ACCOUNT=mtproto-user
```

`docker-compose.yml` reads the session only from
`./config/mtproto-user.session` mounted at
`/config/mtproto-user.session`. Ensure its owner/group and mode permit the
configured non-root `PUID:PGID` to read it. **Never** copy the StringSession
into `.env`. Set a unique 16+-character Web password and generate an
independently backed-up S3 master key as documented in [README](../README.md#4-s3-compatible-api).

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=120 teledrive
```

The listeners may be reachable before a new installation is provisioned, but
**uploads remain unavailable until the live authenticated session connects**.
Any partially configured identity may prevent startup; correct the credentials
or restore the sealed session rather than falling back to a Bot.

## 3. What stays readable after retiring Bot uploads

| Historical payload | Can the USER-only server read it? | Safe action |
| --- | --- | --- |
| New USER channel object with intact SQL | **Yes**, if the original account/channel/session is accessible | Read and SHA-256-verify |
| Old `api-bot` chunk in the **same** accessible channel with original SQL `chat_id` and `message_id` | **Yes**, via Telethon message lookup without any Bot token | Keep original SQL; verify every chunk/full hash |
| Old Bot chunk in a different/inaccessible channel | **No automatic fallback** | Preserve media, credentials and SQL; independently authorize a read-only migration |
| Missing SQL or unknown channel marker | **Unverified candidate only** | Inventory read-only; require independent bucket/key/size/full-hash/version evidence |

The server never rewrites historic object manifests to invent ownership,
transfers payloads from inaccessible channels, or enables physical deletion.
Removing Bot configuration **does not prove** every existing object is
recoverable. Back up first and test an isolated restore.

## 4. Read-only history and original-media disaster recovery

With a connected owner USER session, the authenticated admin can inspect
bounded history pages:

```bash
curl -u admin \
  'http://127.0.0.1:8080/api/v1/admin/storage/channel-history?limit=100&include_external_markers=true'
```

Use `next_before_message_id` from the result as
`before_message_id` for older pages. Other-sender/Bot markers are **not**
ownership, bucket/key, full SHA-256 or version proofs. No media is downloaded,
deleted or indexed by this endpoint.

For an **original owner-provided sealed SQLite metadata snapshot**, use the
read-only user-MTProto DR command in an **isolated environment**, not with a
production writer active on the same session:

```bash
# In the staging shell, set TELEDRIVE_STORAGE_MTPROTO_API_ID and
# TELEDRIVE_STORAGE_MTPROTO_API_HASH, and point the following variable to
# a protected owner-authorized copy of the original USER StringSession.
export TELEDRIVE_STORAGE_MTPROTO_SESSION_FILE=/secure/staging/owner-user.session
python scripts/rehearse_telegram_dr.py \
  --snapshot /secure/staging/sealed-original.db \
  --sha256 EXPECTED_ARCHIVE_SHA256 \
  --bucket ORIGINAL_BUCKET --key ORIGINAL_KEY \
  --account ORIGINAL_ACCOUNT --chat-id ORIGINAL_CHAT_ID \
  --target ORIGINAL_CHANNEL_ID_OR_USERNAME
```

The tool checks the archive fingerprint, opens a **temporary clone**, validates
the original channel identity, reads via user MTProto and verifies whole-body
SHA-256, exact size and a byte range. No upload, remote delete or production
SQL mutation occurs. An old `api-bot` object is readable if its immutable SQL
reference points to **this same authorized channel**. This is a sample-object
DR check—not proof of a complete independent payload backup or full archive
recovery. PostgreSQL/MySQL owners must rehearse their native SQL restores
separately.

For objects with no reliable SQL map, use
`scripts/inventory_telegram_history.py` (private candidate inventory)
and `scripts/rebuild_from_telegram.py` (separately proven plan, new isolated
catalog); see [Backup and Restore](BACKUP_RESTORE.md).

## 5. Status, CI and unfinished safety gates

The exact-source 9-workflow baseline
[`a430212`](https://github.com/babico/TeleDrive/commit/a4302122ff80d0f5d05bab8a362e1983a5910235)
passed Security, REST/S3, Databases, Desktop, Docker fault injection, live
USER MTProto, live Bot **historical-compatibility** E2E, live Multiworker
PostgreSQL and release gate. Check [Actions](https://github.com/babico/TeleDrive/actions)
for any newer commit; PR tests alone do not certify production recovery.

**Still open:** independently archived media bytes and real restore;
unknown-history reconciliation without invented metadata; complete
writer/version/alias-aware GC fencing; physically separate host acceptance;
owner-approved long-duration capacity/429 telemetry; genuine legacy cutover
or signed no-legacy exemption; two-person production approval; and
release-candidate checks on the new exact SHA. The advanced S3 feature
matrix is tracked separately in [S3 Specs](S3_SPECS.md).

**Physical Telegram deletion remains disabled.** Original Telegram message
IDs cannot be resurrected from a SQL snapshot. Follow the
[owner-local mission](LOCAL_AGENT_ACCEPTANCE.md); never mark these gates
passed based on synthetic fixtures or a green unit-test suite.
