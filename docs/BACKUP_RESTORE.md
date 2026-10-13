# TeleDrive metadata backup and recovery — operator runbook

Metadata databases contain private object locations, API key metadata and
encrypted S3 secrets, **not** a complete copy of the Telegram-hosted payload.
The S3 master key and Telegram sessions/tokens are independent secrets and
must be backed up separately in a secure secrets manager. Never commit
backups or credentials to the Git repository.

## SQLite

Run the following on the TeleDrive host, specifying a destination on
encrypted, access-controlled storage:

    python scripts/backup_metadata.py --database /data/teledrive.db --output /secure-backups/teledrive-2026-10-13.db

This uses the SQLite online backup API, verifies the resulting snapshot
with PRAGMA integrity_check, restricts the file to mode 0600 and refuses
to overwrite existing files. Record the printed SHA-256 in a separate
operator log. Run a test restore in a **separate test environment** before
relying on any recovery plan.

To test restoration, stop the disposable test TeleDrive instance, place a
copy of the snapshot at that instance's configured database path with
owner-only permissions and run its schema/integrity checks before enabling
any upload workers. **Do not overwrite a live production database.**
After a recovery, verify Telegram Bot file IDs, account/session access,
source-root configuration, object head pointers and version history.

## PostgreSQL

Use a consistent native logical backup (e.g. pg_dump -Fc) with a
dedicated read-only backup role and your existing credential manager.
Do not put database passwords on the command line. Test pg_restore into
a separate empty database and verify schema, head/version counts and
chunk reference constraints.

## MySQL / MariaDB-compatible deployments

Use an engine-aware transactionally consistent backup with
--single-transaction where applicable (e.g. mysqldump). Validate
storage engine, triggers/routines and schema migration compatibility.
Restore to a separately provisioned test database before promotion.

## Disaster-recovery constraints

- Database snapshots must be coordinated with encrypted S3 master-key
  backups, Telegram account sessions and Bot credentials; without these,
  metadata alone cannot recover all objects.
- Do not run multiple writers against one shared Telegram session file.
- Telegram message deletion/physical GC remains disabled until an audited
  retention-aware cleanup mechanism is approved.
- Hard-killed uploads may leave Telegram messages not yet in SQL. Restore
  must not delete unindexed media; reconcile using available history
  evidence and fail closed if ownership is uncertain.
- Before production cutover, validate versioned GET/HEAD, ranged checksum
  downloads, S3 credential revocation, real Telegram E2E, and a
  representative subset of durable queued jobs.
- A successful backup command is **not** proof that live restoration or
  full disaster recovery has been rehearsed. Track drills separately.

## Read-only disaster-recovery snapshot check

Verify the SQLite snapshot in a separate environment, using the independent
SHA-256 reported by the backup command:

    python scripts/verify_metadata_backup.py --database /secure-backups/teledrive-2026-10-13.db --sha256 YOUR_BACKUP_SHA256

The verifier checks SQLite consistency, FK constraints, all current HEAD
key hashes and the published-version index. It only reads local metadata;
it does not contact Telegram, recover missing messages, rotate credentials
or turn on physical garbage collection. Test actual Telegram recovery
separately before production cutover.


## Live Bot API disposable restore rehearsal

The regular configured `Telegram Bot Storage E2E` CI workflow also exercises
an actual remote restore: it snapshots a dedicated test SQLite database,
verifies its SHA-256, opens the snapshot as a separate SQL connection,
reconstructs the published HEAD, retrieves the preserved Bot API file IDs and
reads back real Telegram channel media with exact SHA-256 and ranged integrity
checks. Cleanup is restricted to messages sent by that test invocation.

This does **not** restore lost Telegram account access or confirm that arbitrary
production media is still accessible. Repeat DR rehearsals with a dedicated
staging bot, a full secret-restore procedure, and native PostgreSQL/MySQL
snapshots before calling a production recovery plan ready.
