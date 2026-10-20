# Engineering Checklist

> **Purpose:** short, actionable development and release tasks. For decisions and evidence limits, see [Project Status](docs/IMPLEMENTATION_STATUS.md). For commands and CI contracts, see [CI and Test Guide](docs/CI_WORKFLOWS.md).

## 0. Telethon-only purge — complete (merged PR #15)

> Owner: Bot work is discarded completely; every live transport becomes Telethon USER MTProto only. CI was green on the exact PR head `56813a8`.

- [x] Bot adapter/receipt modules, Bot tests and the Bot E2E workflow deleted.
- [x] GC inventory/reference graph and physical-cleanup fences: MTProto send intents/receipts + active durable jobs only.
- [x] Web/S3 services: `storage_transport` factory, Telethon `RECOMMENDED_CHUNK_SIZE`, MTProto health alerts, `mtproto-unresolved` admin endpoint.
- [x] `rehearse_telegram_dr.py`: USER-only read path, reusable `open_read_only_user`, no Bot fallback.
- [x] MTProto rate guard: shared send slots, flood-wait cooldowns, durable flood observations; wired into `UnifiedUserTransport`.
- [x] Fault-injection scenarios and all converted test fakes run USER MTProto.
- [x] Multiworker PostgreSQL E2E converted to USER MTProto; channel resolution by name or numeric target.
- [x] All scripts (acceptance, rehearse, setup, cutover), workflows (7-workflow matrix), `test_ci_workflows.py` and docs converted.

## 0.1 In progress — config-only settings overhaul (owner directive)

> Owner: everything Telethon-only and config-driven. Except TZ and the config-file path, NO env settings; the UI must edit the config; strategies must cover single/round_robin/failover/parallel; SSL script; compose healthcheck/logging; DB extra packages via config; all tracked here.

- [x] `storage:` config section with `apis:` list + channel/strategy (merged PR #20); root `config.example.yaml`; examples/ folder removed.
- [x] Central settings loader (`teledrive/settings.py`): all service settings from `config/config.yaml` only (`web:`, `jobs:`, `state:`, `storage:`, `paths:` sections); no operator env except TZ + `TELEDRIVE_CONFIG`.
- [x] Removed `TELEDRIVE_STORAGE_TRANSPORT` (only Telethon exists).
- [x] Strategies `single | round_robin | failover | parallel` supported in `storage.strategy`; `parallel` uploads chunks concurrently across the apis sets (engine batches via `parallel_degree`).
- [x] Legacy investigation: `state.database_url` and `upload.source_dir` are what the unified service/bridge use; accounts/phone dropped from the example config (sessions are pre-authorized; phone/2FA used once interactively by `scripts/dev/create_storage_user_session.py`).
- [x] UI: Settings page gained a Storage card (channel, strategy, apis: list editor) writing back to config.yaml via the existing /api/config PUT.
- [x] SSL: `scripts/ops/generate_ssl.py` (openssl self-signed cert+key, config snippet, `--test` HTTPS verification) + `web.ssl_certfile/keyfile` + unified.py TLS support.
- [x] docker-compose: env stripped to TELEDRIVE_CONFIG + TZ; added healthcheck and json-file log rotation; service binds 0.0.0.0 (host mapping stays loopback).
- [x] `state.db_extra_packages:` installs extra DB drivers into /data/site-packages at startup (oracledb example in config.example.yaml); no env.
- [ ] Local skipped tests: install aws CLI so `tests/s3/test_s3_cli_compat.py` runs locally; document why live e2e skips stay credential-gated (they run green in CI).

## 1. Owner directives


- [x] **Desktop app cancelled completely** (merged PR #16): removed `app/`, `installer/`, desktop requirements/example config, build scripts, desktop modules/tests and the desktop CI workflow. Shared config/state helpers kept.
- [x] **Single requirements + single config** (merged PR #16): one `requirements.txt`; the service generates `config/config.yaml` with defaults (no example files needed).
- [x] **Website not maintained here** (merged PR #18): `website/` and every reference removed.
- [x] **Multiple api_id/api_hash pairs** (config-driven): `storage:` section in `config/config.yaml` holds the dedicated channel, `round_robin`/`failover` strategy and an `apis:` list of `{api_id, api_hash, session_file, account}` sets; root `config.example.yaml` is the tidy template; env comma-lists remain a legacy fallback; adversarial tests.
- [x] **Regroup `scripts/` and `tests/`** (PR #19): tests split into `s3/ storage/ web/ e2e/ meta/`, scripts into `ops/ dev/`; every import, CI path and doc link updated. Release packaging now builds the single-container Docker image.

## 2. Verified milestones

- [x] Publish [v1.0.7](https://github.com/babico/TeleDrive/releases/tag/v1.0.7) from `747747652cd1ca36878453d49daf627d8c0a60ad` with Linux and Windows release assets.
- [x] Validate the bounded REST/S3 storage feature set and 13-gate disposable Docker fault-injection acceptance (the desktop application was later removed).
- [x] Exercise real Bot API, user MTProto and multiworker PostgreSQL transfer scenarios against dedicated test Telegram targets.
- [x] Provide shared REST/S3/local-job object catalog and a single WebUI/REST/S3 Docker container.
- [x] Implement guarded metadata recovery, owner-verified isolated Telegram rebuild (atomic bounded multi-object batches), and the offline two-person production-evidence verifier.
- [x] Verify all nine standard GitHub Actions workflows successfully on development commit `c2b76880b4ada3d61d47a7926ea819244047fa33`, including User MTProto, isolated historical Bot, and PostgreSQL multiworker workflows. This does **not** change the contents of v1.0.7.

## 3. Engineering backlog

- [x] Implement and CI-validate an isolated, read-only **Telethon USER MTProto original-media restore rehearsal** using an independently sealed SQL snapshot, original channel/message references and whole-body/range SHA-256 verification. Owner-authorized production-equivalent execution remains an operator acceptance task.
- [x] Require Telethon USER MTProto for every unified REST/S3/local-job upload; reject Bot mode and do not read stale Bot secrets, while retaining hash-verified read access to original Bot objects from the same channel.
- [x] Add mainline release-gated cleanup of only merged, unchanged, unprotected PR branches; keep open/unmerged/advanced branches untouched.
- [x] Add bounded, admin-authenticated, read-only history enumeration for the live Telethon storage session, with exclusive cursor pagination and visibly unverified other-sender marker candidates; prohibit guessed bucket/key mapping and Telegram mutations.
- [x] Implement an authenticated USER-MTProto transport for the unified REST/S3/local-job SQL catalog, private-channel-only operation, durable uncertain-send evidence, signed S3 synthetic regression and backward-compatible Bot object read fallback.
- [x] Write a complete [owner-local Docker, independent media backup, restore and GC acceptance mission](docs/LOCAL_AGENT_ACCEPTANCE.md). This checks off the **runbook delivery only**, not operator execution or production authorization.
- [x] Complete bounded ListParts and ListMultipartUploads with SQL-keyset pagination, genuine metadata fields, delimiter/URL encoding, ownership isolation, boto3 and adversarial tests; promote both documented private S3 contracts after CI.
- [x] Test non-destructive GC SQL graph guards (including removed versions, writer fences and active durable jobs) and real test-channel history inventory automatically in CI.
- [x] Implement the full GC **non-destructive machinery**: StorageGCInventory.inspect (complete reference graph: HEADs, published/removed versions, delete markers, transitive copy aliases, multipart parts, MTProto intents/receipts, active durable jobs, writer fences), preview_failed_retention (bounded dry-run candidates) and plan()/verify_plan() (integrity-bound, stale-plan-detecting dry-run plan with explicit owner-gate checklist). Physical deletion remains disabled by default.
- [ ] **Authorize physical GC** only after sealed independent payload backup, ownership/alias/retention checks, an actual restore drill, writer barrier, two-person approval and an owner-authorized isolated single-message trial. `deletion_permitted` stays False; no Telegram media has been deleted.
- [x] Add a bounded, read-only marker inventory for one owner-authorized Telegram channel, with private output and synthetic regression coverage; candidate hints are not recovery proofs.
- [x] Compare history candidates against independently supplied bucket/key, full SHA and message identity; real CI performs isolated byte-verified restore of its own Telegram test fixture.
- [ ] Extend trustworthy recovery of unknown original Telegram history beyond candidate markers without inventing bucket/key, full SHA-256 or missing version metadata.
- [x] Align S3 documentation with CRC64NVME integration-test coverage; retain explicit limits for other checksum modes.
- [x] Count unresolved MTProto send intents without journal/receipt evidence in non-destructive GC audits.
- [x] Implement signed Get/PutBucketVersioning with strictly Enabled semantics, scoped permissions and real boto3 compatibility tests.
- [x] Add signed Get/PutPublicAccessBlock for the enforced all-private configuration; reject any weakening attempts.
- [x] Support signed lifecycle XML GET/PUT/DELETE and hourly bounded incomplete-MPU abort without physical Telegram deletion.
- [x] Add 116-operation machine-readable S3 status manifest plus signature-independent, fail-closed selector validation and adversarial REST/S3 CI coverage.
- [x] Implement scoped read-only GetBucketPolicyStatus as an honest private-only response with boto3 integration and negative policy tests.
- [x] Complete AWS-compatible PutBucketTagging with 50 UTF-16-aware bucket tags, idempotent replacement, MD5/CRC validation and boto3 regression coverage.
- [x] Complete ListBuckets: bounded SQL keyset pagination, Prefix/MaxBuckets/BucketRegion, tamper-proof scoped cursor, actual bucket creation times and boto3 clients.
- [x] Extend GetObjectAttributes with published VersionId reads, immutable SHA-256 Checksum and STANDARD storage class; verify signed requests and boto3 compatibility.
- [x] Complete bounded GetObjectAttributes for private general-purpose buckets: immutable committed multipart ObjectParts snapshots, keyset part pagination, copy/history preservation, pre-upgrade journal fallback and fail-closed tamper handling. Validate with real boto3/AWS CLI/rclone and SQL/HTTP regression CI; keep AWS-only tiers/SSE and directory buckets out of scope.
- [ ] Deliver [P2 Advanced S3](docs/S3_SPECS.md#5-p2-advanced-s3-implementation-plan) from the [116-operation S3 compatibility matrix](docs/S3_SPECS.md): fail-closed selectors, advanced multipart/checksums, policies, encryption, lifecycle and independent client tests. Keep partial capabilities explicitly partial until verified.
- [x] Expose opt-in lease-fenced legacy folder ingestion in default single-container Compose and verify cross-interface REST/S3 readback from the shared catalog.
- [ ] Establish longer-duration real-environment capacity benchmarks on owner-controlled infrastructure.

## 4. Operator-only production sign-off

**Assigned to on-device agent:** [Owner-local acceptance mission](docs/LOCAL_AGENT_ACCEPTANCE.md). Its safe synthetic and test-channel work can run on one Docker host. Original-media recovery, physically distinct hosts, authorized soak, real legacy cutover and independent approvals must remain unchecked until documented original-owner evidence exists.
- [ ] Collect independently verified identities from physically distinct deployment hosts sharing the intended SQL database.
- [ ] Rehearse original-media recovery using the owner's sealed SQL archive, Telegram credentials and real channel without overwriting production data.
- [ ] Conduct the authorized physical soak; capture genuine telemetry without deliberately flooding Telegram.
- [ ] Perform legacy cutover only when a real legacy source exists, otherwise obtain the explicit signed no-legacy exemption.
- [ ] Validate independent owner/operator approvals with `scripts/ops/verify_production_bundle.py`; never infer sign-off from a synthetic CI fixture.

## 5. Release decision rules

- [ ] For any **new** release candidate, require all mandatory checks to pass on **that exact SHA**; previous green commits cannot be reused as proof.
- [ ] Review binaries, tag, release metadata and supported-feature limits before publication; do not silently publish unreleased `main` features as v1.0.7.
- [ ] Keep automatic physical Telegram deletion disabled until its independent safety conditions are verified.

**Reference:** [Project Status and Acceptance](docs/IMPLEMENTATION_STATUS.md) · [CI and Test Guide](docs/CI_WORKFLOWS.md) · [Backup and Restore](docs/BACKUP_RESTORE.md).

## 2. Legacy direct-send uploader removal — complete (merged cf98e1a)

- [x] Legacy Telethon folder scanner/uploader CLI (`teledrive.uploader` main/async_main/parse_args) removed.
- [x] Legacy direct-send scheduling, account management, `files`/`account_status` DB helpers removed from `uploader.py` (config/state/scan primitives retained).
- [x] WebUI legacy endpoints removed: `/api/dashboard`, `/api/files`, `/api/process/*`, `/api/auth`, `/api/logs`; `scheduler_loop`, `start_uploader`, `stop_uploader`, `process_snapshot` removed.
- [x] Legacy auto-bucket scheduler (`jobs.legacy_auto_bucket`) removed; folder ingestion now only via admin `import-legacy` endpoint.
- [x] `pyproject.toml` CLI entry removed; README §6 updated.
- [x] `build_caption` retained (used by `validate_config`); tests updated (test_openapi, test_security, test_legacy_bridge, test_legacy_import_admin).
- [x] Full local suite: 1085 passed / 61 skipped / 0 failed on `cf98e1a`.
- [x] PR opened: https://github.com/amjadlle/TeleDrive/pull/2 (CI pending maintainer approval of fork workflows).
