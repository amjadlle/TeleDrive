# Production acceptance · operator runbook

**Status: NOT SIGNED OFF.** TeleDrive v1.0.7 is a completed P0/P1 *development*
milestone, **not** proof that a physical production deployment exists.
All nine required development workflows passed on the v1.0.7 release source
`747747652cd1ca36878453d49daf627d8c0a60ad`.
See [CI catalog](CI_WORKFLOWS.md), [release](https://github.com/babico/TeleDrive/releases/tag/v1.0.7)
and [canonical status](IMPLEMENTATION_STATUS.md).

Only the owner of the real hosts, Telegram accounts and legacy data may perform
or approve the following steps. No production credentials belong in GitHub
repository files or ordinary CI.

## A. Verify actual independent hosts and Telegram load

1. Deploy at least two physically distinct application hosts on a shared
   PostgreSQL/MySQL database. Record **independently observed** machine
   identities (SHA-256 fingerprints), shared database identity and host labels.
   Two URLs on one host do not prove independence.
2. Enable safe, rate-limited uploads and independently watch for **naturally
   occurring** Telegram 429 responses. Never intentionally flood Telegram.
3. Configure protected environment values:
   `TELEDRIVE_P1_HEALTH_HOSTS` (JSON array of distinct HTTPS origins,
   `label`/`url`), `TELEDRIVE_P1_BASIC_USERNAME`, and
   `TELEDRIVE_P1_BASIC_PASSWORD`. Run from a trusted operator host:

   ```bash
   python scripts/monitor_p1_soak.py \
     --output /secure/acceptance/soak.json \
     --duration-seconds 3600 --interval-seconds 60
   ```

   The report must show `telemetry_gate_passed=true`, a full hour, nonzero
   **actual** explicit 429 deltas for every observed SQL-sharing host,
   matching final counters, no unresolved ACKs and drained job queues.
   This is **telemetry evidence only**; physical independence is attested separately.

## B. Original Telegram disaster recovery

Back up and seal SQL metadata plus original Telegram secrets **separately**.
Restore into an isolated SQL environment, never the live database.

For SQLite run `scripts/backup_metadata.py` and
`scripts/verify_metadata_backup.py` as in
[Backup & Restore](BACKUP_RESTORE.md). With the **original** production Bot
account/token run the non-mutating actual-channel byte verifier:

```bash
python scripts/rehearse_telegram_dr.py \
  --snapshot /secure/backups/sealed.db --sha256 <ARCHIVE_SHA256> \
  --bucket <BUCKET> --key <ORIGINAL_KEY> \
  --account <ORIGINAL_ACCOUNT> --chat-id <ORIGINAL_CHAT_ID>
```

The script verifies source archive SHA, independent restored SQL, full original
Telegram content SHA/size and a Range window. It never uploads/deletes media.
For native PostgreSQL/MySQL repeat the isolated native restore and original
Telegram readback using the matching account; a successful SQL-only restore
does not certify media recovery.

## C. Legacy cutover — only if a real legacy source exists

If there is no legacy installation, do **not** fabricate test data or a cutover.
The owner may explicitly attest `no_legacy_source_attested=true` in the
signed production evidence bundle and use `{"not_applicable": true}`.
Otherwise stop the old uploader, quiesce the source, hold the external
cutover lease, configure a restricted source root, and run:

```bash
python scripts/verify_legacy_cutover.py \
  --database /secure/isolated.db \
  --source-root /approved/legacy \
  --allowed-root /approved \
  --scratch-dir /secure/scratch \
  --bucket <BUCKET> --account <ORIGINAL_ACCOUNT> \
  --chat-id <ORIGINAL_CHAT_ID>
```

Require `verified=true`, no truncated scan, no queued/inconsistent files,
no mutations and every source revision verified against published HEAD and
original Telegram bytes. Only then may the operator perform authorized cutover.

## D. Dual-attested evidence review

`scripts/verify_production_bundle.py` accepts a JSON bundle with:
`release_sha`, `topology`, `soak`, `dr`, `cutover`,
`original_telegram_data_attested`, `no_legacy_source_attested` (only when
applicable) and `approvals`. The `topology.hosts` list contains two to eight
entries with `label`, independently observed
`machine_id_sha256` and the **same** `shared_database_sha256`, plus
`independent_hosts_owner_attested=true`.

The `soak` / `dr` / `cutover` entries are the actual JSON outputs from the
commands above, not example fixtures. Two independent people sign the
canonical JSON bytes of the bundle **excluding** `approvals`, using separate
Ed25519 private keys; the `approvals.owner` and `approvals.operator` fields
contain base64 signatures. Public keys are separate trusted 32-byte files.

```bash
python scripts/verify_production_bundle.py \
  --bundle /secure/acceptance/production-bundle.json \
  --release-sha <REVIEWED_40_CHARACTER_COMMIT_SHA> \
  --owner-public-key /secure/keys/owner.pub \
  --operator-public-key /secure/keys/operator.pub
```

Invalid evidence, altered reports, incomplete 429s, missing independent
signatures, wrong source SHA or unapproved legacy cutover are rejected.
A **passing verifier is not a deployment signoff**: it still prints
`production_acceptance_signed_off=false` and
`physical_telegram_deletion_enabled=false`. The owner must independently
authorize the deployment after reviewing the raw server identities, Telegram
origin, encrypted backups, test traffic and change-control record.

## E. Telegram cleanup and recovery limitations

Physical deletion is still **disabled in the HTTP admin API**, even after
an accepted production evidence bundle. Telegram physical deletes cannot
restore their original message IDs; there is no unconditional rollback.
See [P2 Recovery & GC](P2_RECOVERY_AND_GC.md) for the implemented SQL
reference gate, isolated Telegram rebuild tool and remaining approval blockers.
