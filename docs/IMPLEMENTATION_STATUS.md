# TeleDrive implementation status and acceptance backlog

Audit date: **2026-10-13**. Snapshot: `feature/docker-webui-multiaccount`
through the current P1/P2 implementation branch (2026-10-13). This is a
code/CI evidence inventory, not a claim of production readiness. Recheck
every workflow at the release SHA.

## Implementation inventory

| Domain | Observed in source | Remaining acceptance |
| --- | --- | --- |
| Multi-account uploader, CLI and Desktop | `teledrive/uploader.py`, `app/` | Mixed-load soak, user session revocation, crash/resume |
| Docker WebUI and REST auth | `Dockerfile`, `docker-compose.yml`, `web/app.py`, `teledrive/api_keys.py` | Deployment hardening, privilege isolation |
| SQL support | `teledrive/db.py`, `tests/test_database_live.py` | Latest-head PostgreSQL and MySQL CI |
| Telegram user + bot transport | `teledrive/storage/*adapter.py` | Rate limiting, revoked access, live multipart |
| Immutable manifests, checksum streaming | `teledrive/storage/engine.py`, `manifest.py` | Cross-process resume and reconciliation |
| S3 core, SigV4, copy, tagging, CORS, multipart | `web/s3.py`, `teledrive/s3/` | Advanced protocol compatibility |
| Stale Complete recovery | `teledrive/s3/multipart.py` | Live crash/fencing acceptance |
| FAILED-object orphan recovery | `teledrive/storage/recovery.py` | Stale PENDING abandonment with writer fencing |
| Non-destructive GC reference audit | `teledrive/storage/gc.py` | Physical deletion is intentionally disabled |
| Linux/Windows desktop packaging | `scripts/`, `tests/test_desktop_ui.py` | Full platform matrix and installer acceptance |

## P0 — release-blocking validation

- [x] **P0.1 Immutable green build:** complete Security, Storage API,
  Database, Desktop (Linux/Windows), and both Telegram E2E workflows on
  the *same commit SHA*. Cancelled jobs do not count.
- [x] **P0.2 Recovery safety (deterministic contract):** pre/post Telegram ACK,
  forced final multipart SQL commit rollback, retrieval failure, SQL restart,
  old-writer fencing and safe non-deletion of READY/PENDING message histories
  covered by Storage API and security regressions. Real process SIGKILL and
  multi-worker soak remain staging/operational acceptance.
- [x] **P0.3 Credentials/security matrix (implemented HTTP scope):**
  independent revoked/expired/read-only credential denial, REST/S3 separation,
  presigned PUT/GET, tagging, copy, multipart, SigV4 mutation and unsafe
  CopyObject metadata directive injection tests passed in CI.
- [x] **P0.4 Real multipart:** run Telegram Bot Storage E2E with
  `run_multipart=true`, or a reviewed feature-branch merge commit
  containing `[p0-live-multipart]`. Verify 5 MiB+ Telegram data,
  restart-persisted bot file IDs, reparented chunks, checksum,
  idempotent completion and test-only message cleanup. Mark complete
  only after a successful *non-skipped* live execution.
  **Verified:** commit `32bb3e850f2c5841cbcb2fa2525340ee25b6667b`,
  Telegram Bot Storage E2E job 115423376758: `TELEDRIVE_E2E_MULTIPART=1`,
  **2 passed in 29.98s**, no skips.
- [x] **P0.5 Supported-client interoperability:** actual AWS CLI and
  rclone processes pass CRUD, sync and forced multipart; multi-chunk 5 MiB+
  Range/HEAD invalid-range cases verified. Cyberduck is **not certified or
  advertised as supported**; its official S3 HTTP/path-style profile was
  investigated and remains a separately tracked P1 manual-client acceptance.
  See `docs/P0_ACCEPTANCE.md`.

## P1 — completeness after P0

- [x] **P1.1 SQL metadata safety:** heartbeat, fencing, stale PENDING
  preview and explicit abandonment are implemented; remote orphan
  identification remains separately open.
- [x] **P1.2 Legacy integration (code + CI):** bounded idempotent folder
  imports, operator preview, fenced automatic cutover, durable Bot API
  upload and worker restart are implemented. Production cutover remains
  explicit and needs operator acceptance on its own data/credentials.
- [ ] **P1.3 Advanced S3 parity:** version listing/restore, logical
  DeleteMarker/VersionId DELETE, conditional writes, presigned multipart
  operations, historical CopySource, common checksums and checksum-pinned
  browser POST are implemented and tested. Browser POST also supports
  exactly signed 200/201/204 success status and HTTPS-only 303 callbacks;
  unsupported policy operators and S3 lifecycle remain open.
- [ ] **P1.4 Operations:** operator metrics/alerts, SQLite online backup and
  verified real Telegram payload restore, Windows/Linux CI, PostgreSQL/MySQL
  integration, multi-process queue tests and **native PostgreSQL 17 + MySQL
  8.4 dump/isolated restore drills** are implemented and verified. Sustained
  real multi-host Telegram fault/429 soak, production cutover and release
  protection still require operator acceptance.

## P2 — advanced functionality

- [ ] **P2.1:** Version-aware audited mark/sweep physical GC with
  retention-aware dry-run approval and ownership verification.
- [ ] **P2.2:** Advanced S3 lifecycle, SSE, policies and virtual-host
  compatibility where Telegram backend semantics permit safe support.
- [ ] **P2.3:** Legacy metadata migration, rebuild from Telegram, disaster
  recovery, capacity testing and multi-worker performance benchmarks.

## CI evidence (snapshot only)

At `e3f086f3361`, all seven push workflows completed successfully on
2026-10-13: Security, Storage API, live PostgreSQL/MySQL, Linux/Windows
Desktop, both standard Telegram E2E, and release verification (not an
actual release publication). Real 5 MiB+ multipart also passed non-skipped
at `32bb3e850f2c` (run `38434063562`), with further P0 tests validated
on `e88d4502cd2d`. Verify all seven again on final signoff SHA.
Production physical GC and full AWS S3 compliance are not implemented.

## P0 safety merge

- CopyObject now requires `x-amz-metadata-directive` in SigV4 SignedHeaders,
  blocking unsigned metadata replacement.
- Added post-Telegram-ACK SQL journal failure, cross-connection writer fence,
  and expired/revoked/read-only credential regression tests.
- Extended PostgreSQL/MySQL CI to cover storage engine and recovery tests,
  with Storage API CI explicitly running the new credential matrix.
- Live Telegram 5 MiB+ multipart **passed** at `32bb3e8` with restart
  persistence, checksum verification and test-only message cleanup.
- P0.2, P0.3 and P0.5 deterministic P0 contracts now pass in CI.
  Extra staging-only stress, Cyberduck and true SIGKILL runs remain explicit
  P1/staging acceptance, not implied production support.

## P0 final acceptance additions

- `tests/test_s3_p0_final.py` injects a database commit failure after multipart
  chunk reparenting, Telegram retrieval failure during Complete, revoked
  presigned read/write credentials, and 5 MiB+ multi-chunk Range/HEAD tests.
- P0 candidate readiness requires a **single fresh seven-workflow green SHA**.
  Cyberduck remains unvalidated and is not a supported release client;
  see `docs/P0_ACCEPTANCE.md`.

## P1 acceptance handoff

The P0 definition is bounded to the tested safe S3 client subset and
non-destructive Telegram storage behavior. Do not infer that Cyberduck, full
S3/AWS feature parity, live cross-worker process kill or physical GC have
passed acceptance. The next workstream is durable queue/resume and operator
stale-PENDING recovery with explicit fencing and dry-run approval.

## P1.1 operator-safe PENDING recovery

- Admin GET `/api/v1/admin/storage/pending-stale` inventories aged and
  expired writer-fenced PENDING objects, including journaled chunk counts,
  without revealing writer tokens or deleting Telegram messages.
- Admin POST `/api/v1/admin/storage/pending-stale/{object_id}/abandon`
  defaults to dry-run; explicit `execute=true` requires Basic/CSRF and
  compare-and-update SQL fencing against that object alone. Legacy unfenced,
  live heartbeat and READY objects cannot be abandoned. Journal kept for
  independent recovery; deletion remains disabled.
- This is metadata reconciliation, **not** automatic retry or physical GC.

- WebUI API Management includes a dry-run-first stale PENDING inventory
  and single-object explicit writer-fencing action; no background cleanup.

## P1.2 durable operator upload queue (initial)

- SQL-persistent job state for files restricted to an explicit
  `TELEDRIVE_JOB_SOURCE_ROOT` path; root-admin POST enqueues, GET lists,
  POST `/run-next` executes one and POST `/{id}/retry` requeues failures.
- Per-job TTL/lease token prevents two workers from completing a job.
  Each Telegram object has its own independent writer heartbeat/fence.
- The source inode/mtime/size is checked before and after Telegram I/O,
  and final HEAD uses an expected generation + object ID CAS. Completed
  manifests are reused when their staged job survives a process crash.
- This initial queue **retries unsaved uploads from the beginning** and
  retains orphans for verified GC. It does not yet resume unjournaled
  Telegram data byte-by-byte; an always-on worker scheduler is future work.

- WebUI now supports inspecting, creating, manually running and explicitly
  retrying source-root-confined durable upload jobs. No new Telegram
  primary-payload backend is introduced.

## P1.3 conditional multipart Complete

- Optional signed `If-Match` or `If-None-Match: *` on POST
  CompleteMultipartUpload now uses a snapshot of generation and immutable
  object ID, checked again inside the atomic final SQL commit.
- A competing write between remote chunk verification and final publication
  yields HTTP 412 and keeps staged messages retryable. Unsigned conditions
  are rejected; incompatible date-based write conditions remain 501.

## P1.3 published immutable history and administrator restore

- New `storage_published_versions` index records only manifests that won
  actual head publication; failed writes, multipart staging objects and
  unfinished uploads never appear in a version list.
- Scoped Bearer GET `/api/v1/buckets/{bucket}/versions?key=...` returns
  verified published IDs, SHA-256, size and current version status.
- Root-admin POST `/api/v1/admin/storage/versions/{id}/restore` makes
  a new immutable alias with metadata and tags copied from the historical
  version and atomically publishes it under generation+object-id CAS.
  No Telegram payload is copied or deleted.
- Versions before installation of this index are not reliably recoverable
  as *published* from old READY journals alone, so no speculative backfill.
  S3 ListObjectVersions and logical delete-marker semantics are now supported.

- Root-admin GET `/api/v1/admin/storage/versions` and WebUI
  API Management present proven published history, snapshot SHA-256,
  generation and authenticated/CSRF-protected alias-based restore.

- Explicit dry-run-first admin version migration only indexes existing
  current HEAD pointers, never guesses old unpublished READY manifests;
  WebUI has an opt-in preview/index control. Earlier superseded versions
  without publication records remain unavailable for safe automatic import.

## P1.4 upload worker and observability (opt-in)

- `TELEDRIVE_JOB_AUTO_RUN=1` enables a single per-WebUI-process
  worker loop. The default remains off. SQL job tokens and TTL protect
  multi-process claims; source-root confinement is mandatory.
- Admin GET `/api/v1/admin/storage/health` reports queue state counts,
  stale lease samples, live/current alias counts and retained Telegram
  message totals; WebUI presents these without exposing credentials or
  enabling physical GC.
- Automatic retries of failed jobs are deliberately **not** enabled:
  operators inspect and explicitly requeue failures to avoid unbounded
  repeated uploads on unrecoverable sources.

## P2 retention-aware GC preview (physical deletion disabled)

Root-admin GET `/api/v1/admin/storage/gc-candidates` lists aged FAILED
journals, excludes PENDING/READY, counts source references across HEADs,
published versions, aliases and multipart parts. Every candidate remains
unapproved for physical deletion until independent Telegram ownership
verification and explicit retention approvals are implemented.

## P1.3 historical GET/HEAD by VersionId

- Signed S3 GET/HEAD now accepts a single `versionId` only for a version
  proven published to that bucket/key in the immutable history index.
  Historical byte ranges and HEAD content-length use the normal checksum
  verified Telegram transport. Deleted current pointers do not hide
  previous published versions.
- Unpublished READY staging manifests and unknown IDs fail with
  NoSuchVersion. Version-specific tagging, CopySource and limited
  ListObjectVersions, delete markers and logical version removal now work.


## Consolidated remaining work — after the current P1/P2 implementation

### Already implemented and covered by automated tests
- P0 accepted safe subset, including real Telegram user/bot E2E and 5 MiB+
  multipart; all seven checks passed on the P0 release candidate.
- Root-admin expired PENDING lease inventory/explicit per-object fence, and
  safe multipart completion recovery; physical messages retained.
- Persistent local-file upload queue, per-job TTL/fencing, source fingerprint,
  explicit retry, staged-manifest recovery, opt-in worker loop and WebUI controls.
- Conditional multipart Complete head CAS; published-history-only version
  index, root-admin alias restore, signed S3 historical GET/HEAD/Range.
- Health counters, conservative alias/retention-based FAILED-journal GC
  candidate planner and explicit current-HEAD migration preview.

### Still open — do not claim these are implemented
- **P1 remaining:** Bot API lost-ACK **automatic recovery** (Bot API cannot
  read arbitrary historical channel posts); durable pre-send fences now
  prevent blind duplicates, but operator-verified reconstruction is still
  required. Unimplemented browser POST
  wildcard operators and non-HTTPS/mixed-response variants, multi-host Telegram 429
  and revocation soak, production-representative staging restore with real
  secrets and Telegram retention, and operator-controlled production legacy
  cutover. Native PostgreSQL/MySQL disposable CI restores, receipt-persisted
  ACKs, bounded Bot 429 handling, folder import, S3 delete markers and
  live SQLite/Bot restore now pass.
- **P2 remaining:** audited retention-approved physical GC, S3 SSE/policy/
  lifecycle semantics, DNS/TLS and external virtual-host/Cyberduck acceptance,
  Telegram rebuild, live full DR/fault/rate-limit benchmarks and installers.
- **Release:** Integration into `main` and a real version-tagged
  Linux/Windows release were not performed by this feature-branch CI.

## Additional job lifecycle safety

- An inactive queued/failed upload job can now be cancelled using
  `StorageUploadJobs.cancel(id)`; active, staged, publishing or completed
  jobs cannot be cancelled, protecting in-flight Telegram ownership and
  staged immutable manifests. Cancellation is metadata-only and persists
  through SQL restart. Root-admin Basic/CSRF cancellation API and WebUI
  action are implemented.

## P1.3 S3 ListObjectVersions (published-only subset)

- Signed `GET /bucket?versions` with independent `s3:ListBucketVersions`
  permission lists only HEAD-proven published immutable versions, not
  staged READY manifests. Supports bounded `prefix`, `max-keys`,
  `key-marker` and `version-id-marker`, returning correct multipart ETags.
- This is a **limited subset**: S3 versioning controls, delimiter and
  alternate encoding remain unsupported.
  Unlinked published objects remain safely discoverable and physically intact.

## P1.3 S3 VersionId tagging

- S3 GET/PUT/DELETE `?tagging&versionId=<published-id>` is scoped to
  the exact bucket/key and a proof-of-publication record. Historical
  tags never alter current-version tags; physical Telegram bytes remain
  immutable. Unknown/staged version IDs return NoSuchVersion. Permission
  and revocation checks use the existing independent tagging scopes.

## P1.3 historical CopySource

- Signed `CopyObject` accepts a single `?versionId=` selector in its
  signed `x-amz-copy-source` header. Only versions proven published to
  the exact source bucket/key are readable; private data cannot be copied
  with a guessed staged ID. No Telegram data is reuploaded or deleted.

## P1.4 / P2 disaster recovery: consistent SQLite metadata snapshots

- `scripts/backup_metadata.py` takes transactional online SQLite backups,
  validates `PRAGMA integrity_check`, produces a SHA-256 fingerprint,
  sets owner-only file permissions, refuses pre-existing destinations and
  never overwrites production DB files.
- Native PostgreSQL/MySQL backup considerations, secrets and Telegram
  session coordination, and staging-only restore verification are in
  `docs/BACKUP_RESTORE.md`. Automated database restore rehearsal is still
  **open for live Telegram**; read-only metadata restore checks are implemented
  but do not prove actual payload recovery.

## P1.3 independently verified SHA-1 / SHA-256 / MD5 checksums

- S3 object PUT and multipart UploadPart now reject contradictory SHA-1,
  SHA-256 or Content-MD5 headers even if the primary signed payload digest
  matches, before reaching Telegram. SHA-1 is used only for interoperability
  integrity checks, never for authentication or access-key derivation.
- CRC32, CRC32C and CRC64NVME are validated for signed PUT/UploadPart
  against the independently calculated streaming digests.

## P1.3 checksum-bound presigned multipart Complete

- A presigned POST is accepted only for an existing `uploadId` Complete
  operation and only with a SigV4-signed exact `x-amz-content-sha256`
  request body digest. XML is rehashed before changing SQL state;
  replay is idempotent and does not reupload Telegram bytes.
- Presigned CreateMultipartUpload with a signed empty-body SHA-256 is also
  supported. Generic presigned POST (HTML form uploads, arbitrary selectors,
  and unsigned payloads) remains rejected and must not be inferred as supported.

## P1.4 actionable operator health alerts

- Admin-only `/api/v1/admin/storage/health` now returns structured
  nonsecret alerts for failed durable jobs, expired PENDING writers,
  stale multipart completions and incomplete 1000-record inventories.
  WebUI shows alert code, severity and count; operator recovery and
  retries remain manual, and no Telegram physical deletion is enabled.

## P2 dry-run disaster-recovery metadata validation

- Read-only SQLite snapshot verifier checks independent backup SHA-256,
  integrity/foreign keys, published versions and current HEAD consistency.
- Tests reject corrupt snapshots, forged HEAD/version hashes and symlinks.
  Restoring production Telegram sessions and missing remote media still
  requires an isolated staging rehearsal and remains unverified.

## P2 opt-in virtual-host-style S3

- S3 can parse a SigV4-authenticated virtual-host bucket only when
  TELEDRIVE_S3_VHOST_SUFFIX is explicitly set to a valid DNS suffix.
  Path-style remains the default, and Host is authenticated by SigV4.
  Proxy X-Forwarded-Host is never used for permission or bucket routing.
- Signed virtual-host GET/HEAD/PUT/ListBucket and host-spoofing rejection
  are covered by Storage API CI. DNS, wildcard TLS certificates, reverse
  proxy forwarding and real external-client verification remain deployment
  prerequisites, not implied automatic compatibility.

## P1 actual-process SIGKILL resume regression

- tests/test_storage_sigkill.py starts a separate OS process executing
  the real durable SQL queue and chunk uploader. It SIGKILLs the worker
  between the first persisted Telegram message and the second send.
  A fresh SQLite worker with expired leases must verify and reuse the
  first remote chunk, transfer only the missing tail and publish an
  immutable HEAD without deleting or retransmitting the existing media.
- This verifies a deterministic process crash; it is not a live
  Telegram revocation or multi-server stress/soak certification.

## P1.2 opt-in legacy-to-object durable folder bridge

- `python -m teledrive.uploader --config ... --scan-only
  --queue-object-bucket EXISTING_BUCKET [--queue-object-prefix RELATIVE_PATH]`
  now scans the configured legacy source tree, honors extension/size filters,
  keeps relative paths as object keys and persists immutable source
  fingerprints in the SQL durable upload queue.
- Stable UUIDv5 keys make repeat scans idempotent across process restarts.
  Existing queued/completed jobs are not duplicated. Modified sources create
  new jobs and must pass destination head CAS when ultimately published.
- This mode **does not upload, change the legacy files table, or delete media**.
  It refuses a concurrently leased legacy uploader. Operators must stop the
  old uploader before enabling the separate durable worker, otherwise
  the two independent pipelines could send the same files.


## P1.2 WebUI legacy folder queue importer

- Root-admin POST `/api/v1/admin/storage/jobs/import-legacy` requires
  Basic authentication and same-origin CSRF protection; defaults to
  `execute=false` for a non-uploading dry run. The source must remain
  within `TELEDRIVE_JOB_SOURCE_ROOT` and the configured legacy uploader
  SQL lease is acquired before scanning. The caller cannot select an
  arbitrary host filesystem root. WebUI provides explicit Preview and
  Queue actions, never automatically invokes the Telegram worker.


## P1.3 Streaming CRC32C checksum integration

- Independent Castagnoli CRC32C validation on object PUT and multipart
  UploadPart verifies the signed request bytes before touching Telegram.
  A declared `CRC32C` SDK checksum must be supplied and correct. The
  streaming implementation is tested with RFC-style known vectors and
  wrong-checksum negative HTTP cases. This is an integrity check, not
  a replacement for SHA-256-bound SigV4 authentication.


## P1.3 CRC64NVME upload integrity

- S3 object PUT and multipart UploadPart now accept signed
  `x-amz-checksum-crc64nvme` (and the matching declared
  `x-amz-sdk-checksum-algorithm: CRC64NVME`). The reflected CRC-64/NVME
  value is accumulated across streamed blocks and compared as an 8-byte
  big-endian Base64 digest **before** a Telegram document send or any
  immutable manifest is created. Invalid/missing declared digests return
  BadDigest; SigV4 SHA-256 binding remains mandatory.
- Tests cover the standard `123456789` checksum, split-block equivalence,
  signed PUT and UploadPart, rejected corruption, and zero remote uploads
  after rejection. AWS-chunked/trailer checksums remain unsupported.


## P1.3 checksum-bound presigned multipart initiation

- SigV4 query-authenticated `POST /bucket/key?uploads` now permits only
  a signed `x-amz-content-sha256` equal to the SHA-256 of an empty body.
  The POST selector is part of the verified canonical query. Any nonempty
  body is rejected before a SQL multipart session is allocated.
- Mixed control selectors, unsigned payload digests, revoked credentials,
  and path tampering remain forbidden. This extends presigned multipart
  support; generic browser form uploads are still unsupported.

- All CRC64NVME declarations use one verified checksum helper: small
  XML/tagging/CORS writes also compare actual bytes, rather than ever
  acknowledging a digest that only PUT/UploadPart checks.


## P1.1 Bot API ACK receipt reconciliation (durable subset)

- After a successful Telegram Bot API `sendDocument` ACK, the BotFileIndex
  atomically commits a file ID and a new keyed receipt recording object ID,
  ordinal, expected size and full SHA-256. This receipt is separate from
  the immutable chunk journal, closing the recoverable window where the
  receipt was committed but `ManifestStore.append` was not.
- A worker reclaiming an expired PENDING writer consults receipts only for
  the next missing chunk; it independently downloads bytes from Telegram,
  checks exact source byte equality, then journals the proven message under
  its fresh writer fencing token. Ambiguous, foreign, missing or changed
  receipts fail closed. No automatic Telegram deletion or speculative HEAD
  publication occurs.
- **Limit:** A successful Telegram send whose response or SQL receipt commit
  was itself lost cannot be rediscovered from Bot API channel history; that
  interval still requires external owner-authorized Telegram reconciliation.
- Cross-SQL-restart resume, no duplicate-send, corrupt receipt, ambiguous
  duplicate and foreign-account negative cases run in Storage API CI.


## P1.4 independent multi-worker SQL soak (deterministic)

- Three independent operating-system worker processes start simultaneously
  against a shared SQLite WAL queue, claim twelve distinct jobs and publish
  100% verified Telegram disk-stub payloads. The test proves each job is
  claimed once, every HEAD is immutable and all remote chunks are accounted
  for. SQLite locked-snapshot contention in queue claims now retries with
  bounded rollback/resnapshot instead of failing active workers.
- This is deterministic multi-process SQL/engine acceptance. Actual Telegram
  429/revocation stress, distributed PostgreSQL/MySQL throughput and extended
  live soak remain separate operational requirements.


## P1.4 live Telegram DR snapshot rehearsal (opt-in credentials)

- The standard Telegram Bot Storage E2E workflow now creates a published
  immutable object in a dedicated test bucket, writes a transactional online
  SQLite snapshot, verifies its independent SHA-256 and HEAD/version/chunk
  referential integrity, closes the original SQL connection, and opens the
  restored snapshot with a freshly instantiated Bot API file index.
- The restored object is fetched from the **real Telegram test channel**,
  verified byte-for-byte and via a cross-chunk range read, then only test-
  owned Telegram messages are cleaned up. No production data is touched.
- This tests a genuine metadata-restore + remote-payload read path; it does
  **not** certify PostgreSQL/MySQL native restores, Telegram Bot account
  revocation recovery, multi-region disaster scenarios or operator secrets
  recovery. Those remain staging/operational acceptance tasks.


## P1.1 Bot receipt SQL atomicity regression

- Added an injected SQLite trigger failure between the Bot file-ID insert
  and ACK receipt insert. Both insertions must roll back together, and no
  discoverable recovery candidate may survive. Test-owned message cleanup
  also removes its associated receipt before a later recovery can use it.

## P1 implementation and CI evidence update (2026-10-13)

- `272d743c`: all seven workflows green after durable Bot ACK receipt
  reconciliation, including real Telegram Bot/User E2E.
- `ce7f96e6`: Storage API passed **339** SQL/HTTP/storage tests including
  the independent three-process, twelve-job SQLite queue soak, and all
  **3** actual AWS CLI/rclone tests; DB CI and release verification passed.
- `17881a75`: live Telegram Bot E2E passed **2** tests (one explicit manual
  multipart skip), including separate SQLite snapshot restore and remote
  media readback with exact SHA-256 and Range integrity.
- A CI pass on an earlier commit does not certify subsequent changes; the
  release candidate still needs a full seven-workflow fresh SHA check.


## P1.4 operator warning for unjournaled Bot ACK receipts

- Admin-only storage health now counts Bot API ACK receipts not referenced by
  any Telegram chunk manifest; this includes transient in-flight ACKs, so
  operators should investigate **persistent** counts rather than treat every
  immediate observation as an orphan. A separate warning code is emitted,
  without exposing Bot file IDs, object IDs, or credentials.
- Multipart reparenting does not create a false orphan: receipts are matched
  by durable account/chat/message identity, not by the original object ID.
  Tests cover a missing journal, redaction and successful journal resolution.


## P1.3 S3 logical DeleteMarker and VersionId removal

- Signed S3 DELETE creates a SQL-only published delete marker with its own
  stable UUID VersionId and a CAS-fenced HEAD transition. GET/HEAD of the
  latest key now returns NoSuchKey and delete-marker response headers;
  historical read-by-VersionId stays supported until explicitly removed.
  ListObjectVersions emits `DeleteMarker` XML entries with stable cursor
  ordering, keeping payload `Version` entries unchanged.
- Signed DELETE `?versionId=` hides only a proven published version for the
  exact bucket/key, without physically deleting any Telegram data. Deleting
  the current version atomically switches HEAD to the latest remaining
  historical version (which may itself be a delete marker). Former version
  IDs are no longer allowed by GET, CopySource, REST restore or tagging.
- Generic S3 versioning configuration, lifecycle/retention policies, and
  physical deletion remain unsupported. Test these only against disposable
  buckets; production GC is still off.

- Version-tag deletion remains a **distinct** authenticated API operation:
  `DELETE ?tagging&versionId=` never deletes the object VersionId. GC live
  head metrics exclude SQL-only delete markers; immutable Telegram references
  are still preserved and counted separately.


### P1 S3 delete-marker regression coverage

- Signed HTTP integration tests cover marker visibility and ordering, stable
  pagination, VersionId deletion and rollback to previous history, foreign
  and staged IDs, conditional replacement, CopySource and tagging isolation,
  scoped credentials, tampered signed selectors, batch-delete response and
  SQLite snapshot retention. All tests exercise metadata-only deletion;
  physical Telegram removal remains disabled.


## P1.2 fenced automatic legacy-folder cutover (explicit opt-in)

- Operator sets `TELEDRIVE_JOB_AUTO_RUN=1`, a pre-existing destination
  `TELEDRIVE_JOB_LEGACY_AUTO_BUCKET`, and optionally
  `TELEDRIVE_JOB_LEGACY_AUTO_PREFIX` and `TELEDRIVE_JOB_LEGACY_AUTO_LIMIT`.
  Legacy `app.auto_run` must be disabled. No cutover is implicit.
- Each scheduler cycle acquires the *same* distributed SQL uploader lease
  used by the legacy Telethon uploader, scans allowed legacy files into
  idempotent UUIDv5 jobs, then runs one durable Bot-API upload while an
  independent connection renews that lease. A competing legacy uploader
  holds or needs the same lease; lease loss cancels the durable task and
  persists partial upload metadata for recovery. Manual WebUI legacy sends
  are refused while this mode is configured.
- Legacy file rows are never rewritten and no Telegram message is removed.
  Jobs targeting an S3 delete marker now snapshot its actual head generation
  and marker ID, preserving compare-and-set on publication.
- This is still an explicit operational migration: source root and bot target
  must be configured, worker runs remain opt-in and separate services using
  different databases are out of scope.


## P1.4 live RDBMS concurrency regression evidence

- `52c442b8` PostgreSQL 17 + MySQL 8.4 CI: **70 passed**, including four
  new tests. Four independent DB sessions compete for the same durable
  queue claim; exactly one worker acquires the fenced lease. Two separate
  importers enqueue the same source revision simultaneously; the stable
  UUIDv5 key produces exactly one record. Both checks run on **both** DBs.
- `d7b906eb` Storage API CI: **360 passed** and the **3/3** real AWS CLI/
  rclone cases passed. These include the leased legacy bridge and WebUI
  cutover restrictions. Release verification and two live Telegram E2E
  workflows also passed at that SHA. The final documentation signoff commit
  reruns the path-filtered release CI matrix; earlier green SHAs do not
  certify later code changes.
- Scope: deterministic concurrency and SQL visibility, not a production
  multi-host Telegram 429/revocation soak. No physical Telegram GC enabled.


## P1.4 live Telegram variable-size stress and recovery matrix (2026-10-14)

- Live dedicated **Bot API** channel receives 0 B, 1 B, 2,047 B, 2,048 B,
  2,049 B, 65,537 B and 1,048,593 B uploads. All data is randomized; each
  case validates Telegram returned message IDs, per-chunk SHA-256, exact
  contiguous offsets, full Telegram readback, four bounded Range requests,
  empty Range, and reopening the SQLite file-ID journal after deleting the
  original local source. Every test cleans up ONLY the IDs it just uploaded.
- Live dedicated **Telethon** account receives 0 B, 1 B, 2,048 B, 2,049 B,
  65,537 B and 1,048,593 B. Independent restarts re-download the real remote
  payload; the upload-tracking transport records ACKs before SQL append so
  cleanup is scoped even after a mid-journal assertion failure.
- Live **durable Bot API SQL job** test uploads 262,157 B across five remote
  chunks, reopens metadata before claim and final readback, verifies exactly
  one job finished, and rejects a concurrently queued mutated local source
  without publishing a second HEAD.
- Live **SQLite backup/restore** tests cover both 3,071 B (two chunks) and
  1,048,593 B (five chunks) with independent checksum and ranged Telegram
  readback from the restored snapshot.
- **One-shot opt-in large test:** the existing `[p0-live-multipart]` push
  marker triggers actual 5 MiB+ multipart assembly and real 5 MiB+1 and
  16 MiB+37 single Telegram Bot documents. The default CI deliberately
  skips those tests to limit bandwidth and respect Telegram flood control.
  Each chunk remains below the Bot API 20 MiB getFile limit; offline exact
  20 MiB success / 20 MiB+1 rejection validates the hard bound.
- First fresh live matrix evidence: Telegram user E2E `38470657196` passed
  **8/8** and Bot E2E `38470588698` passed **9/9** (3 heavy tests skipped).
  Later SHA-bound queue, backup and manual-large acceptance must be checked
  independently; prior successful runs do not certify new commits.

## P1.3 checksum-bound SigV4 browser POST policy

- Browser multipart POST to /bucket now supports a **bounded strict policy**
  profile: exact key and bucket, exact SHA256 checksum, SigV4 credential and
  signing time, an explicit content-length-range condition, and a 15-minute
  maximum expiration. Signatures are verified with existing revocable S3
  credentials and s3:PutObject scope. The file is independently rehashed
  before any Telegram call; failed signatures never create SQL manifests.
- Wildcard keys, starts-with policies, unsigned extra form fields, redirects,
  ACL/encryption/retention and other unsupported browser POST extensions are
  deliberately rejected. The entire MIME form is capped at 20 MiB or lower
  configured REST upload limit. Physical Telegram deletion remains disabled.

## P1.4 real Telethon 5/16 MiB data stress

- A one-shot '[p1-live-size]' commit marker opts the real Telegram user
  E2E workflow into two additional tests (5 MiB + 1 and 16 MiB + 17 bytes).
  They write random real Telegram chunks of 4 MiB each, cross the 4 MiB
  boundary, verify every remote message ID and SHA256, reopen SQL, then
  download the original data using only immutable Telegram messages. The
  cleanup scope is exclusively the ACK IDs recorded by the test invocation.
- The workflow defaults to skipping these high-bandwidth checks on routine
  pushes and supports an explicit workflow_dispatch run_stress flag.

## P1 test signoff evidence — 2026-10-14

- Feature SHA `a01b79bb851b6ceeacae4983a29af8827587854e`:
  **7/7** GitHub Actions workflows successful.
- Storage API **379 passed**, AWS CLI + rclone **3 passed**, PostgreSQL 17 /
  MySQL 8.4 DB Integration successful; Security CI and Windows/Ubuntu Desktop
  CI successful. Release workflow was **verification only**: no release tag,
  binary publication or main branch merge.
- Dedicated Bot API channel **12 live tests passed**, including real Bot ACK
  stored without a chunk journal, subsequent SQL restart, exact readback and
  verified adoption **without retransmitting the accepted chunk**.
- Dedicated Telegram user account **10 live tests passed**, including
  one-shot **5 MiB + 1 B** and **16 MiB + 17 B** payloads split into 4 MiB
  Telegram messages, per-message hashes, reopened SQLite, full download and
  cross-boundary byte ranges. The `[p1-live-size]` marker is opt-in and not
  repeated on ordinary pushes.
- boto3 native SigV4 browser POST, strict signed-policy SHA-256, permission
  checks, tampering, lifetime, size and duplicate form-field rejection pass
  against real HTTP multipart requests, backed by deterministic Telegram
  bytes and real SQL manifests. Unsupported policy operators remain explicit.
- **This signoff is for the tested feature-branch subset, not a blanket P1
  production readiness certification.** A later document-only commit does
  not change the underlying tested implementation but still gets its own CI.

### P1.4 bounded Telegram 429 retry policy

- The Bot API adapter decodes only explicit Telegram 429 responses with
  verified integer retry_after seconds (1..3600), without logging or
  exposing the API token. A single retried send is allowed after a server
  rejection and only when the requested cooldown is <=60 seconds.
- Retry and per-chat minimum intervals are serialized on the transport.
  Unknown HTTP/timeout errors remain fail-closed and are **not retried**:
  Telegram may already have accepted the document before its ACK was lost.
  A successful retried ACK is still durably indexed before chunk journaling.
- Deterministic HTTP 429, repeat-429, >60-second cooldown, one-success-after-
  cooldown, and ambiguous-network-failure tests assert the exact number of
  actual Telegram sends/receipt writes without intentionally flooding CI.

## P1.4 latest acceptance — native restore and bounded Telegram 429

- Native DB CI run `38475604718` successful: PostgreSQL 17 `pg_dump -Fc`
  and `pg_restore` plus MySQL 8.4 transaction-consistent `mysqldump` and
  restore into separate empty `teledrive_dr_ci` databases. **70 live DB
  tests passed** and both restored stores reproduced exact HEAD/generation,
  two checksum-bound immutable chunk records, published/removed versions,
  UTF-8 tags, unjournaled Bot ACK receipt, queued job and decryptable
  independent **test** S3 credential. No production media was deleted.
- Storage API run `38475357913` **384 passed**, AWS CLI/rclone **3 passed**,
  including bounded 429 parsing/retry, 429 exhaustion and indefinite network
  outcome fail-closed behavior. A known rejected send can be retried once
  after retry_after, but an ambiguous failed response is never automatically
  resent.
- Real Bot E2E run `38476179404` **13 passed, 3 optional large tests
  skipped**. A synthetic first Telegram 429 is followed by two genuinely
  accepted channel chunks; exact two-record durable SQL receipt index,
  independent restart/readback SHA-256 and cross-chunk ranged bytes pass.
  Only test-owned message IDs are cleaned up. We do NOT intentionally
  exceed Telegram flood limits to manufacture 429 responses.
- Real Telethon E2E and Security CI on `6452c123` both successful.
  Successful isolated CI drills are not a full production secrets-recovery
  test, nor a sustained multiple-application-host load test.

## P1.3 signed browser POST prefix and P1.4 real size extensions (2026-10-14)

- Browser POST accepts SigV4 policy `["starts-with", "$key", "prefix/"]`
  for the object key **only**, keeping bucket, signing scope, date, SHA-256,
  mandatory size range and expiration constraints exact. Unexpanded
  `${filename}` template values are still rejected as actual object keys.
- HTTP integration tests cover a signed nested Unicode key, refusal of
  out-of-prefix destinations, checksum mutation, other-bucket uploads,
  unsupported wildcard fields and independently generated boto3
  browser-prefix policies with a browser-expanded key.
- Live Bot API and Telethon standard E2E matrices add random 2 MiB + 17 B
  three-chunk uploads (1 MiB + 1 MiB + 17 B) with real message IDs, SQL restart,
  per-chunk checksums, Range validation and scoped cleanup; Telethon also
  adds 2047 B and Bot adds exact 8192 B.
- **Verified at SHA `4b5984ba`: 7/7 GitHub Actions workflows green**;
  the newer response-mode/redirect/mixed-binary changes require their own
  same-SHA GitHub Actions signoff. This does not close production multi-host
  Telegram soak, lost-ACK-before-receipt or full S3 policy parity.

## P1.3 browser POST response-mode and HTTPS callback contract (2026-10-14)

- Signed browser forms may request AWS-compatible success status
  `200` (empty), `201` (`PostResponse` XML with bucket/key/ETag/Location),
  or `204` (empty, default). This optional field must itself appear as an
  **exact policy condition**; 202/302, malformed values, unsigned overrides
  and duplicate fields fail closed before any Telegram transfer.
- Exactly signed `success_action_redirect` can select a **303** response to
  an HTTPS destination; the caller's signed query is preserved and
  URL-encoded `bucket`, `key`, `etag` are appended only after upload
  and durable publication. TeleDrive never fetches callback URLs itself.
  HTTP redirects, credentials inside URLs, fragments, control characters,
  invalid ports, changed/unsigned destinations and conflicting signed status
  + redirect are rejected before any send. Other browser POST success
  operators remain unsupported.
- HTTP integration tests use independently signed boto3 browser policies for
  201 XML and HTTPS 303, plus randomized payload sizes, Unicode/XML/URL
  escaping, normal GET readback, signed field tampering and negative URL
  vectors. No intentional Telegram deletion is introduced.
- Live user/Bot channel tests now also include 3 MiB + 31 B sent as
  four 1 MiB-bounded remote messages, along with a NUL/0xFF-alternating
  65,537-byte payload. Existing readback SHA-256, SQL restart, contiguous
  offsets, ranged downloads and test-owned cleanup remain mandatory.
- **Fresh same-SHA CI evidence required** before claiming these changes
  accepted. This scope is not full AWS browser-policy parity, production
  credential recovery or an extended multi-host Telegram soak.

## P1.1 pre-send Bot API unknown-outcome durable fences (2026-10-14)

- Before sending any nonempty Bot chunk, SQL now commits a keyed intent
  `(account, chat_id, object_id, ordinal, SHA-256, size)`.
  Telegram ACK indexing and intent removal occur in **one** SQL transaction.
  A process crash, timeout, response lost after Telegram accepted the message,
  or injected receipt SQL failure leaves the intent present after restart.
- When an expired worker attempts resume, a missing ACK receipt combined with
  an unresolved send intent **fails closed** instead of resending the chunk.
  This prevents automatically creating a duplicate Telegram post with an
  unknown accepted outcome. Exact Telegram HTTP 429 rejections are known
  non-acceptances; they remove only their matching intent and preserve the
  bounded 429 retry path. No remote deletion is enabled.
- Root-admin storage health reports a **critical** `unresolved_bot_sends`
  alert and aggregate count, without disclosing Telegram file IDs, message
  IDs, tokens, object IDs, or source paths. Persistently unresolved cases
  need **out-of-band owner-authorized** inspection. Bot API alone cannot
  recover an arbitrary channel post after losing its ACK.
- Deterministic contract tests: inspect SQL intent from another connection
  *during* send, server ACK-loss after real fake remote acceptance, SQL
  close/reopen, simultaneous duplicate sends, independent SQL connections,
  nonmatching account/size/checksum, failed receipt insert, and known-429
  cleanup. The real Telegram test deliberately withholds an actual successful
  ACK, verifies zero duplicate send after restart, independently downloads
  the accepted file from Telegram, records the known test ACK and resumes the
  verified tail. This is **not** an automatic production recovery mechanism.

## P1.4 bounded live Telegram mixed-size soak (opt-in)

- Bot manual `workflow_dispatch.run_soak=true` or reviewed push marker
  `[p1-live-soak]` enables a real-channel, 12-object mixed-size soak, ranging
  from empty to 3 MiB + 31 B. Payloads alternate between high-entropy and
  NUL/0xFF patterns; uploads are throttled, SQL connections reopened every
  three objects, all returned message IDs tracked, each object fetched from
  Telegram with exact SHA-256 and bounded Range tests, and **only** messages
  uploaded by that test invocation are cleaned up.
- This is a bounded single-runner reliability acceptance, not a sustained
  multi-host/per-region production soak or revocation load certification.
  Tests are optional by design to avoid unexpected data transfer/costs.
- **New P1.1/P1.4 changes require fresh 7-workflow same-SHA CI evidence.**

## P1.3 signed browser POST Content-Type interoperability (2026-10-14)

- Browser POST accepts the optional S3 `Content-Type` form field only
  when the signed policy includes an exact field condition or a bounded
  `["starts-with", "$Content-Type", "image/"]` constraint. Checksum,
  credentials, bucket, success status and redirect remain strictly bound;
  an added unsigned Content-Type or an altered exact MIME type fails before
  Telegram send.
- MIME values pass through the normal S3 metadata validator (bounded value
  size, no control characters) **before upload** and are attached to the
  immutable object in the same SQL transaction that publishes the HEAD.
  Subsequent signed GET and HEAD return the persisted content type without
  changing Telegram bytes.
- Real HTTP SDK interoperability covers boto3-generated `Content-Type`
  POST policies, MIME prefix acceptance and denial, binary-body checksum,
  signed head/readback, and malformed metadata. The multipart file part
  MIME header is **not** treated as an unsigned replacement for this
  independently signed form field.
- Advanced `x-amz-meta-*` browser fields, arbitrary AWS policy operators
  and unsupported storage semantics are still rejected rather than
  acknowledged without persistence. Same-SHA CI signoff remains required.

## P1 end-to-end acceptance trigger (2026-10-14)

- This revision schedules one explicit real Telegram acceptance run for
  the expanded Bot mixed-size workload and the Telethon 5/16 MiB matrix.
  The upload/data tests run ONLY against configured dedicated E2E channels,
  record every received message ID and perform test-owned message cleanup.
- An all-green seven-workflow result at the **current exact commit SHA** is
  required before this acceptance can be checked off. Do not treat a
  cancelled, skipped opt-in stress case or an earlier commit as evidence.
- Outstanding non-automatable work remains: full operator-authorized
  reconciliation of a Bot message whose entire ACK was lost, prolonged
  cross-host distributed Telegram flood/revocation soak, production secrets
  restore, controlled live legacy cutover and unsupported advanced S3
  policy/lifecycle semantics. These must not be marked complete from CI.
