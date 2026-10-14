# P0 · development acceptance and supported release scope

**Status:** accepted for the bounded, implemented feature set at `a7e3adf` (2026-10-14). This is NOT production approval, unconditional full-S3 compatibility or a claim about the latest untested code.

## Acceptance criteria and evidence

1. **Single-SHA CI matrix:** Security, REST/S3 API, live PostgreSQL/MySQL, desktop Ubuntu/Windows, Telethon User E2E, Telegram Bot E2E, real PostgreSQL Multiworker E2E, disposable Docker Fault Injection and release verification all concluded success at `a7e3adf`. See the [Actions runs](https://github.com/babico/TeleDrive/actions).
2. **Crash atomicity and integrity:** persisted journals, fail-closed unknown Bot ACK, fenced concurrent writers, SHA-256 authenticated full and Range retrieval; the disposable 13-gate Docker campaign exercises PostgreSQL dump/restore, SIGKILL and bad/missing media.
3. **Credentials:** unauthorized/revoked/expired keys fail closed; REST and S3 credentials remain separate; signed writes are checksum validated and unsupported semantics rejected rather than silently accepted.
4. **Real Telegram:** dedicated Bot and User channel E2E and three independent database workers passed in CI at `a7e3adf`. Expensive 5/16/19 MiB multipart and soak were previously verified on opt-in live test-channel runs; they are not necessarily rerun on every push.
5. **Clients:** AWS CLI, boto3 and rclone supported within documented SigV4-compatible subset. Cyberduck is not certified.

Supporting runs: [Security](https://github.com/babico/TeleDrive/actions/runs/38565237975), [Storage API](https://github.com/babico/TeleDrive/actions/runs/38565238070), [Database](https://github.com/babico/TeleDrive/actions/runs/38565238030), [Desktop](https://github.com/babico/TeleDrive/actions/runs/38565237852), [Bot](https://github.com/babico/TeleDrive/actions/runs/38565237802), [User](https://github.com/babico/TeleDrive/actions/runs/38565237851), [Multiworker](https://github.com/babico/TeleDrive/actions/runs/38565237918), [Docker](https://github.com/babico/TeleDrive/actions/runs/38565237956), [Release gate](https://github.com/babico/TeleDrive/actions/runs/38565237938).

## Explicitly out of scope

Physical Telegram GC, full AWS S3 policies/lifecycle/SSE, all SDK features, complete metadata reconstruction from arbitrary historical Telegram messages, production-original DR, physical multi-host soak and signed legacy cutover.

Only sign off a new release candidate after its own CI matrix is successful. For the separate P1 development gate and future production-only gates, see [P1 Development Acceptance](P1_DEVELOPMENT_ACCEPTANCE.md) and [P1 Production Acceptance](P1_PRODUCTION_ACCEPTANCE.md).
