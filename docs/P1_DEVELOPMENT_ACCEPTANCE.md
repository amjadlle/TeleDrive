# TeleDrive P1 development acceptance — disposable Docker

**Current required acceptance gate for development. No installed production environment is assumed.**
An active Telegram user StringSession, BotFather token, private channel ID,
real migration, production backup, natural 429 or deployed physical hosts
are NOT prerequisites for the P1 development acceptance campaign.

## One command

Run from the main branch after installing Docker Engine + Docker Compose v2:

```bash
git switch main
git pull --ff-only origin main
python scripts/p1_acceptance_wizard.py
```

Equivalent direct entrypoint:

```bash
bash scripts/run_docker_fault_injection.sh
```

The script creates a random project name, private workspace and random SQL/
HTTP passwords, builds **two real TeleDrive Web containers** plus a real
PostgreSQL 17 instance and a separate fault-scenario container. Its
Telegram transport is disk-backed and deterministic, not a live Telegram
API client. No external TCP ports are published. It must clean only its own
project and named DB volume, never run Docker system prune or touch normal
application data.

## DEV acceptance criteria (13/13 mandatory)

| Gate | Tested behavior | Required evidence |
| --- | --- | --- |
| DEV-01 | Clean PostgreSQL + two independent Web processes, Basic auth | Authenticated both pass; anonymous denied |
| DEV-02 | New objects 0B/1B/4097B/65537B/5MiB+7B | Real PostgreSQL HEAD/immutable manifests and disk-backed chunks |
| DEV-03 | Original local files removed | Full SHA and cross-chunk range readback |
| DEV-04 | Simulated post-send/pre-ACK crash | Durable pre-send fence survives; no blind duplicate upload |
| DEV-05 | Old FAILED manifests and GC protections | Unknown ACK retained; physical deletion disabled |
| DEV-06 | Synthetic 429 after reserved send slot | PostgreSQL cooldown/metric shared by both Web nodes |
| DEV-07 | Real PostgreSQL pg_dump and separate DB pg_restore | Independent full SHA, object identities, chunk offsets and Range |
| DEV-08 | Corrupt native archive | pg_restore --list rejects tampered dump |
| DEV-09 | Corrupt remote chunk | Checksum mismatch fails closed; bytes restored and readback passes |
| DEV-10 | Scoped cleanup | Only generated Compose project and volume removed |
| DEV-11 | Real Web SIGKILL + PostgreSQL service restart | Two Web nodes reconnect; persisted SQL/fences and media survive |
| DEV-12 | Missing remote chunk and mid-download timeout | No partial or overwritten destination; repair allows readback |
| DEV-13 | Machine checked combined acceptance | seed/restarted/restored/acceptance JSON all validated, 13 checks, no production signoff |

## How the campaign executes

1. Build and launch PostgreSQL + web-a + web-b. Validate the **real Web configuration loader** before beginning.
2. Create synthetic source files, upload using the real TelegramObjectStorage
   engine and disk-backed Telegram mock, publish HEADs, remove local sources
   and validate SHA/ranges.
3. Simulate a lost Bot ACK after a persisted pre-send intent. Assert retry
   remains fenced. Reserve a shared SQL slot before injecting a synthetic
   429; compare persisted metrics on both independent HTTP nodes.
4. SIGKILL web-a, restart PostgreSQL, recreate web-a and require both Web
   health/API/SQL endpoints to pass. Fresh test process reopens SQL, checks
   manifest identity and byte-level Telegram-simulation readback.
5. pg_dump real PostgreSQL, corrupt a separate dump copy, require native
   rejection, then pg_restore a newly created independent database. Verify
   every object by SHA and cross-chunk Range.
6. Corrupt a remote document, temporarily hide a referenced document and
   inject a timeout during second chunk download. Each failure must leave
   an existing destination byte-identical, and recovery must pass.
7. Combine four receipts, verify all 13 cases, stop/remove ONLY the random
   Compose project with volumes, and write a mode-0600 report.

## Acceptance result

The CLI report lives at `.docker-fault-evidence/docker-fault-<random>.json`
or under the optional `TELEDRIVE_FAULT_REPORT_DIR`. It must include:

```json
{
  "mode": "disposable-docker-development",
  "result": "PASS",
  "docker_project_removed": true,
  "production_data_used": false,
  "real_telegram_tested": false,
  "legacy_cutover_required": false,
  "production_acceptance_signed_off": false
}
```

These values are an **example of acceptance expectations**, not evidence of
a particular run. Each actual report must contain `stages.seed`,
`stages.restarted`, `stages.restored`, `stages.acceptance` and
`stages.acceptance.checks_count == 13`. No stage may be skipped; a nonzero
exit or failed scoped cleanup is an acceptance failure. GitHub Actions:
[TeleDrive Docker Fault Injection CI](../.github/workflows/docker-fault-injection-ci.yml).

## What is deferred until an actual deployment exists?

Actual multi-physical-host load with natural Telegram 429s, real Telegram
channel original-media recovery, live credential/session revocation and
owner-approved legacy read cutover remain **deployment/production** gates;
they are not asserted by this development simulation. Review
[P1 Production Acceptance](P1_PRODUCTION_ACCEPTANCE.md) only after a real
production installation is available. Live **test-channel** Telegram E2E
has its own separate GitHub workflows and is not replaced by this simulator.

Implementation details: [Docker Fault Injection](DOCKER_FAULT_INJECTION.md).
