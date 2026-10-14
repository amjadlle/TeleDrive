# P1 production acceptance: future deployment-only operator runbook

> **NOT required for P1 development-stage completion.** TeleDrive has
> not yet been deployed to production. Do not invent a legacy migration,
> live host, production backup, or one-hour natural Telegram 429 result.
> Run the fully automated **Docker DEV** campaign first:
> [P1 development acceptance](P1_DEVELOPMENT_ACCEPTANCE.md),
> `bash scripts/run_p1_dev_acceptance.sh`. This document becomes relevant
> only when a real production environment and owner-controlled data exist.


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
Secret over `gh secret set` stdin, and **re-lists variable names after
deleting the old variable** to verify its absence. Only when a separate
names-only listing confirms that no legacy variable exists does it use
hidden input instead. An unreadable existing variable, failed list,
failed deletion, or variable re-creation aborts with a nonzero exit code; resolve it from **Settings → Secrets and variables →
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


## 2026-10-14 dedicated P1 evidence ledger

- [x] Multiworker **test-channel** PostgreSQL + three independent worker
  processes: [run 38537359663](https://github.com/babico/TeleDrive/actions/runs/38537359663)
  on `243d691` succeeded. No claim of three physical production hosts.
- [x] Standard seven-workflow CI baseline: `fc582f4` passed including
  actual User and Bot Telegram roundtrip. Bot ran 20 tests; 5 opt-in
  cases were skipped and do **not** count as live acceptance.
- [x] Dedicated test-channel Bot 5/16/19 MiB, 12-object mixed-size
  soak, independent test-channel ACK proof and Telethon size matrix:
  Bot run 38537902913 (25 passed, none skipped) and Telethon run
  38537902676 (14 passed) at SHA `900ab491`. This proves staging/test
  channel coverage, **not** original production channel ownership.
- [ ] Two physically independent app hosts, shared durable SQL, at least
  one hour of approved load and naturally occurring 429 evidence. Do not
  deliberately flood Telegram to manufacture an HTTP 429.
- [ ] Production-representative restored SQL archive + original Telegram
  channel, original keys and authenticated independent full/Range readback.
- [ ] Authorized production legacy migration comparison and final read
  cutover approval; never auto-delete source files or Telegram messages.

These are separate acceptance receipts. No CI runner can replace
operator access to original production media or real deployment hosts.


## Verified dedicated-channel live test evidence (2026-10-14)

- Bot run 38537902913 at `900ab491`: 25 passed, zero skipped with
  multipart 5/16/19 MiB, mixed-size soak and cross-client ACK proof enabled.
- Telethon run 38537902676 at `900ab491`: 14 passed, zero skipped with
  large-size user-session stress enabled.
- 3-process real PostgreSQL + test Telegram Bot run 38537359663 at
  `243d691`: one live multiworker test passed; no production hosts.
- Seven standard workflows passed together on `900ab491`, providing a
  coherent code/CI signoff distinct from external operator acceptance.

Still not production-accepted: original channel Bot lost-ACK provenance,
a real one-hour physically separate host run with naturally occurring
429s, original production DB/Telegram restore and authorized legacy read
cutover. Do not conflate isolated test-channel evidence with operator
production signoff or authorize physical GC from this report.


## Late source-mutation and symlink-swap protection (2026-10-14)

The read-only cutover verifier now binds every local SHA-256 to the exact
regular-file descriptor, checking device/inode/size/mtime **before open, on
the opened descriptor, after hashing, and after re-resolving the root**.
An attacker racing final-component symlinks cannot redirect verification to
an outside file. After all Telegram readbacks, it re-hashes every verified
source revision again: a file changing while a later Telegram object is
downloaded fails the report even when its original mtime and size are
restored. The verifier still NEVER mutates the source, SQL, or Telegram.
Physical quiescence and a separately held cutover lease remain mandatory;
repeat verification immediately before authorized read switchover.


## Durable, privacy-preserving actual Bot 429 evidence counter

The shared Bot SQL cooldown transaction now also increments
`storage_bot_429_observations.observations` per Bot account/channel
when and only when the caller observes an explicit Telegram 429 with
validated `retry_after`. Operator-authenticated
`GET /api/v1/admin/storage/health` exposes the **aggregate**
`bot_explicit_429_total` without revealing Bot tokens, account aliases,
numeric Telegram peer IDs or file IDs. A new installation starts at 0;
in-place schema creation does not rewrite the preexisting cooldown table.
Compare counts before/after a real physically separated one-hour soak;
a stable counter does not prove that a natural 429 happened. Synthetic 429
unit tests and separate test-channel CI are never production soak receipts.

The counter update, shared cooldown and send-slot fence are one SQL
transaction; if journal persistence fails all three roll back and the
operator must not claim observed 429 evidence. A live worker still refuses
unsafe unknown-outcome retransmission; this metric is never a retry
authorization. Physical production soak remains operator-owned.


## 6. Non-mutating, two-host >=1h telemetry receipt

Use the implementation's `scripts/monitor_p1_soak.py` from an
**authorized operator shell** with two truly independent app hosts sharing
one SQL database. Both HTTP services must serve the identical
authenticated `/api/v1/admin/storage/health` endpoint over HTTPS.
This collector is strictly **read-only**: it performs no load-generation,
no Telegram uploads, no remote deletion and no SQL writes.

Configure endpoint URLs in a protected environment variable, not shell
arguments or a public Actions variable. Example host labels are arbitrary;
actual production host independence needs operator/network evidence.

```bash
export TELEDRIVE_P1_HEALTH_HOSTS='[{"label":"node-a","url":"https://HOST_A"},{"label":"node-b","url":"https://HOST_B"}]'
export TELEDRIVE_P1_BASIC_USERNAME='operator'
# Supply the password from your secret manager; do not put it in Git history.
export TELEDRIVE_P1_BASIC_PASSWORD='...'
python scripts/monitor_p1_soak.py \
  --duration-seconds 3600 --interval-seconds 60 \
  --output /secure/operator-evidence/p1-soak-UNIQUE.json
```

The script refuses HTTP, query-parameter credentials, redirects, duplicate
hostnames, fewer than two probes, missing fields, truncated health samples
and a monitoring interval gap. Its non-overwriting receipt uses file mode
0600 and contains host **labels**, aggregate 429 counter deltas, sample
count, drain/safety status and explicit
`production_acceptance_signed_off: false`; neither the API Basic
credentials nor host URLs are written to the receipt. Exit 0 means the
**telemetry gate** passed: >=1h observed, both endpoints agree on the
final shared SQL counter, the definitive 429 count increased during
observation, and no pending/reconciliation/backlog alert remains on
either endpoint. Exit 2 means observations were inconclusive; exit 1
means probe/configuration error. Do not artificially flood Telegram
to manufacture an HTTP 429.

This is deliberately **not** a complete production signoff. Separately
attach operator-verified physical host/SQL connection evidence,
permitted load and checksum/range results, original-media DR report,
and signed legacy cutover approval to the change record. A passing
CI or sandbox collector cannot certify the original production channel.


## Current P1 staging test gate and owner-held production gates

The code commit `73135b0` includes native PostgreSQL/MySQL restore
checks for the **synthetic** Bot 429 observation record and a production
soak telemetry collector. A one-shot `[p1-live-multiworker]` validation
on the next `main` commit deliberately exercises three SQL worker
processes and actual dedicated-channel Telegram messages; it must be
recorded by run ID and final result, not assumed to pass merely because
GitHub started it.

Still open: authorization and real evidence from physically separate
production hosts, original SQL archive with the original Telegram
account/channel, and explicit legacy cutover approval. An operator must
independently verify the source system and supply those receipts before
changing any production read routing.


## 7. One-command P1 operator acceptance wizard

On an operator-owned interactive terminal (not stdin/heredoc), from the
latest TeleDrive main checkout with the virtual environment active:

`python scripts/p1_acceptance_wizard.py`

The wizard guides six separate tasks: latest successful CI E2E evidence,
Telegram user StringSession creation/validation, genuinely independent HTTPS
host one-hour soak, consistent SQLite or PostgreSQL/MySQL isolated backup and
actual original-channel Bot readback, legacy cutover SHA/HEAD verification, and
OPTIONAL lost-ACK independent user+Bot verification. For the last operation
it is explicitly **dry-run only** and never calls `--apply`.

The wizard writes unique 0600 evidence JSON under
`~/teledrive-p1-evidence/` and stores newly generated Telethon
StringSessions (without printing them) under
`~/.config/teledrive/operator.session`. Do not send Telegram
sessions, API hashes, Bot tokens, DB URLs, host URLs, Bot file IDs, backups
or original private chat/message IDs to an assistant, issue or public CI.

Missing independent hosts or production credentials are **BLOCKED**, not
faked as passing. A passing telemetry receipt never proves host physical
independence, signed cutover, or authorization. Production routing is never
switched automatically; physical remote deletion stays disabled.
