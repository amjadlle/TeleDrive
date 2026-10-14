# P2 · recovery, garbage collection and capacity

This is the **single current P2 engineering backlog**. Older planning notes
in Git history are snapshots, not extra release-blocking checklists.
Source of truth: [Implementation Status](IMPLEMENTATION_STATUS.md) and
[TODO](../TODO.md).

## 1. Physical Telegram GC — blocked from production execution

**Implemented:** `StorageGCInventory` inspects FAILED-object retention,
HEAD/alias/published-version/multipart and Bot ACK references. The
`RecoveryManager.cleanup` destructive code path now rechecks installed
HEAD, immutable published history, logical removed versions, copy aliases,
multipart parts, unresolved Bot intents and durable receipts both before
and during remote deletion. The ownership-aware Telethon adapter only removes
messages it can independently probe.

**Not complete:** a version-wide, transactionally fenced mark/sweep planner;
immutable audit/owner authorization; backup of every candidate's original
bytes and a tested recovery method; host-wide writers pausing; a
cross-worker deletion epoch and atomic recheck after remote requests.
Telegram message deletion is intrinsically irreversible with original IDs,
so **do not claim rollback** from ordinary SQL restore.
Physical GC stays OFF in WebUI/API/default scheduling. Do not enable it to
turn a test green.

Next gate: dry-run signed plan, independent complete Telegram ownership scan,
cross-version reference proof, sealed replacement media backup, concurrency
barrier, explicit two-person authorization and recovery rehearsals.
Aborted/unknown-ACK media must remain fenced.

## 2. Original Telegram metadata rebuild — constrained recovery supported

`teledrive.storage.rebuild.restore_isolated_catalog` and
`scripts/rebuild_from_telegram.py` verify all original messages, validate
their TeleDrive UUID/ordinal/checksum-prefix markers (authorized Telethon
session), stream/download each chunk, check **independent full chunk SHA-256**
and whole-object SHA/size, then publish that object into an **empty NEW SQLite
database**. The existing production SQL catalog is never overwritten.

The JSON plan must have exact keys:

```json
{
  "object_id": "00000000-0000-4000-8000-000000000001",
  "bucket": "example-backup",
  "key": "original/archive.bin",
  "sha256": "64_lowercase_hex_full_object_digest",
  "size": 1048576,
  "chunks": [
    {
      "ordinal": 0,
      "account": "original-user",
      "chat_id": -1001234567890,
      "message_id": 123,
      "size": 1048576,
      "sha256": "64_lowercase_hex_chunk_digest"
    }
  ]
}
```

*Replace digest placeholders with real 64-character hex strings.*
Protect this plan as sensitive metadata, verify bucket/key/account mapping
from an **independent authenticated record**, and provide the original
Telethon user `TELEDRIVE_REBUILD_API_ID`, `TELEDRIVE_REBUILD_API_HASH`,
`TELEDRIVE_REBUILD_SESSION` in a protected environment.

```bash
python scripts/rebuild_from_telegram.py \
  --plan /secure/verified-plan.json \
  --new-database /secure/isolated-rebuilt.db \
  --account original-user --target <ORIGINAL_TELEGRAM_CHANNEL>
```

The CLI fails closed on wrong identity, old/reused database path, missing
message, corrupt bytes or checksum mismatches. It never sends or deletes
Telegram messages. **This cannot magically find original bucket/key,
reconstruct lost publication history or recover media with no trusted message
mapping.** Bot-only unknown-ACK reconciliation likewise requires trusted
independent file_id proof. Broad automatic reconstruction of all unknown
historical objects remains open.

## 3. Long-duration and physical host capacity

The multi-process PostgreSQL CI proves independent worker processes, not
independent physical production machines. `scripts/monitor_p1_soak.py` runs
a real >=1-hour read-only sample of 2-8 HTTPS nodes, requiring actual 429
counter changes, consistent SQL metrics and queue drain. No intentional
Telegram flood is permitted.

Missing: a deployed physical-host acceptance environment, longer-duration
capacity/performance percentiles, real original media restore during a fault,
and independent machine-ID attestation. Follow
[Production Acceptance](P1_PRODUCTION_ACCEPTANCE.md) and retain signed
evidence outside GitHub.

## 4. Other P2 S3 compatibility

Version-aware physical GC, lifecycle, bucket policies, server-side encryption,
unimplemented SDK corner cases and broader client testing remain outside
the tested v1.0.7 P0/P1 release scope.
\nDevelopment-only endurance workflow: [06 · Endurance | Offline 60m Capacity Soak](../.github/workflows/p2-capacity-soak.yml) runs an isolated SQLite/real storage-engine, synthetic Telegram 60–110 minute loop with SHA-verified recovery, durable manifest accumulation and roundtrip p50/p95. It is manual so ordinary commits do not consume an hour of CI quota. It does **not** replace physical host, natural 429 or original Telegram acceptance.\n
### Atomic multi-object rebuild (unreleased development)

The recovery plan may also be a JSON object of the form `{"objects": [<complete original object plans>]}`, with 1–128 independently verified objects, up to 4,096 chunks and 1 GiB total content. Every bucket/key, object UUID and Telegram message identity must be unique. All remote objects and their byte-level proofs are validated **before** the staging SQLite database is created. The database is committed and integrity-checked privately; an exclusive filesystem link publishes the complete catalog atomically without overwriting existing SQL. Any failed object leaves no published output database. Original published version history is **not** inferred from Telegram captions.
