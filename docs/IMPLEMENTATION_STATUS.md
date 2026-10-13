# TeleDrive implementation status and acceptance backlog

Audit date: **2026-10-13**. Snapshot: `feature/docker-webui-multiaccount`
at `916a41da1734`. This is a code/CI evidence inventory, not a
claim of production readiness. Recheck every workflow at the release SHA.

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

- [ ] **P0.1 Immutable green build:** complete Security, Storage API,
  Database, Desktop (Linux/Windows), and both Telegram E2E workflows on
  the *same commit SHA*. Cancelled jobs do not count.
- [ ] **P0.2 Recovery safety:** inject crashes before/after Telegram ACK,
  at SQL commit, and during multipart Complete; prove SQL fencing and no
  deletion of READY or PENDING Telegram data.
- [ ] **P0.3 Credentials/security matrix:** independent revoked/expired/
  read-only credentials on REST, S3, presigned URLs, tagging, copy, and
  multipart; negative SigV4, metadata mutation and protocol abuse tests.
- [ ] **P0.4 Real multipart:** manually dispatch Telegram Bot Storage E2E
  with `run_multipart=true` and verify 5 MiB+ Telegram data, reparented
  chunks, checksum, retrieval, and test-only message cleanup.
- [ ] **P0.5 Client interoperability:** real AWS CLI and rclone required;
  investigate Cyberduck and large Range/multipart edge cases.

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

At `916a41da1734`, standard Telegram user and bot E2E push
workflows finished successfully on 2026-10-13. The 5 MiB+ real multipart
path is **opt-in and was not established as passing**. Frequent feature
pushes cancelled Security CI and Storage API Integration CI jobs; these
checks must finish on an immutable candidate before P0.1 can close.
Production physical GC and full AWS S3 compliance are not implemented.
