# TeleDrive · development status (canonical)
Updated: 2026-10-14. This document replaces historical incremental status narration. Older detailed snapshots remain accessible in Git history.

## Acceptance boundary

| Phase | Development result | Evidence and limits |
| --- | --- | --- |
| P0 · Supported S3/REST/uploader scope | Accepted on the complete 9-workflow green `a7e3adf` baseline | [GitHub Actions](https://github.com/babico/TeleDrive/actions); no claim of full AWS S3 compatibility |
| P1 · Disposable Docker development acceptance | Accepted on `a7e3adf` (Docker 13-gate workflow success) | [run 38565237956](https://github.com/babico/TeleDrive/actions/runs/38565237956); synthetic Telegram transport |
| P1 · Real test-channel Telegram E2E | Accepted at the same `a7e3adf` baseline | User/Bot/Multiworker runs [38565237851](https://github.com/babico/TeleDrive/actions/runs/38565237851), [38565237802](https://github.com/babico/TeleDrive/actions/runs/38565237802), [38565237918](https://github.com/babico/TeleDrive/actions/runs/38565237918) |
| P1 · Production/operator acceptance | **NOT accepted; no production deployment assumed** | Independent physical hosts, real original-channel DR and authorized cutover remain future operator gates |
| P2 · Advanced capabilities | **OPEN, not required for the bounded P0/P1 release** | Physical GC, advanced AWS parity and reconstruction need distinct design and evidence |

Release/signoff SHA must have all required GitHub Actions checks complete and successful. A green workflow on a previous SHA is evidence of a baseline, not a license to mark a later untested SHA green.

## Implemented supported scope

- Multi-account Telegram desktop/CLI/WebUI with single, round-robin, failover and parallel strategies.
- SQL metadata on SQLite, PostgreSQL and MySQL; immutable Telegram object manifests, SHA-256/Range verification, durable journaling and fenced leases.
- REST object and bucket APIs with scoped credentials and admin management.
- Partial SigV4 S3 gateway supporting CRUD, multipart, conditional operations, version history, copy, tags, CORS, restricted presigned URLs and selected checksum/client workflows.
- Real AWS CLI and rclone subprocess tests, Windows/Linux desktop builds, native PostgreSQL/MySQL restore drills and Docker fault injection.
- Read-only GC inventory/preview, opt-in queue/recovery and operator tooling; Telegram physical removal remains disabled.

## Remaining work outside accepted development scope

- **P2-SAFE-GC** — Audited retention/alias/version-aware GC with independent proof of Telegram message ownership, multi-step operator approval and recoverability. No physical deletion without safety proof.
- **P2-S3** — AWS S3 lifecycle, bucket policy, SSE, additional checksum/storage behavior and extended client parity where support is safe and truthful.
- **P2-DR** — Single-object original Telegram reconstruction into a new SQL catalog is implemented behind trusted chunk/marker/SHA proof; automatic unknown-history discovery, broad legacy migration and production-scale DR remain open.
- **PRODUCTION** — Original media/credential restore, independent hosts/long soak with natural rate limits, ownership-approved cutover and external release deployment.
- **CLIENTS** — Cyberduck and additional third-party clients require actual interoperability evidence before being advertised as supported.

These P2/production tasks must not be silently relabeled as completed, removed from tracking, or made release blockers for the explicitly supported P0/P1 subset. See [CI catalog](CI_WORKFLOWS.md), [P0 acceptance](P0_ACCEPTANCE.md), [P1 development acceptance](P1_DEVELOPMENT_ACCEPTANCE.md) and [P1 operator acceptance](P1_PRODUCTION_ACCEPTANCE.md).

## Status discipline

Treat canceled/skipped required jobs as inconclusive, not success. Real test-channel proof is not original production data proof. Never enable physical Telegram deletion as a CI shortcut. Preserve old detailed plans in Git history rather than carrying repeated and stale project-state narratives in this current document.
