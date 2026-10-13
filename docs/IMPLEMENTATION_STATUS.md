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
- [ ] **P1.2 Legacy integration:** durable job queue, verified chunk resume
  and worker restart are implemented; integrating the legacy folder
  uploader with this pipeline remains outstanding.
- [ ] **P1.3 Advanced S3 parity:** published-only version listing/restore,
  conditional and presigned checksum-bound Complete, version tagging,
  historical CopySource and common checksums are implemented; S3 delete
  markers and generic presigned POST remain outstanding.
- [ ] **P1.4 Operations:** operator controls, metrics/alerts, SQLite
  backup/verification and runbooks are implemented; repository protection,
  cross-platform migrations and live-remote DR drills remain outstanding.

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
  Full S3 `ListObjectVersions`/delete-marker semantics remain pending.

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
  ListObjectVersions now work; delete markers and version DELETE remain open.


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
- **P1 remaining:** Bot API ACK-without-receipt loss (not discoverable via
  Bot API), legacy folder uploader merge, multi-worker soak, S3 delete markers,
  generic browser presigned POST, migration and
  real Telegram restoration exercises. Receipt-persisted ACKs now reconcile.
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
- This is a **limited subset**: delete markers, S3 versioning controls,
  version deletion, delimiter and alternate encoding remain unsupported.
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
