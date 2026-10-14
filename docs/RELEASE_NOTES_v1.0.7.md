# TeleDrive v1.0.7 · P0/P1 development milestone

This release contains the supported and CI-tested multi-account desktop/CLI/WebUI uploader, REST and partial S3 gateway, Telegram-backed immutable object manifests, checksum and Range validation, SQL job recovery, PostgreSQL/MySQL restore drills, and security hardening.

CI now groups workflows by Quality, Integration, Resilience, Live Telegram and Delivery. The disposable P1 Docker campaign exercises 13 development-acceptance gates, and separate User, Bot and Multiworker jobs exercise real Telegram **test-channel** transfers.

**Boundaries:** Production-original restore and physically independent host acceptance remain separate. Do not enable physical Telegram garbage collection. Full S3 lifecycle, SSE, bucket policies, reconstruction from unknown Telegram history and extra clients remain P2 work.

See [CI catalog](CI_WORKFLOWS.md), [P1 acceptance](P1_DEVELOPMENT_ACCEPTANCE.md), and [TODO](../TODO.md).
