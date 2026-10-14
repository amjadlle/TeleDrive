# CI workflow groups

GitHub Actions has a flat workflow sidebar rather than nested folders. The numeric prefixes group the nine workflows without moving YAML files, renaming job IDs, changing permissions, mutating concurrency groups, or changing test triggers. Each job appends a small coverage/outcome summary even when it fails.

| Group | Workflow | Purpose |
| --- | --- | --- |
| 01 | [01 · Quality | Security & Regression](../.github/workflows/security-ci.yml) | Offline regression; dependency and container security tests |
| 02 | [02 · Integration | Databases](../.github/workflows/database-ci.yml) | Live PostgreSQL/MySQL and native restore rehearsals |
| 02 | [02 · Integration | REST & S3](../.github/workflows/storage-api-ci.yml) | REST and S3 protocol; real AWS CLI and rclone processes |
| 02 | [02 · Integration | Desktop](../.github/workflows/desktop-ci.yml) | Windows and Linux UI, CLI and portable packaging |
| 03 | [03 · Resilience | P1 Docker Acceptance](../.github/workflows/docker-fault-injection-ci.yml) | Isolated 2-node API; PostgreSQL; crash, restore and teardown |
| 04 | [04 · Live Telegram | Bot API E2E](../.github/workflows/telegram-bot-e2e.yml) | Dedicated Telegram Bot test channel; checksum and cleanup |
| 04 | [04 · Live Telegram | User MTProto E2E](../.github/workflows/telegram-storage-e2e.yml) | Dedicated Telegram user session; byte readback and Range |
| 04 | [04 · Live Telegram | Multiworker PostgreSQL E2E](../.github/workflows/telegram-multiworker-e2e.yml) | Three SQL worker processes and real Telegram test channel |
| 05 | [05 · Delivery | Release Gate & Packages](../.github/workflows/release.yml) | Mainline release gate; no publishing on branch push; Linux and Windows tagged packages; Version-tagged GitHub release assets |

**01 Quality** runs offline regression and security checks. **02 Integration** covers data, REST/S3 protocols and desktop packaging. **03 Resilience** is the disposable P1 Docker 13-gate development acceptance (fake Telegram transport, real PostgreSQL/WebUI). **04 Live Telegram** uses dedicated test-channel credentials and performs real Telegram transfers. **05 Delivery** validates release readiness on `main` and only publishes artifacts when a release tag is pushed.

Production deployment signoff is separate: GitHub CI cannot prove original production-channel data, independent physical hosts or an approved legacy cutover. See [P1 development](P1_DEVELOPMENT_ACCEPTANCE.md) and [production-only acceptance](P1_PRODUCTION_ACCEPTANCE.md).
