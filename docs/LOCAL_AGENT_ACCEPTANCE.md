# Local Agent Mission — Real-Data Docker, Recovery, GC Safety and Owner Acceptance

> **Give this document to the agent running on the owner's machine.** Repository: `babico/TeleDrive`. This is an executable engineering assignment, **not** permission to operate on production or to delete Telegram history. All written evidence, source code, and documentation must be in English. Perform the work autonomously on this host wherever possible, test it, and report objective results; ask for owner secrets/authorization only when essential.

## 0. Non-negotiable boundaries

1. Work on a **fresh dedicated clone and dedicated disposable environment**, never on an existing production `data/`, `config/`, database, Telegram channel or `~/.ssh` keys. Name all Docker Compose projects with a unique prefix; never run `docker system prune`, global image/volume deletion, `git reset --hard`, or blanket Telegram message deletion. Clean up only fixtures explicitly created and recorded by this run.
2. **No automatic physical Telegram GC** and no mutating original-media recovery. Unknown acknowledgments, orphan-looking messages, removed versions, aliases, historical copies, multipart parts, active write leases, and Bot file IDs with uncertain origin are all **protected**. If a complete owner-approved independent payload backup and restore proof is absent, stop at a signed dry-run plan.
3. Use a **new private test channel** with an authorized USER Telethon session and posting rights (preferred), or the existing Bot transport for compatibility. Test the optional `TELEDRIVE_STORAGE_TRANSPORT=telethon` path separately from Bot fallback: create a private `0600` session via `scripts/create_storage_user_session.py`, verify channel history after a forced lost-send ACK, restore readback across restart, and preserve old Bot chunks if supplied. Do not run two active clients on one Telethon session. Bot-only installs cannot enumerate arbitrary history. If the owner authorizes a *read-only* inspection of an original channel, separate it entirely from test fixtures. No test is allowed to manufacture or substitute an original `bucket`, `key`, `versionId`, full SHA-256, machine identity, original-media claim, Telegram HTTP 429, or approval signature.
4. Keep credentials in protected `0600` private files or a secret manager; do not expose tokens, sessions, Bot file IDs, SQL URLs, chat IDs or actual media filenames in commit messages, logs, CI artifacts or GitHub issues. Use redacted, integrity-checked evidence only.
5. A **single Docker host, even with several containers, is not two physically independent hosts**. A fabricated 429 and fake Telegram transport are not rate-limit/real-media evidence. Two owner/operator approvals require two real independent people. On unmet gates, report `BLOCKED_OWNER_ACTION` — never mark TODO items passed.

## 1. Discovery and isolated installation

From the trusted owner-controlled host:

```bash
git clone https://github.com/babico/TeleDrive.git TeleDrive-local-audit
cd TeleDrive-local-audit
git fetch origin
git checkout main
git rev-parse HEAD | tee /tmp/teledrive-reviewed-sha.txt
docker version
docker compose version
python3 --version
umask 077
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements/server.txt pytest boto3
python -m compileall -q teledrive web app tests
python -m pytest -q
docker compose config --quiet
bash scripts/run_docker_fault_injection.sh
```

Use Docker only on the explicitly approved test instance. Run the normal single-service `docker-compose.yml` **separately** from the two-Web fault-injection fixture: normal WebUI/REST port 8080 and S3 port 9000 must live inside **one `teledrive` container**, sharing one SQL catalog and Telegram object store.

Provision test-only configuration from `README.md` section 1 and 4. **Check existing `docker ps -a --format '{{.Names}}'` and port 8080/9000 owners first:** normal Compose uses global `container_name: teledrive`. Do not reuse, replace, stop or rename any existing installation to make this test fit. If there is a collision, use a genuinely separate VM/host or stop with a request for owner direction. The fault runner uses isolated Compose project resources:

```bash
cp -n .env.example .env
chmod 600 .env
mkdir -p config data uploads
# Set a unique long TELEDRIVE_WEB_PASSWORD, PUID/PGID, and ONLY
# disposable TELEDRIVE_STORAGE_BOT_TOKEN / TELEDRIVE_STORAGE_BOT_CHAT_ID.
# Create config/config.yaml and config/s3-master.key according to README.
docker compose up -d --build
docker compose ps
docker compose logs --tail=120 teledrive
```

The agent must verify loopback-only bindings, secret-file permissions, startup readiness, admin authentication, denied unauthenticated access and a unique catalog identity. **Do not guess or overwrite missing secret material.** If Telegram test credentials are missing, finish SQL/Docker synthetic work, mark live-media steps blocked, and request the minimum owner input.

Create a private evidence directory *outside* the Git checkout: `$HOME/teledrive-audit-evidence/<run-id>/`, permissions `0700`, receipts `0600`. Save OS/container versions, reviewed SHA, GitHub Actions URL, sanitized `docker compose ps`, CI and local test results and clean worktree diff.

## 2. Seed real bytes, not just metadata

Issue a test-only scoped S3 key via **WebUI → API Management → S3-compatible gateway**. Configure boto3 with the explicit `http://127.0.0.1:9000` path-style endpoint and SigV4; never record its secret. Implement a repeatable `scripts/local_owner_storage_matrix.py` (or tests), keeping random seed + SHA-256 manifest private. Create a unique bucket and deterministic test file paths. At minimum:

- Upload **0 B, 1 B, 4,097 B, 65,537 B, 5 MiB + 7 B, 16 MiB + 101 B** and at least one multi-chunk 32 MiB+ object if the test Bot target's limits allow. Mix incompressible random, repetitive and Unicode-named payloads. Avoid unbounded uploads or rate-limit floods.
- For each: PUT via S3; GET and HEAD via both S3 and REST; verify exact bytes, full SHA-256, stored object size, ETag consistency where applicable and cross-chunk `Range` (first/middle/boundary/last).
- Test two-version overwrite and explicit `VersionId` reads, delete marker, removed historical version, server-side CopyObject preserving source after logical deletion, and REST/S3/local-job visibility for one explicitly queued source file under the approved read-only `uploads/` mount.
- Exercise multipart `CreateMultipartUpload`, 5 MiB first part + final remainder, reordered/sparse/replaced parts, `ListParts` pages, `ListMultipartUploads` prefix/delimiter/URL encoding pages, `Complete`, `Abort`, `GetObjectAttributes/ObjectParts` and published-version reads using **real boto3**. Assert aborted, uncommitted and wrong-owner data remain unavailable.
- Repeat GET/HEAD/list pagination after container restart and an isolated SQL restore. Run storage under bounded concurrency with both same-key competing writers and distinct buckets. Compare the shared SQL HEAD/generation journal to actual bytes.
- Run negative cases: changed payload with old checksum, wrong SigV4, revoked credential, cursor tampering, unknown S3 controls, truncated Telegram transfer, missing remote chunk, stale multipart writer and ambiguous Bot ACK. **No failed case may publish an unverified head or delete remote media.**

All byte-level production-equivalent scenarios must actually contact the owner's **dedicated test Telegram Bot/channel**. The separate CI fake transport results should be labeled `synthetic`.

## 3. Independent backups and isolated disaster recovery

Review `docs/BACKUP_RESTORE.md` and `docs/IMPLEMENTATION_STATUS.md` sections 3–4 before executing. First generate and seal a consistent SQL snapshot and receipt:

```bash
python scripts/backup_metadata.py --database data/teledrive.db \
  --output "$EVIDENCE_DIR/sealed-test-sqlite.db"
sha256sum "$EVIDENCE_DIR/sealed-test-sqlite.db" > "$EVIDENCE_DIR/sealed-sqlite.sha256"
python scripts/verify_metadata_backup.py \
  --database "$EVIDENCE_DIR/sealed-test-sqlite.db" \
  --sha256 "$(cut -d' ' -f1 "$EVIDENCE_DIR/sealed-sqlite.sha256")"
```

For PostgreSQL, use `pg_dump -Fc` and `pg_restore` into a **new database**; for MySQL use transactionally consistent dump and independent restore. Execute `scripts/rehearse_native_database_restore.py` where applicable. Metadata backup **alone is not an independent media backup**.

Before any GC experiment, produce a **separate encrypted payload archive**, independent of both SQL and Telegram, with all bytes needed for *all* candidate media and referenced aliases/versions/parts. Manifest the full SHA-256/size and exact source relationships in private storage; verify archive integrity from an independent location and a separate read-only restoration process. Rehydrate archive bytes into a **new disposable test channel**, then prove full-body and range hash matches using a **new isolated SQL catalog**. Do not claim that Telegram message IDs can be restored after deletion; only a complete separately maintained plaintext archive can rebuild the logical content with new Telegram references.

For an owner-authorized **original** dataset, run the read-only original-media check separately, with the original *sealed* archive and its independent hash:

```bash
python scripts/rehearse_telegram_dr.py \
  --snapshot "$ORIGINAL_SEALED_DB" --sha256 "$ORIGINAL_SEALED_SHA256" \
  --bucket "$ORIGINAL_BUCKET" --key "$ORIGINAL_KEY" \
  --account "$ORIGINAL_ACCOUNT" --chat-id "$ORIGINAL_CHAT_ID"
```

Do not substitute synthetic fixtures for this command or reuse production DB paths. Record `BLOCKED_OWNER_ACTION` if original inputs or permissions do not exist.

## 4. History recovery and GC implementation acceptance

Read `teledrive/storage/gc.py`, `gc_safety.py`, `history_inventory.py`, `rebuild.py`, `docs/BACKUP_RESTORE.md`, and the existing tests. Extend actual implementation and tests rather than checking TODO boxes by inspection.

- Use the admin-only `/api/v1/admin/storage/channel-history` on the isolated USER transport with bounded `limit` and continuation `before_message_id`; `include_external_markers=true` may reveal Bot/admin posts but must label them unverified. Compare administrative read-only reports with the CLI report, and never convert marker hints into object ownership or deletion authority.
- Inventory one explicitly authorized channel **read-only** via `scripts/inventory_telegram_history.py --account TEST_ACCOUNT --target TEST_CHANNEL --limit 1000 --output "$EVIDENCE_DIR/history-private.json"`. Distinguish verified original message identity from weak filename/UUID markers. An unknown message without independent key, whole SHA, version or owner stays an **unresolved candidate**, never auto-published. Compare only against owner-supplied signed metadata and verified bytes; recover only into isolated catalog via `scripts/rebuild_from_telegram.py --help` and a validated plan.
- Construct a version-aware GC graph covering live HEADs, published/removed histories, delete markers, CopyObject aliases (including transitive), MPU active/completed/aborted parts, unindexed Bot ACKs/file IDs, inflight writer intents, leases, retention/hold rules, shared chat IDs and owner scope. Assert no planned physical message deletion if **any** potential reference/ownership doubt exists.
- Build a **deterministic dry-run plan** with message identity + expected media checksum, ref graph fingerprint, SQL generation, backup-set identity, prospective action count and no-op outcome. Test races (new alias/version/writer after planning), plan replay/reuse, stale lease, DB reconnect, interrupted deletion, unknown Telegram response, absent original identity, and duplicate/retried upload. Any race must stop/refresh the plan; avoid TOCTOU between final writer fence and the deletion.
- On this assignment's **isolated test-only fixture** and only after a verified external payload archive plus a successful isolated restore, you may implement and test a **separately gated one-message manual delete** of a message created by this run, if the owner explicitly authorizes this experimental action. Refuse all wildcard IDs, production targets, untracked candidates and any 2-person-approval bypass. Keep the production GC feature disabled unless a genuine reviewed authorization permits it.
- Rehearse rollback as **restoration of logical objects from independent payload backup**, *not* pretending deleted Telegram message IDs are resurrected. Recheck REST/S3 and historical versions with fresh persisted references. Unknown send acknowledgment => zero deletion.
- Run stress/endurance with rate budgets and backoff on a disposable test channel. Capture genuine Telegram responses and counters; **never synthesize HTTP 429 as real evidence and never induce abuse to force rate limiting**.

## 5. Owner-only acceptance — mandatory unresolved gates

These cannot be completed by a single local agent merely by starting containers:

| Gate | What proves it | If unavailable |
| --- | --- | --- |
| Physically independent hosts | Two different physical machines, independently obtained host identity hashes, same external SQL DB identity | `BLOCKED_OWNER_ACTION` |
| Authentic one-hour soak | `scripts/monitor_host_soak.py --output "$EVIDENCE_DIR/soak.json" --duration-seconds 3600 --interval-seconds 60` with genuine telemetry and required invariants | `BLOCKED_OWNER_ACTION` |
| Original-media DR | Original sealed archive, independent checksum, correct credentials/channel, real original bytes and range checks in isolated restore | `BLOCKED_OWNER_ACTION` |
| Legacy cutover/exemption | Real legacy source + verified owner-controlled cutover via `scripts/verify_legacy_cutover.py`, **or** independent signed no-legacy attestation | `BLOCKED_OWNER_ACTION` |
| Two-person bundle | Independent owner + operator Ed25519 signatures, matching reviewed SHA; verify with `scripts/verify_production_bundle.py --help` | `BLOCKED_OWNER_ACTION` |
| GC physical deletion | Independent plaintext-media backup, verified restore, exact target authorization, fenced dry-run, no ambiguous refs, two-person change approval | **DISABLED** |

Do not fabricate a release tag or publish binaries. For a new release candidate require the nine mandatory GitHub Actions workflows **green on its exact commit SHA**, review released binary hashes and supported protocol limits, then request release owner authorization. Development `main` does not retroactively update the v1.0.7 release.

## 6. Required deliverables and report

Create an English `docs/LOCAL_AGENT_ACCEPTANCE_RESULTS.md` **template** in the working branch (redacted, never real secrets) and a private `$EVIDENCE_DIR/report.json` with:
`release_sha`, `host_identity_redacted`, `compose_topology`, `fixture_seed`, `fixtures[{size,sha256,version_id_redacted}]`, `test_command`, `tests_passed`, `tests_failed`, `failure_reasons`, `sql_backup_sha256`, `payload_backup_sha256`, `restoration_verified`, `gates`, `blocked_owner_actions` and `garbage_collection_enabled:false`.

For each item in `TODO.md` **outside Advanced S3**, add evidence links and change `[ ]` to `[x]` **only after the exact acceptance criterion genuinely passes**. If evidence is missing, preserve the checkbox and append a specific blocker and safest next action. Submit reviewed code/tests/docs through one PR; do not merge failing CI or secretly change production policy. End with a table for every original TODO item: `PASS`, `FAIL`, `BLOCKED_OWNER_ACTION` or `NOT_APPLICABLE_ATTESTED`, evidence path and exact SHA.
