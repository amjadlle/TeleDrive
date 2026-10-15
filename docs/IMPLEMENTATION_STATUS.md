# Project Status, Acceptance, and Recovery Roadmap

> **Audience:** contributors, release maintainers, and deployment owners.
> **Scope:** supported features, development evidence, production sign-off, recovery limits, and remaining engineering work.
> **Source of test details:** [CI and Test Guide](CI_WORKFLOWS.md). **Source of executable backup procedures:** [Backup and Restore](BACKUP_RESTORE.md).
>
> **Do not treat a successful CI run as approval to operate on production data or physically delete Telegram messages.**

## 1. Current release and supported scope

| Area | State | Evidence and limitations |
| --- | --- | --- |
| P0 — basic upload and supported REST/S3 subset | **Development-validated** | Transfer, authentication, integrity and tested client subset; not complete AWS S3 parity |
| P1 — disposable Docker acceptance | **Development-validated** | Two Web processes, real PostgreSQL, 13 resilience and cleanup gates; Telegram I/O is synthetic |
| P1 — real test-channel transfers | **Development-validated** | Bot API, Telethon user, three-worker PostgreSQL integration; not the owner's original production media |
| P1 — physical production operation | **Not signed off** | Independent host identity, owner data recovery and authorized change control not established |
| P2 — advanced recovery, GC and compatibility | **In development** | Rebuild and offline verification components exist; production safeguards and further protocol work remain |

**Published release:** [v1.0.7](https://github.com/babico/TeleDrive/releases/tag/v1.0.7), source SHA `747747652cd1ca36878453d49daf627d8c0a60ad`. Published release contents are not automatically updated when new development commits reach `main`. Release notes and binaries belong on [GitHub Releases](https://github.com/babico/TeleDrive/releases), not in a separate per-version Markdown file.

**Verified newer development baseline:** `aa44bd67e01495ad590af4460350cab1da8c28ac`; all nine normal CI workflows completed successfully on that SHA. For any *later* source SHA, read its own [Actions runs](https://github.com/babico/TeleDrive/actions) before making a CI claim. A cancelled, pending or fully skipped required workflow is not evidence of successful live acceptance.

### P0: implementation boundaries

- **State and durability:** SQLAlchemy with SQLite/PostgreSQL/MySQL integration testing, immutable object/chunk manifests, SHA-256 verification, ranged reads, fencing and persistent job history.
- **API and credentials:** separately scoped REST Bearer credentials and S3 SigV4 credentials, signed request checks, conditional writes, restricted administrative operations and fail-closed unsupported inputs.
- **Supported S3 subset:** object/bucket operations, a bounded multipart implementation, conditional operations, selected versioning (including safe always-Enabled controls), fixed all-private Public Access Block, tags, copies, CORS and presigned requests. Tested clients include boto3, AWS CLI and rclone.
- **Not complete S3:** arbitrary AWS lifecycle and bucket policy behavior, SSE, every checksum mode, full delete-marker semantics and untested client combinations are not warranted.
- **Telegram transports:** dedicated test-channel Bot API and user MTProto scenarios plus a shared PostgreSQL multiworker scenario. Credentials and private test channel are isolated from production.
- **Backups:** native PostgreSQL/MySQL backups and separate SQL restore rehearsals, plus disposable Docker fault injection. These exercises do not certify recovery of the owner's original Telegram account or channel.

### P1: development acceptance

The development acceptance criteria consist of the 13 Docker fault-injection gates, four evidence stages and scoped cleanup, plus the distinct real test-channel Bot/User/Multiworker workflows. Run the documented [fault-injection command and checklist](CI_WORKFLOWS.md#3-fault-injection-acceptance-gates); do not maintain a second copy of the 13 gates here.

A production installation, original legacy files, signed operator approval, an hour of real production rate limits, and restoration of original production media are **not** prerequisites for declaring disposable *development* acceptance. They are separate owner/operator responsibilities.

## 2. Architecture and unified storage contract

### A single service deployment

The default [`docker-compose.yml`](../docker-compose.yml) creates **one `teledrive` container** and one Python process with two local listeners:

| Interface | Local endpoint | Auth model | Storage |
| --- | --- | --- | --- |
| WebUI and REST v1 | `http://127.0.0.1:8080` | Basic admin + scoped REST Bearer tokens | Shared SQL catalog and Telegram object engine |
| S3-compatible API | `http://127.0.0.1:9000` | Scoped S3 credentials with SigV4 | **Same** SQL catalog and Telegram object engine |
| Local-folder durable jobs | Admin job API on 8080 | Basic admin + CSRF | **Same** catalog; files under allowed read-only mount are explicitly queued |

**Shared transport selection:** `TELEDRIVE_STORAGE_TRANSPORT=telethon` uses a preauthorized real user StringSession and a dedicated private broadcast channel; `bot` retains current Bot upload behavior. The web application's single ASGI lifespan holds the user session and is shared with the S3 listener. Both paths retain the same SQL heads; a switch affects only newly uploaded physical chunks, not historical references. Telethon channel history provides candidate markers but not independent key/whole-hash proof. MTProto unknown-ACK send intents and receipts are durable SQL GC blockers. Do not use Saved Messages.

An S3 request and a REST request can address the same bucket/key and observe the same published object and generation. A local file is **not** automatically imported merely by copying it into `/uploads`. Create an authenticated job for that path; Compose enables `TELEDRIVE_JOB_AUTO_RUN=1` to process explicitly queued jobs. This is distinct from the original user-account Telethon folder scanner/batch uploader. Explicitly opting in to `TELEDRIVE_JOB_LEGACY_AUTO_BUCKET` now exposes the existing fenced legacy-to-durable-job cutover on the default single-container Compose service. It queues scanned files under the common catalog (with the legacy direct sender disabled); the original direct Telethon uploader is **not** silently cross-indexed or migrated.

For REST/S3 payload I/O, configure either an authorized **Telethon user** (MTProto) session with posting rights in a dedicated private channel or the legacy-compatible **Bot API** channel/credentials; all interfaces use one transport setting and shared SQL. Pre-switch Bot chunks can also be read directly by the authorized user from the same channel via their immutable SQL message IDs; for inaccessible/different Bot channels retain the original Bot credentials and file-ID index. The S3 master key/credentials are separate. Launching the listeners alone does not provision those secrets. See the [README](../README.md) for installation and examples and [Security Policy](SECURITY.md) for network exposure rules.

### Metadata and physical payloads

SQL stores heads, version records, chunk identities/hashes, copy aliases, jobs and encrypted S3 key metadata. Actual object payload chunks reside in Telegram. SQL backups alone do **not** restore missing Telegram media. Both endpoints must enforce checksum verification and fail closed when an immutable chunk cannot be trusted.

## 3. Production acceptance (not yet signed off)

**Delegated local execution:** [Local Agent Acceptance](LOCAL_AGENT_ACCEPTANCE.md) specifies Docker deployment, exact byte fixtures, independent payload+SQL backups, isolated disaster-recovery drills, GC race tests and evidence files. Providing this mission does not prove that owner-only acceptance gates have run. In particular, multiple Docker containers on a single host are not physically separate machines.
The following gates apply **only after** an owner has provided actual hosts, a suitable private Telegram target, sealed independent backups, and permission to conduct production-equivalent drills. Never upload sessions, tokens, connection URLs or original media identifiers to this public repository.

### 3.1 Independent physical hosts and rate-limit soak

Provide at least two **physically independent** application hosts sharing an explicitly identified SQL database. Record independently observed SHA-256 machine identities and a common DB identity. Two URLs terminating on the same host do not satisfy the requirement.

Configure `TELEDRIVE_P1_HEALTH_HOSTS` with two to eight unique `label`/`url` HTTPS entries, alongside `TELEDRIVE_P1_BASIC_USERNAME` and `TELEDRIVE_P1_BASIC_PASSWORD` in a protected environment. Then:

~~~bash
python scripts/monitor_host_soak.py \
  --output /secure/acceptance/soak.json \
  --duration-seconds 3600 \
  --interval-seconds 60
~~~

The evidence must demonstrate a run of at least one hour, `telemetry_gate_passed=true`, real observed Telegram 429s as required by the gate, shared counter coherence, no unresolved send acknowledgment and drained queues. It is still **telemetry evidence**, not independent proof of physical host separation. Do not intentionally flood Telegram to force 429s.

### 3.2 Recovery of original Telegram media

Keep the original SQL snapshot, expected archive SHA-256, Telegram account credentials and destination/channel identifiers in separate protected storage. Restore into an **isolated staging database**, never over the live production DB.

SQLite backup validation is described in [Backup and Restore](BACKUP_RESTORE.md). With an original, owner-authorized Bot account and channel:

~~~bash
python scripts/rehearse_telegram_dr.py \
  --snapshot /secure/backups/sealed.db \
  --sha256 EXPECTED_ARCHIVE_SHA256 \
  --bucket ORIGINAL_BUCKET \
  --key ORIGINAL_KEY \
  --account ORIGINAL_ACCOUNT \
  --chat-id ORIGINAL_CHAT_ID
~~~

The drill checks archive identity, published SQL metadata, original Telegram bytes, object SHA/length and cross-chunk ranges without reuploading or deleting media. PostgreSQL/MySQL installations must use their appropriate native backups and independent restore databases. A consistent SQL dump **alone** is insufficient original-data disaster-recovery proof.

### 3.3 Legacy cutover only if original legacy sources exist

If real legacy files exist, stop the legacy uploader, make its source directory read-only, fence concurrent writes, constrain paths to approved roots and verify original Telegram bytes and the current SQL heads. This is a **controlled operator action**, not an automated development acceptance step.

~~~bash
python scripts/verify_legacy_cutover.py \
  --database /secure/isolated.db \
  --source-root /approved/legacy \
  --allowed-root /approved \
  --scratch-dir /secure/scratch \
  --bucket ORIGINAL_BUCKET \
  --account ORIGINAL_ACCOUNT \
  --chat-id ORIGINAL_CHAT_ID
~~~

Require `verified=true`, a complete scan, zero inconsistent/queued items, and exact source inode/revision/hash correspondence with the published head and original media. If **no legacy installation exists**, obtain an independent owner attestation (`no_legacy_source_attested=true`) and record `{"not_applicable":true}` rather than inventing a test. No live traffic cutover occurs without change approval.

### 3.4 Two-person evidence review

The offline verifier `scripts/verify_production_bundle.py` consumes `release_sha`, `topology`, `soak`, `dr`, `cutover`, `original_telegram_data_attested` and two independent approvals. Each host's topology entry includes `label`, `machine_id_sha256` and `shared_database_sha256`, with `independent_hosts_owner_attested=true`.

The same canonical JSON (excluding `approvals`) must be signed by **two different people** using separate Ed25519 private keys. Provide independently obtained 32-byte public keys:

~~~bash
python scripts/verify_production_bundle.py \
  --bundle /secure/acceptance/production-bundle.json \
  --release-sha REVIEWED_40_HEX_COMMIT_SHA \
  --owner-public-key /secure/keys/owner.pub \
  --operator-public-key /secure/keys/operator.pub
~~~

An incorrect SHA, incomplete drill, duplicate host identity, invalid media proof or missing approval fails closed. Even a verified bundle is **not** an automatic deployment action or authorization to delete Telegram messages. Explicit owner change control remains necessary.

## 4. P2 recovery and safety roadmap

### 4.1 Physical Telegram garbage collection: disabled

**Implemented safeguards:** An optional MTProto user-SQL send journal now persists pre-send ambiguous intents and acknowledged remote message coordinates, which block non-destructive GC candidate approval; actual independent media backup and owner approval are still required. `StorageGCInventory` performs retention/reference previews with a fail-closed, fingerprinted SQL graph covering HEADs, published/removed versions, transitive copy aliases, multipart parts, writer fences and Bot intents/receipts; `RecoveryManager.cleanup` checks SQL heads, published and removed history, copy aliases, multipart references, send intents and Bot receipts. The Telethon ownership probe also checks account/channel/marker identity. Non-destructive GC audits separately count Bot file IDs without any chunk or acknowledged send record, without exposing opaque file IDs in the admin API.

**Not yet authorized/complete:** transactional version-aware mark/sweep across concurrent writers, a complete writer barrier, independently verified media backups, complete ownership audit, explicit two-person deletion approval and a tested disaster-recovery alternative. Deleted Telegram **message IDs cannot simply be restored from SQL**. Automatic physical GC in WebUI/API remains disabled. Unknown Bot ACKs and ambiguous ownership must stay protected.

Do not enable physical deletion merely because offline integrity tests pass. Require a signed dry-run plan, complete remote inventory, retention/reference validation, independent payload snapshot plus successful restore drill, writer fencing and recorded approval.

### 4.2 Rebuild metadata from verified original Telegram messages

`teledrive.storage.rebuild.restore_isolated_catalog_batch` and `scripts/rebuild_from_telegram.py` verify original Telethon message identifiers, stable UUID/ordinal/SHA-prefix markers, account/channel/message identity, full per-chunk checksums and an **independently supplied** whole-object SHA-256. They create a **new isolated SQLite database**, not a replacement for the live database.

Each plan requires `object_id`, `bucket`, `key`, `sha256`, `size` and `chunks`. Each chunk requires `ordinal`, `account`, `chat_id`, `message_id`, `size` and `sha256`. Multiple plans use `{"objects":[...]}`. The bounded batch accepts 1–128 objects, up to 4,096 chunks and 1 GiB total; the staging database is privately validated (`PRAGMA integrity_check`) and atomically published with mode `0600` only after **all** source bytes verify. A failed object never exposes a partial destination catalog.

~~~bash
# Supply TELEDRIVE_REBUILD_API_ID, TELEDRIVE_REBUILD_API_HASH,
# and TELEDRIVE_REBUILD_SESSION through a secure environment.
python scripts/rebuild_from_telegram.py \
  --plan /secure/verified-plan.json \
  --new-database /secure/isolated-rebuilt.db \
  --account original-user \
  --target ORIGINAL_TELEGRAM_CHANNEL
~~~

**Bounded history candidate inventory:** `scripts/inventory_telegram_history.py` scans at most 10,000 readable messages from **one explicitly authorized original Telegram channel**, checks canonical TeleDrive UUID/ordinal/SHA-prefix markers and outgoing/channel/media hints, and creates a new private JSON report (mode `0600`). It downloads no media and writes no SQL. Missing and repeated ordinals are flagged; the report never claims that the channel scan represents all historical sources.

~~~bash
# Use the same protected TELEDRIVE_REBUILD_API_ID, _API_HASH and _SESSION
# environment variables as the isolated rebuild command.
python scripts/inventory_telegram_history.py \
  --account original-user \
  --target ORIGINAL_TELEGRAM_CHANNEL \
  --limit 1000 \
  --output /secure/isolated-history-candidates.json
~~~

**Independent manifest reconciliation:** `teledrive.storage.reconcile_inventory.reconcile_inventory` compares bounded marker candidates with separately provided, already independently verified recovery plans. Wrong object IDs, ordinals, Telegram account/chat/message coordinates and SHA-prefixes fail closed. `scripts/rebuild_from_telegram.py --inventory /secure/candidates.json` optionally makes this check a prerequisite to full payload byte verification. CI also tests a real channel-marker upload, read-only discovery, independent manifest reconciliation and a new isolated SQLite restore with original Telegram bytes. A match is **not** by itself a verified restore.

**Limits:** inventory markers are **unverified candidates**, not an importable rebuild plan. They do not prove the original bucket/key mapping, complete chunk SHA-256 or sizes, whole-object hash, historical versions, or the existence of messages outside the scanned window/channel. Actual isolated reconstruction still requires independent metadata and byte-level verification. Automated discovery without trustworthy independent metadata is still open work. Bot unknown-ACK recovery needs an independently reliable `file_id`. This reconstruction does **not** upload or delete Telegram media.

### 4.3 Capacity and recovery endurance

The manually triggered [Capacity Soak workflow](../.github/workflows/endurance-capacity.yml) runs the real SQLite/object engine with persistent **synthetic** Telegram files, periodic SQL restarts, historical SHA readback and p50/p95 timings. It does not prove actual Telegram bandwidth, natural 429 behavior, physically separate hosts or an hour of production operation. Those require a separate owner-authorized environment and measured evidence.

### 4.4 Advanced S3 protocol parity

Use the [S3 Specifications and Compatibility Matrix](S3_SPECS.md) as the operation-level P2 Advanced S3 backlog. It distinguishes verified bounded routes from partial AWS semantics and deliberately unsupported APIs; ListParts and ListMultipartUploads now implement owner-scoped SQL-bounded keyset paging, immutable part metadata and validated prefix/delimiter/URL-encoded listings in the documented TeleDrive private S3 contract; this status page remains the source for production acceptance boundaries. GetObjectAttributes is now **supported for the documented private, single-region, general-purpose TeleDrive contract**: signed selected fields, immutable full-object SHA-256, fixed STANDARD class, published VersionIds and recorded multipart ObjectParts with bounded marker pagination. Completion atomically snapshots the selected parts, CopyObject preserves the snapshot, and historic uploads use a strictly validated read-only legacy journal fallback. SQL inconsistency fails closed; Telegram chunks are never mistaken for S3 part boundaries. This is **not** support for AWS managed storage tiers, SSE-C, payer billing, directory buckets or alternate region semantics. See its regression and boto3 tests; validate all mandatory checks on the exact release candidate SHA before making a release claim.

Only SQL-only incomplete-multipart lifecycle abort is now supported, applied by the hourly one-service scheduler; no Telegram messages or completed versions are deleted. Full object lifecycle expiration/transitions, SSE, advanced checksum/SDK modes, bucket policies, comprehensive delete-marker semantics and unverified third-party clients (including Cyberduck) remain unsupported or unverified until independently tested. Do not substitute an expected AWS behavior for TeleDrive's documented supported subset.

## 5. Outstanding work and release decision rules

- [x] Publish bounded v1.0.7 development milestone with separate release artifacts and evidence.
- [x] Implement disposable 13-gate Docker validation and distinct live test-channel Bot/User/Multiworker workflows.
- [x] Implement fail-closed recovery guards, owner-proofed isolated one/multi-object rebuild and an offline dual-approval evidence verifier.
- [x] Verify nine normal workflows on the newer `aa44bd6` development baseline (not the published v1.0.7 SHA).
- [ ] Verify **every required workflow on the exact SHA** of any subsequent release candidate.
- [ ] Obtain owner-supplied physical-host identities, original-media disaster-recovery evidence and legacy cutover/no-legacy attestation.
- [ ] Implement and independently rehearse the complete physical GC safety/rollback alternative before authorizing deletion.
- [ ] Complete unknown-history discovery, additional S3 parity tests and long-duration real-environment capacity profiling.

Use [TODO.md](../TODO.md) for the short working checklist. Update this file when the verified *behavior or acceptance boundary* changes; do not paste every transient CI run log here. Separate per-version Markdown release notes are intentionally not maintained; use [GitHub Releases](https://github.com/babico/TeleDrive/releases).
