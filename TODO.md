# Engineering Checklist

> **Purpose:** short, actionable development and release tasks. For decisions and evidence limits, see [Project Status](docs/IMPLEMENTATION_STATUS.md). For commands and CI contracts, see [CI and Test Guide](docs/CI_WORKFLOWS.md).

## 1. Verified milestones

- [x] Publish [v1.0.7](https://github.com/babico/TeleDrive/releases/tag/v1.0.7) from `747747652cd1ca36878453d49daf627d8c0a60ad` with Linux and Windows release assets.
- [x] Validate the bounded REST/S3/desktop storage feature set and 13-gate disposable Docker fault-injection acceptance.
- [x] Exercise real Bot API, user MTProto and multiworker PostgreSQL transfer scenarios against dedicated test Telegram targets.
- [x] Provide shared REST/S3/local-job object catalog and a single WebUI/REST/S3 Docker container.
- [x] Implement guarded metadata recovery, owner-verified isolated Telegram rebuild (atomic bounded multi-object batches), and the offline two-person production-evidence verifier.
- [x] Verify all nine standard GitHub Actions workflows successfully on development commit `aa44bd67e01495ad590af4460350cab1da8c28ac`. This does **not** change the contents of v1.0.7.

## 2. Engineering backlog

- [x] Test non-destructive GC SQL graph guards (including removed versions and writer fences) and real test-channel history inventory automatically in CI.
- [ ] Implement full version-aware, writer-fenced physical garbage collection **only after** sealed independent payload backup, ownership/alias/retention checks, an actual restore drill and explicit two-person approval.
- [x] Add a bounded, read-only marker inventory for one owner-authorized Telegram channel, with private output and synthetic regression coverage; candidate hints are not recovery proofs.
- [x] Compare history candidates against independently supplied bucket/key, full SHA and message identity; real CI performs isolated byte-verified restore of its own Telegram test fixture.
- [ ] Extend trustworthy recovery of unknown original Telegram history beyond candidate markers without inventing bucket/key, full SHA-256 or missing version metadata.
- [x] Align S3 documentation with CRC64NVME integration-test coverage; retain explicit limits for other checksum modes.
- [x] Count Bot file IDs without journal/receipt evidence in non-destructive GC audits.
- [ ] Complete tested S3 capabilities beyond the documented subset (for example lifecycle, policies, SSE and currently unsupported checksum modes).
- [ ] Establish longer-duration real-environment capacity benchmarks on owner-controlled infrastructure.

## 3. Operator-only production sign-off

- [ ] Collect independently verified identities from physically distinct deployment hosts sharing the intended SQL database.
- [ ] Rehearse original-media recovery using the owner's sealed SQL archive, Telegram credentials and real channel without overwriting production data.
- [ ] Conduct the authorized physical soak; capture genuine telemetry without deliberately flooding Telegram.
- [ ] Perform legacy cutover only when a real legacy source exists, otherwise obtain the explicit signed no-legacy exemption.
- [ ] Validate independent owner/operator approvals with `scripts/verify_production_bundle.py`; never infer sign-off from a synthetic CI fixture.

## 4. Release decision rules

- [ ] For any **new** release candidate, require all mandatory checks to pass on **that exact SHA**; previous green commits cannot be reused as proof.
- [ ] Review binaries, tag, release metadata and supported-feature limits before publication; do not silently publish unreleased `main` features as v1.0.7.
- [ ] Keep automatic physical Telegram deletion disabled until its independent safety conditions are verified.

**Reference:** [Project Status and Acceptance](docs/IMPLEMENTATION_STATUS.md) · [CI and Test Guide](docs/CI_WORKFLOWS.md) · [Backup and Restore](docs/BACKUP_RESTORE.md).
