# Backup and Restore Operations

> **Audience:** deployment operators and release maintainers.
> **Purpose:** create and verify protected SQL metadata backups and rehearse recovery **without modifying a live installation**.
> **Related:** [Project Status and production sign-off](IMPLEMENTATION_STATUS.md#3-production-acceptance-not-yet-signed-off) · [Security Policy](SECURITY.md).

## 1. What a backup contains

TeleDrive stores **payload bytes in Telegram** and **object references/versions, job state, Bot file IDs, and encrypted S3 credential metadata in SQL**. An SQL backup is **not** an independent copy of the Telegram objects.

Protect these as separate, coordinated recovery inputs:

| Asset | Why it matters | Handling |
| --- | --- | --- |
| Metadata database | Object heads, history, chunk identities, durable jobs | Encrypted backup; recorded independent SHA-256 |
| Telegram USER StringSession / API credentials | Required for current REST/S3/local-job access and same-channel message history | Protected `0600` private file/secret manager, independent of SQL archive |
| Historical Bot credentials (if any) | Old-channel recovery/testing only, **not** current unified writes | Separately protected migration archive |
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
3. Verify schema, SQL HEAD pointers, version history, references, upload intents, Bot file IDs and credential decryptability with the staged secrets.
4. Reconnect only to approved **test/original owner-authorized** Telegram media, verify full SHA-256 and at least one cross-chunk range.
5. Record the results privately. **Never overwrite a running production database.**

## 3. PostgreSQL and MySQL/MariaDB

### PostgreSQL

Use a consistent native logical backup, for example `pg_dump -Fc`, under a dedicated and appropriately scoped backup role. Retrieve credentials through a secret store; **do not put a password in a shell command or committed script**.

Rehearse a native `pg_restore` into a **new, empty, isolated** database. Compare schema, object heads and generations, published/deleted versions, chunk references and SHA-256 hashes, Bot receipts, durable jobs and S3 credential decryptability.

### MySQL / MariaDB

Choose an engine-aware, transactionally consistent backup method such as `mysqldump --single-transaction` where applicable. Validate storage-engine behavior, schema, triggers/routines and required migrations. Restore into a **separately provisioned staging database**; never replace the production database as a test.

## 4. Automated restore evidence

### Disposable database integration CI

The [Database Integration workflow](../.github/workflows/integration-databases.yml) exercises PostgreSQL 17 `pg_dump -Fc`/`pg_restore` and MySQL 8.4 `mysqldump --single-transaction`/restore using **new disposable databases**.

It seeds multiple published versions, chunk-boundary SHA metadata, delete markers/version removal, tags, Bot upload receipt records, queued jobs and independently encrypted **test** S3 credentials. Fresh SQL connections check exact heads/generations, chunk identities, retention semantics, job state and decryptability.

**Limit:** this checks actual SQL backup and restore machinery, **not** original Telegram media or a production secret-recovery drill.

### Dedicated historical Bot-compatibility recovery CI

`scripts/rehearse_telegram_dr.py` remains **Bot API-only** (`TELEDRIVE_BOT_TOKEN`) for a historical/isolated original Bot source. It is **not** evidence for a production Telethon USER restore; that independent verifier and owner-authorized test remain open.

The [Bot API E2E workflow](../.github/workflows/live-telegram-bot.yml) takes a disposable SQLite snapshot from a dedicated **test** Bot/channel transfer, verifies its SHA-256, opens it independently, and retrieves real Bot message bytes using the persisted file IDs. Full checksums and ranges are rechecked. Cleanup is scoped to messages created by that test run.

**Limit:** this test exercises the historical Bot adapter in isolation, not the live unified writer. It cannot prove that the owner's original Bot token, channel history or production media remain recoverable.

## 5. Production disaster-recovery acceptance

For actual owner-controlled original Telegram data, follow [Project Status: original-media DR](IMPLEMENTATION_STATUS.md#32-recovery-of-original-telegram-media). The evidence must link a sealed SQL archive and its SHA-256 to the **original** channel/account and verify real Telegram payloads and byte ranges without deleting or reuploading them.

A successful SQL restore does **not** mean production recovery passed. Owner credentials, independently backed-up encryption keys, source-root mounts, versioned GET/HEAD, Bot ACK records, credential revocation and queued-job behavior must also be checked.

## 6. Failure modes and prohibited actions

- A process crash after Telegram accepted media can leave an unindexed message. Do **not** delete such media or assume a retry is safe when acknowledgment is ambiguous.
- Concurrent processes must never write using the **same** Telethon session file.
- Retained historical versions, aliases, multipart parts and failed upload intents can still reference Telegram messages.
- Physical Telegram deletion remains disabled until independently proven retention-aware GC, verified media backups and explicit owner authorization exist.
- Only promote a staging restore after independent owner acceptance and controlled change management; this runbook itself performs **no production cutover**.
