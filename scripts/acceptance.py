#!/usr/bin/env python3
"""Interactive, fail-closed TeleDrive acceptance assistant.

Run as a file with "python scripts/acceptance.py". Do not pipe the
source to "python -": that consumes stdin and makes input() raise EOFError.

Produces a private, sanitized report. Never approves production, deletes
Telegram messages, changes routing, or applies unresolved MTProto receipts.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPO = "babico/TeleDrive"
WORKFLOWS = {
    "user": "04 · Live Telegram | User MTProto E2E",
    "user": "04 · Live Telegram | User MTProto E2E",
    "multiworker": "04 · Live Telegram | Multiworker PostgreSQL E2E",
}


def ask(prompt: str, default: str = "") -> str:
    suffix = (" [" + default + "]") if default else ""
    answer = input(prompt + suffix + ": ").strip()
    return answer or default


def secret(prompt: str) -> str:
    return getpass.getpass(prompt + " (gizli): ").strip()


def yes(prompt: str, default: bool = False) -> bool:
    value = ask(prompt + " (e/h)", "e" if default else "h").lower()
    if value not in ("e", "evet", "h", "hayir", "hayır"):
        raise ValueError("Yalnızca e/h kullanın")
    return value in ("e", "evet")


def safe_run(args, *, env=None, stdin=None, timeout=1800):
    # Captures outputs so DB URLs, Telegram ids and sensitive CLI diagnostics
    # are never echoed or copied verbatim to the evidence report.
    return subprocess.run(
        args, cwd=ROOT, env=env, stdin=stdin,
        capture_output=True, text=stdin is None, timeout=timeout, check=False,
    )


def file_digest(path: Path) -> str:
    hash_ = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            hash_.update(block)
    return hash_.hexdigest()


def output_file(directory: Path, prefix: str, suffix: str) -> Path:
    from uuid import uuid4
    return directory / (prefix + "-" + dt.datetime.now(dt.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    ) + "-" + uuid4().hex[:8] + suffix)


def report_status(result: dict, stage: str, state: str, reason: str):
    assert state in ("PASS", "BLOCKED", "FAIL", "NOT_APPLICABLE")
    result["stages"][stage] = {"status": state, "reason": reason}
    print("  [" + state + "] " + stage + ": " + reason)


async def session_login(session_path: Path) -> bool:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    print("\n[Telegram kullanıcı StringSession]")
    api_id = int(ask("API ID (my.telegram.org/apps)"))
    api_hash = secret("API Hash")
    phone = ask("Kullanıcı telefon numarası (+90...)")
    existing = session_path.read_text(encoding="utf-8").strip() if session_path.exists() else ""
    client = TelegramClient(StringSession(existing), api_id, api_hash)
    try:
        await client.start(
            phone=phone,
            code_callback=lambda: ask("Telegram giriş kodu"),
            password=lambda: secret("Telegram 2FA şifresi"),
        )
        identity = await client.get_me()
        if not identity or getattr(identity, "bot", False):
            raise ValueError("Bot değil Telegram kullanıcı hesabı gerekli")
        saved = client.session.save()
        if not saved:
            raise ValueError("Oturum kaydı oluşturulamadı")
        if not existing:
            session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(session_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as destination:
                destination.write(saved)
        # Never print session, phone, Telegram ID, hash or auth codes.
        print("  Kullanıcı girişi doğrulandı; session gizli dosyada saklanıyor.")
        return True
    finally:
        await client.disconnect()


def github_stage(result: dict):
    print("\n[1/6 GitHub E2E kanıt kontrolü]")
    if shutil.which("gh") is None:
        report_status(result, "github_e2e", "BLOCKED", "GitHub CLI yok")
        return
    output = safe_run([
        "gh", "run", "list", "--repo", REPO, "--limit", "100",
        "--json", "databaseId,headSha,workflowName,status,conclusion,url",
    ], timeout=45)
    if output.returncode:
        report_status(result, "github_e2e", "BLOCKED", "GitHub login/API listesi alınamadı")
        return
    try:
        records = json.loads(output.stdout)
    except (TypeError, ValueError):
        report_status(result, "github_e2e", "FAIL", "Geçersiz GitHub JSON cevabı")
        return
    passed = {}
    for key, name in WORKFLOWS.items():
        found = next((x for x in records if x.get("workflowName") == name
                      and x.get("conclusion") == "success"), None)
        if found:
            passed[key] = {"run_id": int(found["databaseId"]),
                           "sha": str(found["headSha"])[:12]}
    result["github"] = passed
    if len(passed) == len(WORKFLOWS):
        report_status(result, "github_e2e", "PASS", "Üç ayrı başarılı E2E koşumu bulundu")
    else:
        missing = ", ".join(sorted(set(WORKFLOWS) - set(passed)))
        report_status(result, "github_e2e", "BLOCKED", "Geçmiş başarılı koşum yok: " + missing)


def session_stage(result: dict, session_path: Path):
    print("\n[2/6 Telegram kullanıcı oturumu]")
    if session_path.exists():
        if session_path.is_symlink() or not session_path.is_file():
            raise ValueError("Session dosyası güvenli bir regular file değil")
        if session_path.stat().st_mode & 0o077:
            raise ValueError("Session dosyası izni fazla geniş; chmod 600 uygulayın")
        print("  Kayıtlı session dosyası mevcut. İçeriği gösterilmeyecek.")
    if not yes("Telegram kullanıcı oturumunu şimdi doğrula/oluştur?"):
        report_status(result, "telegram_session", "NOT_APPLICABLE",
                      "Oturum kontrolü operatör tarafından atlandı")
        return
    try:
        asyncio.run(session_login(session_path))
        report_status(result, "telegram_session", "PASS",
                      "Kullanıcı oturumu doğrulandı, gizli session ekrana yazılmadı")
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception as exc:
        report_status(result, "telegram_session", "BLOCKED",
                      "Telegram giriş kontrolü başarısız: " + type(exc).__name__)


def soak_stage(result: dict, evidence: Path):
    print("\n[3/6 Gerçek iki host / minimum bir saat / doğal 429]")
    if not yes("İki ayrı fiziksel VM/host ve TLS health URL'leri hazır mı?"):
        report_status(result, "multi_host_soak", "BLOCKED",
                      "İki ayrı canlı host yok; CI bu kabulü ikame edemez")
        return
    first = ask("Host A HTTPS origin (443, path yok)")
    second = ask("Host B HTTPS origin (443, path yok)")
    from scripts.monitor_host_soak import parse_hosts
    hosts = [{"label": "host-a", "url": first},
             {"label": "host-b", "url": second}]
    try:
        parsed = parse_hosts(json.dumps(hosts))
    except (ValueError, TypeError):
        report_status(result, "multi_host_soak", "FAIL", "Host URL/doğrulama hatası")
        return
    username = ask("Her iki host için Basic Auth kullanıcı adı")
    password = secret("Basic Auth parolası")
    if not username or not password:
        report_status(result, "multi_host_soak", "FAIL", "Yönetici kimlik doğrulaması eksik")
        return
    if not yes("Doğal iş yükü hazır mı? En az bir saat gözlem başlatılsın mı?"):
        report_status(result, "multi_host_soak", "BLOCKED", "1 saatlik gözlem başlatılmadı")
        return
    env = dict(os.environ)
    env.update({
        "TELEDRIVE_P1_HEALTH_HOSTS": json.dumps(hosts),
        "TELEDRIVE_P1_BASIC_USERNAME": username,
        "TELEDRIVE_P1_BASIC_PASSWORD": password,
    })
    target = output_file(evidence, "p1-soak", ".json")
    # Host names / passwords are never recorded in our master report.
    run = safe_run([
        sys.executable, "scripts/monitor_host_soak.py",
        "--duration-seconds", "3600", "--interval-seconds", "60",
        "--output", str(target),
    ], env=env, timeout=3900)
    if target.exists():
        try:
            content = json.loads(target.read_text(encoding="utf-8"))
            result["soak"] = {
                "sample_count": content.get("sample_count"),
                "duration_seconds": content.get("duration_seconds"),
                "telemetry_gate_passed": content.get("telemetry_gate_passed"),
                "final_shared_counter_agreement":
                    content.get("final_shared_counter_agreement"),
                "explicit_429_deltas_by_host":
                    content.get("explicit_429_deltas_by_host"),
            }
        except (OSError, ValueError):
            pass
    report_status(result, "multi_host_soak",
                  "PASS" if run.returncode == 0 else "BLOCKED",
                  "1 saatlik telemetry geçti; fiziksel host onayı ayrıca gerekli"
                  if run.returncode == 0 else
                  "429/backlog/TLS/host izleme kapısı tamamlanmadı")
    if parsed:
        print("  Host adresleri ve parolalar rapora yazılmadı.")


def ask_user_identity():
    print("  DR: ÖZGÜN Telethon USER session ve özgün mesajların kanalı gerekir.")
    session_file = Path(secret("Üretim USER StringSession dosya yolu")).expanduser()
    api_id = secret("Telegram API ID")
    api_hash = secret("Telegram API hash")
    account = ask("TeleDrive USER account alias (kullanıcı adı değil)")
    chat_text = secret("Orijinal Telegram kanal ID")
    if not re.fullmatch(r"-100[0-9]{5,16}", chat_text):
        raise ValueError("Telegram kanalı -100... biçiminde olmalı")
    chat_id = int(chat_text)
    target = secret("Kanal ID veya username (target)")
    bucket = ask("Mevcut S3 bucket")
    key = ask("Mevcut S3 object key")
    if not all((session_file, api_id, api_hash, account, chat_id, target,
                bucket, key)):
        raise ValueError("DR kimliği eksik")
    return session_file, int(api_id), api_hash, account, chat_id, target, bucket, key


async def native_telegram_readback(db_url, *, session_file, api_id, api_hash,
                                   target, account, chat_id, bucket, key,
                                   scratch: Path):
    """Read-only Telegram payload validation against restored native SQL."""
    from teledrive.db import connect_database
    from teledrive.storage.catalog import BucketCatalog
    from teledrive.storage.engine import TelegramObjectStorage
    from teledrive.storage.manifest import ManifestStore
    from scripts.rehearse_telegram_dr import (
        ReadOnlyUserChannelTransport, open_read_only_user,
    )
    db = connect_database(db_url)
    try:
        manifests = ManifestStore(db)
        obj = BucketCatalog(manifests).get(bucket, key).item
        if obj.size < 1 or obj.size > 64 * 1024 * 1024:
            raise ValueError("DR örneği 1B-64MiB aralığında olmalı")
        client, resolved = await open_read_only_user(
            session_file, api_id, api_hash, target, chat_id,
        )
        try:
            transport = ReadOnlyUserChannelTransport(
                client, resolved, account=account, chat_id=chat_id,
            )
            storage = TelegramObjectStorage(manifests, transport,
                                            scratch_directory=scratch)
            destination = scratch / "restored-telegram-object"
            await storage.download_to_path(obj.id, destination)
            if destination.stat().st_size != obj.size or file_digest(destination) != obj.sha256:
                raise ValueError("Telegram restore SHA/size uyuşmuyor")
            start = max(0, obj.size // 2 - 17)
            end = min(obj.size, start + 37)
            seen = b"".join([part async for part in storage.iter_bytes(
                obj.id, start=start, end=end, block_size=7,
            )])
            with destination.open("rb") as handle:
                handle.seek(start)
                if handle.read(end - start) != seen:
                    raise ValueError("Telegram restore Range uyuşmuyor")
            return {"verified": True, "size": obj.size, "sha256_match": True,
                    "range_match": True, "telegram_uploads": 0,
                    "telegram_deletions": 0}
        finally:
            await client.disconnect()
    finally:
        db.close()


def db_connection_parts(kind: str, name: str):
    print("  " + name + " DB hedefi; parolayı terminale yazdırmıyoruz.")
    host = ask(name + " hostname")
    port = int(ask(name + " port", "5432" if kind == "postgresql" else "3306"))
    dbname = ask(name + " DB adı")
    username = ask(name + " kullanıcı")
    password = secret(name + " DB parolası")
    if not host or not dbname or not username or not password or not 1 <= port <= 65535:
        raise ValueError("DB ayarları eksik")
    if any("\n" in v or "\r" in v for v in (host, dbname, username, password)):
        raise ValueError("DB ayarlarında satır sonu yasak")
    return dict(host=host, port=port, dbname=dbname, user=username,
                password=password)


def native_restore(kind: str, source: dict, target: dict,
                   backup: Path, workspace: Path) -> str:
    if (source["host"].lower(), source["port"]) == (
        target["host"].lower(), target["port"]
    ):
        raise ValueError("Aynı DB sunucusuna geri yükleme yasak")
    if source["dbname"] == target["dbname"] and source["host"].lower() == target["host"].lower():
        raise ValueError("Kaynak DB hedefi staging'den farklı olmalı")
    if backup.exists():
        raise ValueError("Yedek dosyası zaten mevcut")
    if kind == "postgresql":
        for executable in ("pg_dump", "pg_restore", "psql"):
            if not shutil.which(executable):
                raise RuntimeError("Gerekli PostgreSQL aracı yok: " + executable)
        def env_for(item):
            env = dict(os.environ)
            env.update({
                "PGHOST": item["host"], "PGPORT": str(item["port"]),
                "PGUSER": item["user"], "PGDATABASE": item["dbname"],
                "PGPASSWORD": item["password"],
            })
            return env
        source_env, target_env = env_for(source), env_for(target)
        check = safe_run(["psql", "-At", "-c",
                          "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')"],
                         env=target_env, timeout=30)
        if check.returncode or check.stdout.strip() != "0":
            raise RuntimeError("Staging PostgreSQL DB boş değil veya ulaşılamıyor")
        command = ["pg_dump", "-Fc", "--no-owner", "--no-acl", "-f", str(backup)]
        if safe_run(command, env=source_env, timeout=1800).returncode:
            raise RuntimeError("pg_dump başarısız")
        if safe_run(["pg_restore", "--no-owner", "--no-acl", "--exit-on-error",
                     "-d", target["dbname"], str(backup)],
                    env=target_env, timeout=1800).returncode:
            raise RuntimeError("İzole pg_restore başarısız")
        from sqlalchemy.engine import URL
        return URL.create(
            "postgresql+psycopg", username=target["user"],
            password=target["password"], host=target["host"],
            port=target["port"], database=target["dbname"],
        ).render_as_string(hide_password=False)
    for executable in ("mysqldump", "mysql"):
        if not shutil.which(executable):
            raise RuntimeError("Gerekli MySQL aracı yok: " + executable)
    def option_file(item):
        fd, name = tempfile.mkstemp(prefix="mysql-login-", suffix=".cnf", dir=workspace)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write("[client]\n")
            for key, val in (
                ("host", item["host"]), ("port", str(item["port"])),
                ("user", item["user"]), ("password", item["password"]),
            ):
                if '"' in val or "\\" in val:
                    raise ValueError("MySQL kimlik bilgisi güvenli option-file biçiminde değil")
                out.write(key + '="' + val + '"\n')
        return Path(name)
    original_opt = restored_opt = None
    try:
        original_opt = option_file(source)
        restored_opt = option_file(target)
        probe = safe_run([
            "mysql", "--defaults-extra-file=" + str(restored_opt),
            "--batch", "--skip-column-names", "--database=" + target["dbname"],
            "--execute=SHOW TABLES",
        ], timeout=30)
        if probe.returncode or probe.stdout.strip():
            raise RuntimeError("Staging MySQL DB boş değil veya ulaşılamıyor")
        dumped = safe_run([
            "mysqldump", "--defaults-extra-file=" + str(original_opt),
            "--single-transaction", "--routines", "--triggers", "--no-tablespaces",
            "--result-file=" + str(backup), source["dbname"],
        ], timeout=1800)
        if dumped.returncode:
            raise RuntimeError("mysqldump başarısız")
        with backup.open("rb") as stream:
            loaded = subprocess.run([
                "mysql", "--defaults-extra-file=" + str(restored_opt),
                "--database=" + target["dbname"],
            ], cwd=ROOT, stdin=stream, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=1800, check=False)
        if loaded.returncode:
            raise RuntimeError("İzole MySQL restore başarısız")
        from sqlalchemy.engine import URL
        return URL.create(
            "mysql+pymysql", username=target["user"],
            password=target["password"], host=target["host"],
            port=target["port"], database=target["dbname"],
        ).render_as_string(hide_password=False)
    finally:
        for path in (original_opt, restored_opt):
            if path is not None:
                path.unlink(missing_ok=True)


def dr_stage(result: dict, evidence: Path):
    print("\n[4/6 DR: özgün SQL yedeği + gerçek Telegram dosyası]")
    if not yes("Bu hostta orijinal DB'ye yetkili backup erişimin var mı?"):
        report_status(result, "disaster_recovery", "BLOCKED", "Üretim yedekleme yetkisi yok")
        return
    kind = ask("DB tipi (sqlite/postgresql/mysql)", "sqlite").lower()
    if kind not in ("sqlite", "postgresql", "mysql"):
        report_status(result, "disaster_recovery", "BLOCKED", "DB tipi desteklenmiyor")
        return
    backup = output_file(evidence, "p1-" + kind + "-backup", ".db" if kind == "sqlite" else ".dump")
    try:
        if kind == "sqlite":
            source = Path(ask("Orijinal SQLite DB mutlak dosya yolu")).expanduser()
            if not source.is_absolute() or not source.is_file():
                raise ValueError("Mevcut mutlak SQLite dosya yolu gerekli")
            if source.is_symlink():
                raise ValueError("SQLite kaynak symlink olmamalı")
            backup_result = safe_run([
                sys.executable, "scripts/backup_metadata.py",
                "--database", str(source), "--output", str(backup),
            ], timeout=300)
            if backup_result.returncode or not backup.is_file():
                raise RuntimeError("Online SQLite backup başarısız")
            checksum = file_digest(backup)
            check = safe_run([
                sys.executable, "scripts/verify_metadata_backup.py",
                "--database", str(backup), "--sha256", checksum,
            ], timeout=120)
            if check.returncode:
                raise RuntimeError("SQLite backup bütünlüğü doğrulanamadı")
            if not yes("Orijinal Telegram dosyasıyla full SHA ve Range testi yapılsın mı?"):
                report_status(result, "disaster_recovery", "BLOCKED",
                              "SQL backup doğrulandı fakat gerçek Telegram readback atlandı")
                return
            (session_file, api_id, api_hash, account, chat_id, target,
             bucket, key) = ask_user_identity()
            from scripts.rehearse_telegram_dr import verify_original_user_channel
            result_obj = asyncio.run(verify_original_user_channel(
                backup, checksum, bucket=bucket, key=key, account=account,
                chat_id=chat_id, target=target, session_file=session_file,
                api_id=api_id, api_hash=api_hash,
            ))
            if (result_obj.get("verified") is not True
                    or result_obj.get("telegram_uploads") != 0
                    or result_obj.get("telegram_deletions") != 0):
                raise ValueError("DR doğrulama sözleşmesi geçmedi")
        else:
            print("  İzole, boş staging DB aynı DB sunucusunda olmamalıdır.")
            src = db_connection_parts(kind, "ÜRETİM")
            dst = db_connection_parts(kind, "STAGING")
            if not yes("Staging DB'nin boş ve tamamen disposable olduğunu ONAYLIYOR musun?"):
                raise ValueError("Staging restore yetkisi verilmedi")
            with tempfile.TemporaryDirectory(prefix="p1-restore-conf-") as workspace:
                staging_db_url = native_restore(
                    kind, src, dst, backup, Path(workspace),
                )
                if not yes("Staging DB üzerinden orijinal Telegram dosyası doğrulansın mı?"):
                    report_status(result, "disaster_recovery", "BLOCKED",
                                  "Native SQL restore tamamlandı; Telegram okunmadı")
                    return
                (session_file, api_id, api_hash, account, chat_id, target,
                 bucket, key) = ask_user_identity()
                with tempfile.TemporaryDirectory(prefix="p1-telegram-readback-") as temp:
                    result_obj = asyncio.run(native_telegram_readback(
                        staging_db_url, session_file=session_file,
                        api_id=api_id, api_hash=api_hash, target=target,
                        account=account, chat_id=chat_id, bucket=bucket,
                        key=key, scratch=Path(temp),
                    ))
        result["dr"] = {"db_type": kind, "backup_checksum_matches": True,
                        "original_telegram_bytes_verified": True,
                        "range_verified": True,
                        "telegram_uploads": 0, "telegram_deletions": 0}
        report_status(result, "disaster_recovery", "PASS",
                      "İzole SQL backup/restore + gerçek Telegram SHA ve Range geçti")
    except (KeyboardInterrupt, EOFError):
        raise
    except Exception as exc:
        report_status(result, "disaster_recovery", "FAIL",
                      "Backup/restore/readback tamamlanamadı: " + type(exc).__name__)


def legacy_stage(result: dict):
    print("\n[5/6 Legacy cutover salt-okunur SHA/HEAD kontrolü]")
    if not yes("Eski uploader verisi yeni TeleDrive'a taşınıyor mu?"):
        report_status(result, "legacy_cutover", "NOT_APPLICABLE",
                      "Gerçek legacy migration hedefi yok")
        return
    if not yes("Eski uploader KAPALI, dosyalar değişmiyor, tüm jobs done ve lease serbest mi?"):
        report_status(result, "legacy_cutover", "BLOCKED",
                      "Eski işçi/source yazımı veya kuyruk hâlâ aktif")
        return
    try:
        db_value = secret("Üretim/staging SQL URL veya SQLite yolu")
        source = Path(ask("Legacy source-root (mutlak)")).expanduser()
        allowed = Path(ask("İzin verilen üst kök (mutlak)")).expanduser()
        scratch = Path(ask("Scratch dizini (mutlak)")).expanduser()
        (session_file, api_id, api_hash, account, chat_id, target,
         bucket, _) = ask_user_identity()
        prefix = ask("Hedef key prefix", "archived")
        max_files = int(ask("Maksimum dosya sayısı (1-1000)", "1000"))
        max_bytes = int(ask("Tek dosya maksimum byte", "67108864"))
        extensions = ask("Uygun uzantılar, virgülle; boş=tümü", "")
        if not (source.is_absolute() and allowed.is_absolute()
                and scratch.is_absolute() and scratch.is_dir()):
            raise ValueError("Güvenli mutlak kaynak/scratch yolları gerekli")
        from scripts.verify_legacy_cutover import run
        original_env = {}
        for key, value in (
            ("TELEDRIVE_STORAGE_MTPROTO_API_ID", str(api_id)),
            ("TELEDRIVE_STORAGE_MTPROTO_API_HASH", api_hash),
            ("TELEDRIVE_STORAGE_MTPROTO_SESSION_FILE", str(session_file)),
        ):
            original_env[key] = os.environ.get(key)
            os.environ[key] = value
        try:
            args = SimpleNamespace(
                database=db_value, source_root=source,
                allowed_root=allowed, scratch_dir=scratch, bucket=bucket,
                prefix=prefix, account=account, chat_id=chat_id,
                target=target, max_files=max_files, max_bytes=max_bytes,
                extensions=extensions, max_file_size_mb=0,
            )
            report = asyncio.run(run(args))
        finally:
            for key, value in original_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        result["legacy"] = {
            field: report.get(field) for field in (
                "verified", "eligible_files", "verified_files",
                "not_yet_published", "inconsistent", "excluded", "truncated",
                "telegram_uploads", "telegram_deletions",
                "source_modifications",
            )
        }
        report_status(result, "legacy_cutover", "PASS" if report["verified"] else "BLOCKED",
                      "Tam kaynak SHA ve Telegram HEAD doğrulaması geçti; read switch onayı AYRI"
                      if report["verified"] else "Cutover kaynağı eksik/uyumsuz")
    except (KeyboardInterrupt, EOFError):
        raise
    except Exception as exc:
        report_status(result, "legacy_cutover", "FAIL",
                      "Legacy ön kabul doğrulanamadı: " + type(exc).__name__)


def ack_stage(result: dict, session_path: Path):
    print("\n[6/6 MTProto lost-send / bağımsız kanal kanıtı]")
    if not yes("Sağlık API'sinde çözümlenmemiş MTProto gönderim intent'i var mı?"):
        report_status(result, "mtproto_ack", "NOT_APPLICABLE",
                      "Operatör belirsiz MTProto gönderimi bulunmadığını beyan etti")
        return
    if (not session_path.is_file() or session_path.is_symlink()
            or session_path.stat().st_mode & 0o077):
        report_status(result, "mtproto_ack", "BLOCKED",
                      "Güvenli, mode 0600 Telegram kullanıcı session dosyası yok")
        return
    if not yes("Orijinal message_id VE tam SHA-256 kanıtı mevcut mu?"):
        report_status(result, "mtproto_ack", "BLOCKED",
                      "Kanıt olmadan kör tekrar gönderim/ACK uygulanamaz")
        return
    try:
        from scripts.reconcile_mtproto_receipt import run
        args = SimpleNamespace(
            database=secret("Salt-okunur DR staging veritabanı yolu/URL"),
            account=ask("Orijinal USER account alias"),
            chat_id=int(secret("Orijinal özel kanal ID")),
            target=secret("Kanal ID veya username (target)"),
            object_id=ask("SQL pre-send intent object UUID"),
            ordinal=int(ask("Parça ordinal", "0")),
            message_id=int(secret("Orijinal Telegram message ID")),
            sha256=secret("Orijinal parça SHA-256"),
            size=int(secret("Orijinal parça byte boyutu")),
            apply=False,
        )
        print("  Sadece dry-run çalışır; --apply yok.")
        original = {}
        for key, value in (
            ("TELEDRIVE_RECONCILE_API_ID", ask("Telegram API ID")),
            ("TELEDRIVE_RECONCILE_API_HASH", secret("Telegram API Hash")),
            ("TELEDRIVE_RECONCILE_SESSION",
             session_path.read_text(encoding="utf-8").strip()),
        ):
            original[key] = os.environ.get(key)
            os.environ[key] = value
        try:
            verified = asyncio.run(run(args))
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        if not verified.get("verified") or verified.get("applied"):
            raise ValueError("Bağımsız MTProto kanıt doğrulaması geçmedi")
        result["ack"] = {"independent_channel_proof": True,
                         "applied": False, "telegram_uploads": 0,
                         "telegram_deletions": 0}
        report_status(result, "mtproto_ack", "PASS",
                      "USER kanal + tam SHA kanıtı doğrulandı; apply yapılmadı")
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception as exc:
        report_status(result, "mtproto_ack", "FAIL",
                      "Özgün MTProto kanıtı doğrulanamadı: " + type(exc).__name__)


def save_report(folder: Path, result: dict) -> Path:
    target = output_file(folder, "p1-acceptance", ".json")
    result["production_acceptance_signed_off"] = False
    result["operator_review_required"] = True
    result["physical_telegram_deletions"] = 0
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(result, file, sort_keys=True, indent=2, ensure_ascii=False)
        file.write("\n")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path,
                        default=Path.home() / "teledrive-p1-evidence")
    options = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("Interaktif terminal gerekli. 'python - <<PY' kullanmayın")
    os.umask(0o077)
    requested_evidence = options.evidence_dir.expanduser()
    if requested_evidence.is_symlink():
        parser.error("Kanıt dizini symlink olmamalı")
    evidence = requested_evidence.resolve()
    if evidence.is_symlink():
        parser.error("Kanıt dizini symlink olmamalı")
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    if evidence.stat().st_mode & 0o077:
        parser.error("Kanıt dizini izinlerini chmod 700 ile daraltın")
    session_path = Path.home() / ".config/teledrive/operator.session"
    run = safe_run(["git", "rev-parse", "HEAD"], timeout=15)
    result = {
        "schema_version": 1,
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "git_sha": run.stdout.strip() if run.returncode == 0 else "unavailable",
        "stages": {},
    }
    print("TeleDrive P1 ÜRETİM KABUL SİHİRBAZI")
    print("Sırlar maskeli kalır. Silme, upload, read cutover, SQL ACK apply YOK.")
    print("Repo:", REPO, "| Git SHA:", result["git_sha"][:12])
    try:
        github_stage(result)
        session_stage(result, session_path)
        soak_stage(result, evidence)
        dr_stage(result, evidence)
        legacy_stage(result)
        ack_stage(result, session_path)
    except (EOFError, KeyboardInterrupt):
        print("\nGirdi kesildi; şimdiye kadarki kanıt kaydediliyor.")
        result["interrupted"] = True
    finally:
        target = save_report(evidence, result)
        print("\nRAPOR YAZILDI:", target)
        print("Raporda token, session, parola, URL veya kanal ID yok.")
        print("Bu rapor tek başına üretim geçiş onayı DEĞİLDİR.")
        print("Çıktıyı (gizli bilgi bulunmadığını kontrol ederek) paylaşabilirsin.")


if __name__ == "__main__":
    # During development the default is a zero-credential, disposable Docker
    # acceptance campaign, never an interrogation about a nonexistent
    # production deployment. Opt into the original operator-only production
    # wizard explicitly once a real production deployment exists.
    if "--production" in sys.argv[1:]:
        sys.argv.remove("--production")
        main()
    elif len(sys.argv) == 1:
        raise SystemExit(subprocess.call([
            "bash", str(ROOT / "scripts" / "run_docker_fault_injection.sh"),
        ], cwd=ROOT))
    else:
        raise SystemExit(
            "Development: python scripts/acceptance.py\n"
            "Future operator stage: python scripts/acceptance.py --production"
        )
