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

- [ ] **P1.1:** Heartbeats, fencing and explicit abandonment of stale
  PENDING uploads with dry-run-first Telegram evidence checks.
- [ ] **P1.2:** Persistent upload queue/job resume; reconcile the legacy
  folder uploader with the Telegram-only immutable object pipeline.
- [ ] **P1.3:** Version listing/restore, conditional multipart Complete,
  advanced checksums and presigned multipart workflows (fail closed until safe).
- [ ] **P1.4:** Operator recovery controls, metrics, alerts, migrations,
  backup/restore runbooks and mandatory branch protection checks.

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
  NoSuchVersion. Version-specific DELETE, tagging, CopySource and AWS
  `ListObjectVersions` remain unsupported until full safe semantics exist.


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
- **P1:** True chunk-level resumable uploads, automatic Telegram orphan
  reconciliation for Bot API, legacy folder uploader integration, durable
  cross-worker source ownership/soak, full S3 ListObjectVersions/delete
  markers, version-ID tagging, advanced checksums/presigned multipart POST,
  comprehensive operator alerts/migration/backup-restore runbooks.
- **P2:** Retention-approved physical mark/sweep GC with complete remote
  ownership verification, S3 SSE/policies/lifecycle/virtual-host compatibility,
  legacy record/data rebuild from Telegram, disaster-recovery rehearsals,
  rate-limit/fault benchmarks and full installer staging acceptance.
- **Release:** Integration into `main` and a real version-tagged
  Linux/Windows release were not performed by this feature-branch CI.

## Additional job lifecycle safety

- An inactive queued/failed upload job can now be cancelled using
  `StorageUploadJobs.cancel(id)`; active, staged, publishing or completed
  jobs cannot be cancelled, protecting in-flight Telegram ownership and
  staged immutable manifests. Cancellation is metadata-only and persists
  through SQL restart. Operator-facing cancellation API is still pending.

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
  **open**; this snapshot is not a substitute for Telegram payload access.

## P1.3 independently verified SHA-1 / SHA-256 / MD5 checksums

- S3 object PUT and multipart UploadPart now reject contradictory SHA-1,
  SHA-256 or Content-MD5 headers even if the primary signed payload digest
  matches, before reaching Telegram. SHA-1 is used only for interoperability
  integrity checks, never for authentication or access-key derivation.
- Unsupported CRC32C and CRC64NVME checksum variants return an explicit
  NotImplemented response rather than false success. CRC32 verification
  remains supported; full AWS checksum family compatibility is still open.

## P1.3 checksum-bound presigned multipart Complete

- A presigned POST is accepted only for an existing `uploadId` Complete
  operation and only with a SigV4-signed exact `x-amz-content-sha256`
  request body digest. XML is rehashed before changing SQL state;
  replay is idempotent and does not reupload Telegram bytes.
- Generic presigned POST, including CreateMultipartUpload and unbound
  payload signatures, remains rejected. A future supported-scope S3
  presigned POST must meet the same integrity and owner requirements.
