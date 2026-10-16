#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$REPO"
command -v docker >/dev/null
docker compose version >/dev/null
command -v python3 >/dev/null
if [[ -n "${TELEDRIVE_STORAGE_MTPROTO_SESSION_FILE:-}" || -n "${TELEDRIVE_STORAGE_MTPROTO_API_HASH:-}" ]]; then
  echo "Üretim MTProto session içeren shell'de DEV fault injection çalıştırma." >&2; exit 2
fi
ID="$(python3 -c 'import secrets;print(secrets.token_hex(6))')"
PROJECT="tdfault$ID"
WORKSPACE="$(mktemp -d "${TMPDIR:-/tmp}/teledrive-fault-disposable.XXXXXXXX")"
export TELEDRIVE_FAULT_WORKSPACE="$WORKSPACE" TELEDRIVE_FAULT_UID="$(id -u)" TELEDRIVE_FAULT_GID="$(id -g)"
export TELEDRIVE_FAULT_DB_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
export TELEDRIVE_FAULT_WEB_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
COMPOSE=(docker compose -f "$REPO/.github/ci/fault-injection/compose.yml" -p "$PROJECT")
REPORT_DIR="${TELEDRIVE_FAULT_REPORT_DIR:-$REPO/.docker-fault-evidence}"
if [[ -L "$REPORT_DIR" ]]; then echo "Symlink rapor dizini reddedildi" >&2; exit 2; fi
mkdir -p "$REPORT_DIR"; chmod 700 "$REPORT_DIR"
REPORT="$REPORT_DIR/docker-fault-$ID.json"
cleanup() {
  rc=$?
  trap - EXIT
  if [[ "$rc" -ne 0 ]]; then
    echo "DOCKER FAULT hata tanısı: yalnızca rastgele izole container logları:" >&2
    "${COMPOSE[@]}" logs --no-color --tail=35 web-a web-b postgres scenarios >&2 || :
  fi
  local docker_removed="false"
  if "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1; then
    docker_removed="true"
  else
    rc=1
    echo "İzole Compose projesi tam temizlenemedi; manuel kontrol gerekli." >&2
  fi
  local report_rc=0
  python3 - "$WORKSPACE/scenario/evidence" "$REPORT" "$rc" "$docker_removed" <<'PY' || report_rc=$?
import json, os, pathlib, sys
evidence, target, rc = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), int(sys.argv[3])
docker_removed = sys.argv[4] == "true"
receipts = {}
required = {"seed", "restarted", "restored", "acceptance"}
for stage in sorted(required):
    p = evidence / (stage + ".json")
    if p.is_file():
        receipts[stage] = json.loads(p.read_text(encoding="utf-8"))
complete = (
    rc == 0
    and docker_removed
    and set(receipts) == required
    and all(r.get("verified") is True for r in receipts.values())
    and receipts["acceptance"].get("all_checks_verified") is True
    and receipts["acceptance"].get("checks_count") == 13
)
payload = {"mode": "disposable-docker-development",
           "result": "PASS" if complete else "FAIL",
           "stages": receipts, "production_acceptance_signed_off": False,
           "real_telegram_tested": False, "legacy_cutover_required": False,
           "docker_project_removed": docker_removed, "production_data_used": False}
fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
with os.fdopen(fd, "w") as output:
    json.dump(payload, output, indent=2, sort_keys=True)
    output.write("\n")
print("DOCKER FAULT:", payload["result"], "| rapor:", target)
sys.exit(0 if complete else 1)
PY
  if [[ "$docker_removed" == "true" && "$WORKSPACE" == "${TMPDIR:-/tmp}"/teledrive-fault-disposable.* ]]; then
    rm -rf -- "$WORKSPACE"
  else
    echo "İzole test workspace saklandı: Docker teardown doğrulanmadı." >&2
  fi
  if [[ "$report_rc" -ne 0 ]]; then
    rc=1
  fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$WORKSPACE"/{config,web-a,web-b,uploads,scenario/{evidence,remote,sources}}
chmod 700 "$WORKSPACE"/{config,web-a,web-b,uploads,scenario,scenario/evidence,scenario/remote,scenario/sources}
python3 - "$WORKSPACE/config/config.yaml" <<'PY'
from pathlib import Path
import os,sys
from urllib.parse import quote
p=quote(os.environ["TELEDRIVE_FAULT_DB_PASSWORD"],safe="")
with Path(sys.argv[1]).open("x", encoding="utf-8") as out:
    out.write('telegram:\n  strategy: single\n  api_id: 12345\n'
              '  api_hash: "DISPOSABLE_OFFLINE_NO_SESSION"\n'
              '  target: "me"\n  accounts: []\n'
              'upload:\n  source_dir: "/uploads"\n'
              '  allowed_extensions: []\n  max_file_size_mb: 0\n'
              '  sleep_min_seconds: 60\n  sleep_max_seconds: 90\n'
              '  max_files_per_run: 12\n  max_files_per_day: 100\n'
              '  retry_attempts: 5\n  backoff_base_seconds: 10\n'
              '  floodwait_buffer_seconds: 5\n'
              '  caption_template: "{name}"\n  send_mode: "document"\n'
              'state:\n  database_url: "postgresql+psycopg://teledrive:%s@postgres:5432/teledrive_dev"\n'
              'logging:\n  log_path: "/data/uploader.log"\n  level: "WARNING"\n'
              'app:\n  auto_run: false\n  run_interval_minutes: 60\n' % p)
PY
echo "DOCKER FAULT: PostgreSQL 17 + iki sıfır TeleDrive Web container kuruluyor."
"${COMPOSE[@]}" config -q
"${COMPOSE[@]}" up --build --wait -d postgres web-a web-b
# A Uvicorn liveness response alone does not exercise config parsing.
# Validate the actual production loader on BOTH running containers before
# the expensive SQL + Telegram-byte fault scenario is seeded.
for node in web-a web-b; do
  "${COMPOSE[@]}" exec -T "$node" python -c 'from teledrive.uploader import load_config; c = load_config("/config/config.yaml"); assert c.db_path.startswith("postgresql+psycopg://")'
done
echo "DOCKER FAULT: 5MiB+ karışık boyut, sentetik 429/kayıp ACK, gerçek SQL/HTTP."
"${COMPOSE[@]}" run -T --rm scenarios seed

# Kill a real Web process and restart the shared PostgreSQL container.
# No production service or global Docker volume is touched.
echo "DOCKER FAULT: web-a SIGKILL + ortak PostgreSQL restart uygulanıyor."
"${COMPOSE[@]}" kill -s SIGKILL web-a
"${COMPOSE[@]}" restart postgres
"${COMPOSE[@]}" up -d --wait postgres web-a web-b
"${COMPOSE[@]}" run -T --rm scenarios restarted

BACKUP="$WORKSPACE/scenario/evidence/native-postgres.dump"
"${COMPOSE[@]}" exec -T postgres pg_dump -U teledrive -d teledrive_dev -Fc > "$BACKUP"
test -s "$BACKUP"
BAD="$WORKSPACE/scenario/evidence/tampered-postgres.dump"
cp "$BACKUP" "$BAD"
python3 - "$BAD" <<'PY'
from pathlib import Path
import sys
with Path(sys.argv[1]).open("r+b") as f: f.write(b"FAIL!")
PY
if "${COMPOSE[@]}" exec -T postgres pg_restore --list < "$BAD" >/dev/null 2>&1; then
  echo "Bozuk native SQL yedeği reddedilmedi" >&2; exit 1
fi
"${COMPOSE[@]}" exec -T postgres createdb -U teledrive teledrive_restore
"${COMPOSE[@]}" exec -T postgres pg_restore -U teledrive -d teledrive_restore --exit-on-error < "$BACKUP"
echo "DOCKER FAULT: bağımsız restore, SHA/Range ve medya bozulma enjeksiyonu."
"${COMPOSE[@]}" run -T --rm scenarios restored
echo "DOCKER FAULT: DEV-01..13 acceptance doğrulanıyor."
"${COMPOSE[@]}" run -T --rm scenarios acceptance
echo "DOCKER FAULT: PASS; yalnızca izole Compose projesi temizleniyor."
