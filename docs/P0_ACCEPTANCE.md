# P0 acceptance contract — supported release scope

Date: 2026-10-13. Target: `feature/docker-webui-multiaccount`.
This document is about a **bounded, explicitly tested** release candidate,
not a claim of complete S3 protocol or Cyberduck compatibility.

## Verified on previous commits

- Baseline seven workflow runs passed on `e3f086f33612235fb3c8982b2dee4e3c9b112f33`.
- Real bot upload and multipart Complete succeeded with non-skipped 5 MiB+
  Telegram E2E on `32bb3e850f2c5841cbcb2fa2525340ee25b6667b`,
  [run 38434063562](https://github.com/babico/TeleDrive/actions/runs/38434063562):
  `TELEDRIVE_E2E_MULTIPART=1`, two tests passed, with SQL restart,
  SHA-256 readback, journal reparenting, idempotent Complete and
  test-only message deletion.
- Security, Storage API, both Telegram user/bot E2E and release verify passed
  on `e88d4502cd2d25d186674d1551314075b4f8b98a`.
- The next P0 signoff commit should trigger the same-SHA seven workflows,
  including real multipart through its explicit `[p0-live-multipart]`
  commit marker; verify results before release/tagging.

## P0 safety acceptance

1. **Crash atomicity** — simulated pre/post Telegram ACK failures, a forced
   failure at the exact final SQL multipart publication commit, remote
   retrieval failure during Complete, SQLite restarts and stale writer
   fencing must never lose a published HEAD, make partial bytes visible,
   or delete Telegram messages without ownership verification.
2. **Credential isolation** — SQL-backed revoked, expired and read-only
   keys must fail closed for inappropriate operations across signed S3
   HTTP, checksum-bound presigned GET/PUT, multipart, copy and tagging.
   REST Bearer keys and S3 credentials are independent trust domains.
3. **Client interoperability** — real AWS CLI and rclone are the **tested
   supported S3 automation clients**, including directory sync and rclone
   forced 5 MiB multipart, with SHA-256 roundtrip. Large multichunk
   signed HTTP Range/HEAD checks cover boundaries and out-of-range errors.
   These are mandatory CI tests, not mocked client process signatures.
4. **Actual Telegram** — user and bot remote E2E both pass; the expensive
   real multipart test also passes on an explicitly opted-in merge/commit.
5. **Single-SHA check** — Security, Storage API, Database (MySQL and
   PostgreSQL), Desktop (Ubuntu and Windows), both Telegram E2E and
   release verification must all succeed at the candidate SHA.

## Explicit limitations — do not present these as shipped capabilities

- **Cyberduck / `duck` is not a validated P0 client.** It requires an
  [S3 HTTP connection profile](https://profiles.cyberduck.io/S3%20%28HTTP%29.cyberduckprofile)
  and [path-style S3 configuration](https://docs.cyberduck.io/protocols/s3/).
  A real Cyberduck UI or CLI end-to-end run is still needed before
  advertising Cyberduck compatibility. Track in P1 client acceptance.
- This is not AWS S3-complete: server-side encryption, bucket policy,
  lifecycle, version listing/restoration, presigned multipart POST and
  conditional multipart Complete remain unimplemented and fail closed.
- Automatic physical Telegram garbage collection is disabled. GC requires
  version/alias reference planning, completed ownership scans and explicit
  retention policy. Do not turn it on solely to make a CI checklist green.
- True system-process SIGKILL, live Telegram user-session revocation,
  mixed-worker soak, disaster recovery and installer acceptance need later
  dedicated staging and operations testing.
- CI verification on feature branches does **not** publish a release.
  Release tags require commit ancestry in `main`; main integration and
  production deployment remain distinct tasks.

## P1 handoff

Proceed with safe stale-PENDING reconciliation, persistent resumable upload
queue/job state and operator-facing recovery tooling, then optional S3
client/protocol expansion and eventual audited physical GC.


## Post-P0 feature development (2026-10-13)

Following P0 signoff, the active feature branch added SQL-fenced stale-PENDING
operator recovery, a root-confined persistent Telegram upload job queue,
opt-in worker scheduling, conditional multipart Complete, an immutable
published-version index, root-admin alias restore and signed historical
VersionId GET/HEAD, plus dry-run-only retention GC planning. Each needs the
latest feature commit's complete CI matrix before release. These are
feature-subset implementations, not a claim of production-grade full AWS
S3 parity, physical message GC or byte-accurate restart/resume.
