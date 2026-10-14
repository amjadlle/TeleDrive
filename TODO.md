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
- [x] Verify eight independent CI workflows on released v1.0.7 source SHA; Release publishing also succeeded (a second duplicate attempt correctly refused overwrite).
- [x] Prepare version 1.0.7 and verify that v1.0.7 tag/release do not already exist. Release publication is gated by all eight independent CI workflows on the **same SHA**.
- [x] Verify Linux/Windows assets and v1.0.7 tagged GitHub Release exist.
- [x] Record release [v1.0.7](https://github.com/babico/TeleDrive/releases/tag/v1.0.7), source `747747652cd1ca36878453d49daf627d8c0a60ad`, and existing CI evidence; new P2 work is unreleased development, not a silent change to v1.0.7.

## Deliberately not signed off as P0/P1
- [x] Recheck SQL HEAD/version/alias/multipart/Bot ACK references before recovery cleanup; add regression tests.
- [ ] Authorize **physical** version-aware Telegram GC only with a sealed independent payload backup, complete ownership proof, two-person approval and a tested replacement/recovery plan. It remains disabled.
- [ ] P2 advanced S3 lifecycle/policies/SSE/full SDK parity.
- [x] Add owner-proofed single-object Telegram rebuild into a new isolated SQLite catalog and negative tests.
- [ ] Full original-channel history discovery, safe migration and long-duration physical-host capacity benchmarks (requires owner-held data/hosts).
- [x] Add dual-attested, fail-closed offline production-evidence verifier for soak/original-media DR/cutover reports.
- [ ] Conduct **actual** 1h+ physical multi-host soak, original-channel DR and owner-approved legacy cutover (or signed no-legacy exemption) on a real installation.

The [CI catalog](docs/CI_WORKFLOWS.md) defines what a green check proves. The [canonical implementation status](docs/IMPLEMENTATION_STATUS.md) describes bounded release support. Keep the physical deletion interlock OFF.

Release candidate: `v1.0.7`. The marker commit `[publish-release]` requests artifact builds and publishing; missing/failed required workflows must block the release. No unrelated feature additions should be merged after a successful release.

Reference: [Single P2 backlog and safety boundary](docs/P2_RECOVERY_AND_GC.md), [Production evidence runbook](docs/P1_PRODUCTION_ACCEPTANCE.md). Never mark live operator-only gates complete using synthetic fixtures.
