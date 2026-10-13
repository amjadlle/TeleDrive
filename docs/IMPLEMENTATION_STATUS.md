# TeleDrive implementation status and acceptance backlog

Audit date: **2026-10-13**. Snapshot: `feature/docker-webui-multiaccount`
at `e88d4502cd2d` plus the final P0 signoff candidate. This is a
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
