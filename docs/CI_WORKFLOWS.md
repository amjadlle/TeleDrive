# Continuous Integration and Test Guide

> **Audience:** contributors, maintainers, and CI operators.
> **Scope:** automated workflows, local validation, fault-injection acceptance, and evidence interpretation.
> **Not covered:** production approval, original-data disaster recovery, and physical Telegram deletion. See [Project Status](IMPLEMENTATION_STATUS.md).

## 1. Workflow matrix

GitHub Actions does not support workflow folders in its sidebar. The numeric prefixes in display names group related checks; workflow files live under [`.github/workflows/`](../.github/workflows/).

| Group | Workflow | Validates |
| --- | --- | --- |
| 01 · Quality | [Security & Regression](../.github/workflows/quality-security.yml) | Python regression, security checks, dependencies, Docker image and listener smoke tests |
| 02 · Integration | [Databases](../.github/workflows/integration-databases.yml) | PostgreSQL/MySQL, native backups and isolated restores |
| 02 · Integration | [REST & S3](../.github/workflows/integration-storage.yml) | API contracts, signed always-Enabled versioning and all-private Public Access Block (real boto3), SigV4, AWS CLI, rclone and shared storage catalog |
| 02 · Integration | [Desktop](../.github/workflows/integration-desktop.yml) | Windows/Linux desktop, CLI and packaging |
| 03 · Resilience | [Docker Fault Injection](../.github/workflows/resilience-docker.yml) | Disposable PostgreSQL, two Web instances, crash/restart, corruption and recovery |
| 04 · Live Telegram | [Bot API E2E](../.github/workflows/live-telegram-bot.yml) | Real test-channel Bot uploads, readback, hashes, ranges and cleanup |
| 04 · Live Telegram | [User MTProto E2E](../.github/workflows/live-telegram-user.yml) | Real Telethon user session, checksum and range recovery |
| 04 · Live Telegram | [Multiworker PostgreSQL E2E](../.github/workflows/live-telegram-multiworker.yml) | Three independent workers sharing PostgreSQL and a real Telegram test target |
| 05 · Delivery | [Release Gate & Packages](../.github/workflows/delivery-release.yml) | Release regression, gated Linux/Windows packages and publishing |
| 06 · Endurance | [Isolated Capacity Soak](../.github/workflows/endurance-capacity.yml) | Manually dispatched 60–110-minute synthetic-media SQLite durability test |

The **first nine workflows** form the normal `main` validation matrix. The endurance workflow is deliberately manual (`workflow_dispatch`). The live Telegram workflows require dedicated **test** credentials; synthetic Docker validation does not.

### Interpreting CI results

- **Success:** the specified checks passed at the workflow's exact commit SHA. It is not evidence for a later SHA or for production operation.
- **Failure:** inspect the failing step and logs, fix the cause, and run checks on the updated SHA.
- **Cancelled, queued, or skipped:** not a successful acceptance result. An optional test case skipped inside a successful workflow is also not evidence that the optional scenario ran.
- **Release gate vs. published release:** the gate can succeed without publishing binaries. Packaging and GitHub Release creation follow their explicit tag/`[publish-release]` rules and release preconditions.
- **Synthetic vs. real Telegram:** AWS CLI/rclone compatibility and Docker fault injection use fake remote Telegram I/O with real SQL. Separate live Telegram workflows verify actual remote bytes.
- **Production sign-off:** requires a different, owner-authorized evidence process documented in [Project Status](IMPLEMENTATION_STATUS.md#3-production-acceptance-not-yet-signed-off).

## 2. Run checks locally

Python 3.11+, the appropriate dependencies, and optional Docker Engine/Compose v2 are required.

~~~bash
python -m pip install -r requirements/server.txt pytest
python -m pytest -q
python -m compileall -q teledrive web app tests
docker compose config --quiet
~~~

For the desktop application, install `requirements/desktop.txt` separately. Docker integration jobs install the tools and database services they need inside disposable runners; they do not require a pre-existing TeleDrive deployment.

### One-command Docker fault-injection acceptance

~~~bash
bash scripts/run_docker_fault_injection.sh
# Optional interactive operator wrapper:
python scripts/acceptance.py
~~~

**Purpose:** development acceptance, not a simulated production cutover. The runner creates a randomly named, isolated Compose project with **PostgreSQL 17, two TeleDrive Web containers, and a fault-scenario container** using actual SQLAlchemy and `TelegramObjectStorage`. The Telegram side is persistent, disk-backed synthetic media. Test credentials, a deployed production instance, an original Telegram account and a legacy folder are **not required**.

The **normal production Compose** has only **one** `teledrive` container. The integration suite also tests optional lease-fenced legacy source ingestion through that same WebUI/REST/S3 catalog with no direct Telethon duplicate send. Its single Python process exposes WebUI/REST on port 8080 and S3 on port 9000. Two separate Web containers in **this fault test** deliberately exercise shared-state behavior and crashes; they are not the deployment layout.

## 3. Fault-injection acceptance gates

All 13 gates and the teardown must pass. The identifiers are evidence labels, not development feature names.

| Gate | Fault or operation | Acceptance requirement |
| --- | --- | --- |
| DEV-01 | Fresh PostgreSQL + two Web instances | Both authenticated health checks work; anonymous access fails |
| DEV-02 | 0 B, 1 B, 4,097 B, 65,537 B, and 5 MiB + 7 B | Durable SQL heads/manifests match the disk-backed chunks |
| DEV-03 | Remove local source after upload | Full SHA-256 and cross-chunk range readback match |
| DEV-04 | Lost Telegram ACK / SQL crash | Persisted send intent prevents unsafe duplicate sends |
| DEV-05 | Aged failed object / GC guard | Unknown ACK remains protected; physical deletion is not enabled |
| DEV-06 | Synthetic Telegram HTTP 429 | SQL send slot reserved first; cooldown/metrics shared across nodes |
| DEV-07 | Real `pg_dump` and isolated `pg_restore` | Object IDs, chunk offsets, SHA-256 and range reads survive |
| DEV-08 | Corrupt SQL archive | Native restore validation rejects the damaged dump |
| DEV-09 | Corrupt remote bytes | Hash mismatch never publishes partial output; repair permits readback |
| DEV-10 | Isolated project teardown | Only the randomly named Compose project and its volumes are removed |
| DEV-11 | Web `SIGKILL` and PostgreSQL restart | Head, send fence, cooldown and remote bytes remain recoverable |
| DEV-12 | Missing chunk / second-chunk timeout | Existing destination remains intact; repair restores checksums |
| DEV-13 | Overall acceptance and cleanup | Four verified stages, all 13 gates and successful scoped cleanup |

### Execution flow

1. Start fresh database and Web instances; exercise real application configuration parsing and authenticated endpoints.
2. Upload size-boundary fixtures, persist immutable manifests, remove sources, and verify checksums and byte ranges.
3. Inject an uncertain upload acknowledgment and a rate limit after reserving a SQL send slot.
4. Kill `web-a` and restart PostgreSQL; assert shared persistent state from reconnected workers.
5. Dump and restore into an **independent** database; reject a deliberately corrupted dump.
6. Inject remote-media corruption, a missing chunk and an interrupted second chunk; confirm fail-closed output behavior.
7. Collect `seed.json`, `restarted.json`, `restored.json` and `acceptance.json` and complete narrowly scoped teardown.

### Artifacts and evidence

Reports are written under `.docker-fault-evidence/docker-fault-<random>.json` by default; `TELEDRIVE_FAULT_REPORT_DIR` can override the directory. Evidence files use permission mode `0600` and must not contain passwords, Bot tokens, Telegram sessions or database credentials. The runner uses only its own project-scoped `docker compose down -v`—never `docker system prune` or global volume cleanup.

The following is a **schema illustration, not an actual run result**:

~~~json
{
  "mode": "disposable-docker-development",
  "result": "PASS",
  "docker_project_removed": true,
  "production_data_used": false,
  "real_telegram_tested": false,
  "legacy_cutover_required": false,
  "production_acceptance_signed_off": false
}
~~~

An actual PASS requires the complete four-stage evidence and `stages.acceptance.checks_count == 13` with `all_checks_verified == true`. Cleanup failure or a missing gate is a failure.

## 4. Regression invariants

- The Bot API's shared cooldown must be preceded by `reserve_send_slot`; tests cannot bypass send fencing to manufacture a passing 429.
- The Web `/api/health` endpoint alone does not validate full configuration; fault tests use the actual `load_config` parser, including `sleep_min_seconds`, `sleep_max_seconds`, `max_files_per_run` and `max_files_per_day`.
- `tests/test_unified_service.py` verifies REST-to-S3, S3-to-REST, local-job-to-S3 data visibility and the shared generation/catalog. The Security workflow checks both listeners **inside one container**.
- Live Bot, User and Multiworker E2E are distinct from synthetic fault tests. A generated 429 is not a measured production rate limit.
- Normal Security and Storage CI run adversarial GC reference-graph tests. The live User MTProto E2E job creates one owned Telegram marker fixture, validates bounded read-only history inventory, reconciles independently known marker coordinates, creates a fresh isolated catalog after full byte/SHA verification, then cleans up only its own fixture. This does not exercise production garbage collection.
- Offline marker inventory and independent-plan reconciliation tests cover bounded Telegram history reads, forged/non-media messages, scan truncation, duplicate ordinals and private non-overwriting reports; discovery is not a recovery plan or full historic inventory.
- Offline capacity soak tests real SQLite operations, durable synthetic media, SQL restarts and historical readback. It does not exercise original Telegram media, independent physical hosts or real-world long-term Telegram behavior.

## 5. Source layout and related guides

| Path | Role |
| --- | --- |
| [`.github/workflows/`](../.github/workflows/) | GitHub Actions YAML definitions |
| [`.github/ci/fault-injection/`](../.github/ci/fault-injection/) | Test-only Dockerfile, ignore rules and Compose project |
| [`docker-compose.yml`](../docker-compose.yml) | **One-container** default WebUI/REST/S3 deployment |
| [`scripts/run_docker_fault_injection.sh`](../scripts/run_docker_fault_injection.sh) | Fault test entrypoint |
| [`scripts/docker_fault_scenarios.py`](../scripts/docker_fault_scenarios.py) | Synthetic fault scenarios and evidence |
| [`requirements/server.txt`](../requirements/server.txt) | Headless API/runtime dependencies |
| [`requirements/desktop.txt`](../requirements/desktop.txt) | Desktop GUI dependencies |

For feature status and production release boundaries, see [Project Status](IMPLEMENTATION_STATUS.md). For actual database backups and restore procedures, see [Backup and Restore](BACKUP_RESTORE.md). For security reporting, see [Security Policy](SECURITY.md).

**S3 P2-0 selector gate:** `tests/test_s3_route_guard.py` is mandatory in both Quality and REST/S3 workflow regressions. It verifies 116/116 operation statuses in the machine-readable inventory against `docs/S3_SPECS.md` and rejects unrecognized signed bucket/object selectors without exposing or mutating data.

**User MTProto unified transport:** `tests/test_storage_user_mode.py` checks signed S3 user-MTProto write/readback, old Bot-object fallback, unknown-ACK SQL fencing, owner isolation and fail-closed transport selection. The ASGI user session has its own encrypted secret handling outside the repository; separate real user E2E tests exercise account/chunk behavior but do not prove live REST/S3 user-mode deployment. Do not use a bot MTProto session to claim `getHistory` support.

**Multipart listing parity:** `tests/test_s3_multipart.py` verifies scoped SQL-bounded ListParts/ListMultipartUploads cursors, sparse/replaced part metadata, timestamps, grouping, URL encoding, invalid requests and actual boto3 over live HTTP. These are developer/test-channel gates; physical GC, recovery and owner sign-off use [Local Agent Acceptance](LOCAL_AGENT_ACCEPTANCE.md).

**ListBuckets pagination contract:** `tests/test_s3_bucket_listing.py` exercises SigV4/boto3 end-to-end, SQL creation dates, 1–10,000 page bounds, prefix and region filters, cursor integrity and credential scoping. Quality and REST/S3 workflows both require it.
