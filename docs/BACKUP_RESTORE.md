# Backup and Restore Operations

> **Audience:** deployment operators and release maintainers.
> **Purpose:** create and verify protected SQL metadata backups and rehearse recovery **without modifying a live installation**.
> **Related:** [Project Status and production sign-off](IMPLEMENTATION_STATUS.md#3-production-acceptance-not-yet-signed-off) · [Security Policy](SECURITY.md).

## 1. What a backup contains

TeleDrive stores **payload bytes in Telegram** and **object references/versions, job state, MTProto message coordinates, and encrypted S3 credential metadata in SQL**. An SQL backup is **not** an independent copy of the Telegram objects.

Protect these as separate, coordinated recovery inputs:

| Asset | Why it matters | Handling |
| --- | --- | --- |
| Metadata database | Object heads, history, chunk identities, durable jobs | Encrypted backup; recorded independent SHA-256 |
| Telegram USER StringSession / API credentials | Required for current REST/S3/local-job access and same-channel message history | Protected `0600` private file/secret manager, independent of SQL archive |
| Historical Bot credentials (if any) | No longer usable by the Telethon-only runtime; retain only as a migration audit trail | Separately protected archive |
| S3 encryption master key | Decrypts issued S3 access secrets | Separate secret backup; never silently rotate/replace |
| Deployment configuration | DB URL, channel target, source mounts and allowlists | Protected copy; no credentials in source control |

Never store these assets in Git, GitHub Actions logs or public test artifacts. Record the SHA-256 and restore instructions in the operator's private change record.

## 2. Back up SQLite metadata

Run on the TeleDrive host with a destination on encrypted, access-controlled storage:

~~~bash
python scripts/backup_metadata.py \
  --database /data/teledrive.db \
  --output /secure-backups/teledrive-YYYY-MM-DD.db
~~~

The command uses SQLite's online backup API, checks `PRAGMA integrity_check`, restricts output to `0600` and refuses to overwrite an existing backup. Save the printed SHA-256 **independently**.

Validate the saved copy without touching the live database:

~~~bash
python scripts/verify_metadata_backup.py \
  --database /secure-backups/teledrive-YYYY-MM-DD.db \
  --sha256 EXPECTED_ARCHIVE_SHA256
~~~

The verifier checks SQLite consistency, foreign keys, current HEAD hashes and published/removed version bookkeeping. It rejects orphan version removal entries and a revoked current HEAD. It does **not** contact Telegram, restore unavailable remote media, rotate secrets or authorize garbage collection.

### SQLite restore rehearsal

1. Create a **separate disposable installation** with workers initially stopped.
2. Copy the verified snapshot to the staging installation's database path with owner-only permissions.
3. Verify schema, SQL HEAD pointers, version history, references, MTProto send intents and credential decryptability with the staged secrets.
4. Reconnect only to approved **test/original owner-authorized** Telegram media, verify full SHA-256 and at least one cross-chunk range.
5. Record the results privately. **Never overwrite a running production database.**

## 3. PostgreSQL and MySQL/MariaDB

### PostgreSQL

Use a consistent native logical backup, for example `pg_dump -Fc`, under a dedicated and appropriately scoped backup role. Retrieve credentials through a secret store; **do not put a password in a shell command or committed script**.

Rehearse a native `pg_restore` into a **new, empty, isolated** database. Compare schema, object heads and generations, published/deleted versions, chunk references and SHA-256 hashes, MTProto send receipts, durable jobs and S3 credential decryptability.

### MySQL / MariaDB

Choose an engine-aware, transactionally consistent backup method such as `mysqldump --single-transaction` where applicable. Validate storage-engine behavior, schema, triggers/routines and required migrations. Restore into a **separately provisioned staging database**; never replace the production database as a test.

## 4. Automated restore evidence

### Disposable database integration CI

The [Database Integration workflow](../.github/workflows/integration-databases.yml) exercises PostgreSQL 17 `pg_dump -Fc`/`pg_restore` and MySQL 8.4 `mysqldump --single-transaction`/restore using **new disposable databases**.

It seeds multiple published versions, chunk-boundary SHA metadata, delete markers/version removal, tags, MTProto send intent/receipt records, queued jobs and independently encrypted **test** S3 credentials. Fresh SQL connections check exact heads/generations, chunk identities, retention semantics, job state and decryptability.

**Limit:** this checks actual SQL backup and restore machinery, **not** original Telegram media or a production secret-recovery drill.

### Dedicated USER-MTProto recovery CI

`scripts/rehearse_telegram_dr.py` verifies original-media readback through an authorized **read-only USER MTProto** session (`TELEDRIVE_STORAGE_MTPROTO_API_ID` / `TELEDRIVE_STORAGE_MTPROTO_API_HASH` / `TELEDRIVE_STORAGE_MTPROTO_SESSION_FILE`, plus `--target` for the original channel). Old `api-bot` chunks in the **same** authorized channel remain readable by message ID without any Bot token. The drill checks archive identity, published SQL metadata, original Telegram bytes, object SHA/length and cross-chunk ranges from a sealed snapshot; it is a single named-object check, **not** proof of a complete independent payload backup or full archive recovery.

The [User MTProto E2E workflow](../.github/workflows/live-telegram-user.yml) exercises a real Telethon user session with checksum and range readback, and the [Multiworker PostgreSQL E2E workflow](../.github/workflows/live-telegram-multiworker.yml) runs three independent SQL workers against a real test channel. Cleanup is scoped to messages created by each test run.

**Limit:** these tests exercise the live USER writer and isolated recovery, not the owner's original production channel or media.

## 5. Production disaster-recovery acceptance

For actual owner-controlled original Telegram data, follow [Project Status: original-media DR](IMPLEMENTATION_STATUS.md#32-recovery-of-original-telegram-media). The evidence must link a sealed SQL archive and its SHA-256 to the **original** channel/account and verify real Telegram payloads and byte ranges without deleting or reuploading them.

A successful SQL restore does **not** mean production recovery passed. Owner credentials, independently backed-up encryption keys, source-root mounts, versioned GET/HEAD, MTProto send receipts, credential revocation and queued-job behavior must also be checked.

## 6. Failure modes and prohibited actions

- A process crash after Telegram accepted media can leave an unindexed message. Do **not** delete such media or assume a retry is safe when acknowledgment is ambiguous.
- Concurrent processes must never write using the **same** Telethon session file.
- Retained historical versions, aliases, multipart parts and failed upload intents can still reference Telegram messages.
- Physical Telegram deletion remains disabled until independently proven retention-aware GC, verified media backups and explicit owner authorization exist.
- Only promote a staging restore after independent owner acceptance and controlled change management; this runbook itself performs **no production cutover**.
