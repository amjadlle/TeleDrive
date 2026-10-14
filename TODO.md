# TeleDrive · release closure TODO (tracked)

Date: 2026-10-14. Primary objective: close the **supported P0/P1 development scope**, publish a bounded release, then freeze feature additions until a new planning cycle. Do not conflate P2 with a prerequisite for releasing the completed P0/P1 subset.

## Completed
- [x] Verify all nine original CI workflows passed at the same `a7e3adf` baseline.
- [x] P0 supported-scope development approval, with explicit unsupported S3/production limitations.
- [x] P1 disposable Docker development acceptance: 13 gates, real PostgreSQL, two WebUI instances, restoration and scoped cleanup.
- [x] P1 dedicated test-channel Bot, User and PostgreSQL Multiworker checks on original green baseline.
- [x] Regroup GitHub Actions by Quality / Integration / Resilience / Live Telegram / Delivery, preserve workflow paths and triggers.
- [x] Remove stale 77kB implementation-status narration and replace with concise current acceptance/backlog.

## Required before release signoff
- [ ] Verify every workflow on the **final release candidate SHA**, including the renamed Release workflow, reports a successful conclusion. Canceled/skipped required jobs do not count.
- [ ] Verify package version/tag is new and not already published; publish only via a tagged, verified build.
- [ ] Verify Linux/Windows assets and tagged GitHub Release actually exist.
- [ ] Record release URL, tag, exact source SHA and final CI run evidence, then freeze feature development.

## Deliberately not signed off as P0/P1
- [ ] P2 version-aware **physical** Telegram garbage collection (unsafe until ownership, alias, retention and rollback proof).
- [ ] P2 advanced S3 lifecycle/policies/SSE/full SDK parity.
- [ ] P2 rebuild from original Telegram media, migration and long-duration capacity/performance testing.
- [ ] Production-only physical multi-host soak, actual original-data DR and authorized legacy cutover.

The [CI catalog](docs/CI_WORKFLOWS.md) defines what a green check proves. The [canonical implementation status](docs/IMPLEMENTATION_STATUS.md) describes bounded release support. Keep the physical deletion interlock OFF.
