# P1 production acceptance: executable, gated operator runbook

This document separates **implemented and CI-tested safeguards** from
**production evidence that requires an authorized operator**. Do not interpret
a green CI run, a staging SQL copy, or a successful dedicated Telegram test
channel as proof that a production channel/database was exercised.

## 1. Uncertain Bot ACK: verify original channel post, then record receipt

A network timeout can mean that Telegram published `sendDocument` but the
response was lost. TeleDrive commits a pre-send SQL intent before I/O. Until
the intent is reconciled, it **must not automatically retransmit** that part.

Use the authenticated admin
`GET /api/v1/admin/storage/bot-unresolved` inventory to identify the exact
`account`, `chat_id`, `object_id`, `ordinal`, SHA-256 and size. The ordinary
storage health endpoint exposes a redacted critical count only.

A production operator with an independently retained original **Bot file ID**
and a separately authenticated **Telegram user** who can read the original
channel may run:

```bash
# Secrets only through a protected operator shell / secret manager.
export TELEDRIVE_BOT_TOKEN='...'
export TELEDRIVE_RECONCILE_API_ID='...'
export TELEDRIVE_RECONCILE_API_HASH='...'
export TELEDRIVE_RECONCILE_SESSION='...'
export TELEDRIVE_RECONCILE_FILE_ID='...'

# Read-only; checks the exact channel message ID, signed Bot upload caption,
# Telegram document size, two independently downloaded byte streams and SHA.
python scripts/reconcile_bot_receipt.py \
  --database /secure/staging-restore.db --account bot-production \
  --chat-id -1001234567890 --object-id REPLACE-OBJECT-UUID \
  --ordinal 0 --message-id 1234

# Only AFTER the report is independently reviewed and owner-authorized:
python scripts/reconcile_bot_receipt.py \
  --database /secure/operator-approved.db --account bot-production \
  --chat-id -1001234567890 --object-id REPLACE-OBJECT-UUID \
  --ordinal 0 --message-id 1234 --apply
```

The CLI never sends new Telegram messages or deletes remote media. It cannot
invent a missing Bot file ID. It requires original Bot proof from a trusted
receipt/log or an operator's retained evidence; Bot API cannot list arbitrary
historical channel messages. The post's caption binds the original object ID,
ordinal and checksum prefix, independently of the bytes. No network read
alone warrants automatic release of an unresolved fence.

Acceptance: exact SQL intent present before proof, dry-run does not change it;
wrong channel, message, caption, byte hash, file size and Bot file ID fail
closed; `--apply` commits a receipt only for the exact intent; subsequent
journal recovery downloads/validates Telegram bytes again.

## 2. Multiworker Telegram and real 429 acceptance

`BotFileIndex.reserve_send_slot` now takes a durable shared SQL write lock
and reserves message start slots for a given `(account,chat_id)`.
Explicit HTTP 429 backoff is propagated to other SQL-sharing workers.
Retries remain bounded; a lost ACK must never be treated as a 429.
All hosts **must have synchronized UTC clocks** and use the *same* durable
PostgreSQL/MySQL database. Account/channel identities must match.

Deterministic tests cover separate OS processes with SQLite and six parallel
real PostgreSQL 17 / MySQL 8.4 connections, independent channels and the
shared 429 cooldown. An opt-in GitHub workflow
`.github/workflows/telegram-multiworker-e2e.yml` opens three independent
OS worker processes against a real PostgreSQL service and uploads/checks
multiple original Telegram documents in the dedicated test channel.
Only the test's own message IDs are deleted after validation.

This validates TCP/database separation and independent processes, **not three
physical production hosts or a prolonged production 429 soak**. To sign off
production, deploy at least two truly separate application hosts with one
shared SQL database, require operator-set rates, and record at least a
one-hour soak including naturally occurring 429 responses, recovery,
checksum/range checks, backlog drain and zero unexpected duplications.
Do not flood Telegram deliberately to provoke restrictions.

## 3. Disaster recovery: SQL and Telegram must both work

SQLite online snapshot:

```bash
python scripts/backup_metadata.py --database /secure/live.db \
  --output /secure/backups/teledrive-UNIQUE.db
python scripts/verify_metadata_backup.py \
  --database /secure/backups/teledrive-UNIQUE.db \
  --sha256 EXPECTED_BACKUP_SHA256
# TELEDRIVE_BOT_TOKEN must be from the SAME ORIGINAL channel and account.
python scripts/rehearse_telegram_dr.py \
  --snapshot /secure/backups/teledrive-UNIQUE.db \
  --sha256 EXPECTED_BACKUP_SHA256 \
  --bucket production-bucket --key verified-sample.bin \
  --account bot-production --chat-id -1001234567890
```

The staging DR command copies the sealed archive to disposable scratch
storage; the original archive is never repaired or overwritten. It validates
the SQL image, original bucket/head, full Telegram checksum, size, and
half-open Range bytes from real Bot `getFile` downloads. All negative
tests must fail closed. This does not write Telegram data, delete messages,
modify the original SQL image, or publish new object heads.

For PostgreSQL/MySQL use **native** pg_dump/pg_restore and
mysqldump/isolated restore, as exercised by `database-ci.yml`. Encrypted
credential material and exact Telegram account/channel ownership must be
available to the staging recovery operator; merely restoring SQL without
the original matching bot/session credentials does **not** prove DR.

## 4. Legacy cutover: compare every source, then accept

Do not run the old legacy auto uploader and durable cutover simultaneously.
Quiesce incoming source writes. Disable old `app.auto_run`. Ensure old
uploader lease has expired/released, the legacy folder is inside the approved
`TELEDRIVE_JOB_SOURCE_ROOT`, and all old jobs have been queued or completed.
The existing fenced cutover mode requires explicit
`TELEDRIVE_JOB_LEGACY_AUTO_BUCKET` and `TELEDRIVE_JOB_AUTO_RUN=1`.

Run the **read-only** final acceptance after the queue has drained:

```bash
export TELEDRIVE_BOT_TOKEN='...'
python scripts/verify_legacy_cutover.py \
  --database /secure/production-or-staging.db \
  --source-root /approved/uploads/legacy \
  --allowed-root /approved/uploads \
  --scratch-dir /secure/scratch --bucket target-bucket \
  --prefix archived --account bot-production \
  --chat-id -1001234567890 \
  --max-files 1000 --max-bytes 67108864
```

Exit code **0** requires at least one eligible source and a complete,
untruncated scan: every eligible source revision has its exact immutable
queued job in state `done`, the job's published HEAD identity and source
SHA match, and all Telegram bytes independently read back to the same SHA.
Exit code **2** denotes a failed/incomplete acceptance; exit code **1**
indicates invalid config or a verification error. Excluded file types are
reported but not counted as migrated. Repeat with a sufficiently high bounded
size budget; never consider a truncated scan accepted.

If a source changes, a job lacks a HEAD or Telegram content fails its SHA
check, **do not switch production reads over**. Retain the old source files,
durable journals and original channel posts. Neither the verifier nor the
cutover scheduler automatically deletes the legacy tree.

## Evidence/status boundary

- Dedicated test-channel, sandbox database and per-run cleanup: CI owns them.
- Production secrets, access to historical channel media, physical multi-host
  deployment, one-hour observation, target database restore and final read
  cutover: authorized operator must perform and record the evidence.
- The implementation can be prepared/tested without operator intervention;
  the **production acceptance cannot truthfully be marked complete until
  those external steps actually run**.

## 5. Private Telegram E2E channel ID: mandatory GitHub Secret migration

**Do not use repository Actions variables for numeric Telegram peer IDs.**
Actions variables are not automatically log-masked. Both
`.github/workflows/telegram-bot-e2e.yml` and
`.github/workflows/telegram-multiworker-e2e.yml` now read
`secrets.TELEDRIVE_E2E_BOT_CHAT_ID` exclusively, and install an additional
`::add-mask::` before checkout or testing. The optional Telethon numeric
target also reads `secrets.TELEDRIVE_E2E_TARGET`, not an unmasked variable.
The private channel **name** and Telethon StringSession were already Secrets.

To migrate a channel ID **without printing it on the command line, in logs,
in this document or in Git history**, use a terminal with an authenticated
`gh` CLI and run:

```bash
python scripts/setup_telegram_e2e.py --repo babico/TeleDrive --secure-bot-chat-id
```

The command reads the existing variable with `gh variable get` into
a captured subprocess buffer (never printing it), writes the value to a
Secret over `gh secret set` stdin and **verifies that the old variable
was deleted**. If the old variable is unavailable, it prompts through
hidden input instead. Any read/deletion permission error aborts with a
nonzero exit code; resolve it from **Settings → Secrets and variables →
Actions** before restarting live tests. Neither test logs nor committed
workflow source should display the ID.

Until the secret exists, live Bot workflows now **fail closed during the
first configuration step**. This is expected and safer than falling back
to the unmasked variable; do not bypass it merely to make CI green.
Previous workflow logs cannot be retroactively masked: review existing
run retention and access controls separately. Do not paste the numeric
channel ID into an issue, PR, workflow YAML or shell command arguments.

To minimize exposed diagnostics, tests also verify that every Bot workflow
loads the target from `secrets`, installs a mask *before* checkout, and
never uses `vars.TELEDRIVE_E2E_BOT_CHAT_ID`.
