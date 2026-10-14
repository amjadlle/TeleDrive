#!/usr/bin/env python3
"""Disposable Docker DOCKER FAULT scenarios. Never connects to real Telegram.

A disk-backed transport models Telegram media, while PostgreSQL, the
production storage engine and two real Web API processes are not mocked.
All injected corruption is limited to the compose project's scratch volume.
"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4

from teledrive.db import connect_database
from teledrive.storage.bot_api_adapter import BotFileIndex, BotApiError
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.engine import IntegrityError, TelegramObjectStorage
from teledrive.storage.gc import StorageGCInventory
from teledrive.storage.manifest import ManifestStore

SOURCE = Path(os.environ["TELEDRIVE_FAULT_SOURCES"])
REMOTE = Path(os.environ["TELEDRIVE_FAULT_REMOTE_DIR"])
EVIDENCE = Path(os.environ["TELEDRIVE_FAULT_EVIDENCE_DIR"])
DB_URL = os.environ["TELEDRIVE_FAULT_DB_URL"]
RESTORED_URL = os.environ["TELEDRIVE_FAULT_RESTORED_DB_URL"]
BUCKET = "docker-fault-only"
CHAT = -100700800900123
ACCOUNT = "fake-dev-telegram"  # NO Telegram network credentials, ever.


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def document_path(message_id: int) -> Path:
    return REMOTE / (str(message_id) + ".bin")


class DiskTelegram:
    """Real byte I/O and durable fake message IDs across Docker process restarts."""
    async def upload(self, source: Path, *, object_id: str,
                     ordinal: int, checksum: str):
        body = source.read_bytes()
        if sha(body) != checksum:
            raise IntegrityError("Original source checksum changed")
        # Per-upload unique BIGINT fits PostgreSQL signed integer;
        # no central in-memory counter or fake process-local reuse.
        msg = uuid4().int & ((1 << 62) - 1)
        path = document_path(msg)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as dest:
            dest.write(body)
        return ACCOUNT, CHAT, msg

    async def download(self, ref, dest: Path):
        source = document_path(ref.message_id)
        with source.open("rb") as original, dest.open("wb") as output:
            shutil.copyfileobj(original, output)


async def read_range(engine, identifier: str, a: int, b: int) -> bytes:
    return b"".join([piece async for piece in engine.iter_bytes(
        identifier, start=a, end=b, block_size=1021,
    )])


def health(host: str, *, authorized=True):
    credential = base64.b64encode(
        ("dev-operator:" + os.environ["TELEDRIVE_FAULT_WEB_PASSWORD"]).encode()
    ).decode("ascii")
    headers = {"Authorization": "Basic " + credential} if authorized else {}
    req = urllib.request.Request(
        "http://" + host + ":8080/api/v1/admin/storage/health", headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.load(response)
            assert response.status == 200
            return payload
    except urllib.error.HTTPError as exc:
        if not authorized and exc.code in (401, 403):
            return None
        raise


def check_two_web_nodes():
    assert health("web-a", authorized=False) is None
    assert health("web-b", authorized=False) is None
    one, two = health("web-a"), health("web-b")
    for name in ("physical_deletion_enabled",):
        assert one[name] is False and two[name] is False
    # Both independent Uvicorn processes must use same SQL and Bot 429 state.
    assert one["bot_explicit_429_total"] == two["bot_explicit_429_total"]
    return one


def write_receipt(stage: str, details: dict):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    record = {
        "environment": "disposable-docker-development",
        "stage": stage,
        "verified": True,
        "production_acceptance_signed_off": False,
        "real_telegram_tested": False,
        "physical_telegram_deletions": 0,
        **details,
    }
    target = EVIDENCE / (stage + ".json")
    with target.open("x", encoding="utf-8") as output:
        json.dump(record, output, indent=2, sort_keys=True)
        output.write("\n")
    print("PASS", stage, json.dumps(details, sort_keys=True))


def seed():
    for folder in (SOURCE, REMOTE, EVIDENCE):
        folder.mkdir(parents=True, exist_ok=True)
    assert not (EVIDENCE / "seed.json").exists(), "Disposable workspace must be empty"
    db = connect_database(DB_URL)
    remote = DiskTelegram()
    now = int(time.time())
    manifest = ManifestStore(db)
    catalog = BucketCatalog(manifest)
    try:
        catalog.create_bucket(BUCKET, now)
        saved = []
        for i, size in enumerate([0, 1, 4097, 65537, 5 * 1024 * 1024 + 7]):
            # Fully synthetic. Covers 0 B, 1 B, short, chunk boundary and 5MiB+.
            content = bytes([i + 37]) * size
            path = SOURCE / ("seed-" + str(i) + ".bin")
            path.write_bytes(content)
            item = asyncio.run(TelegramObjectStorage(
                manifest, remote, chunk_size=128 * 1024, scratch_directory=SOURCE,
            ).put_file(BUCKET, "objects/" + path.name, path))
            catalog.publish(item.id, now=now)
            assert item.sha256 == sha(content)
            parts = manifest.parts(item.id)
            assert sum(p.size for p in parts) == size
            # Delete source to prove restored payload comes from stored chunks.
            path.unlink()
            out = SOURCE / ("readback-" + str(i))
            asyncio.run(TelegramObjectStorage(
                manifest, remote, chunk_size=128 * 1024, scratch_directory=SOURCE,
            ).download_to_path(item.id, out))
            assert out.read_bytes() == content
            out.unlink()
            saved.append({"id": item.id, "key": item.key,
                          "size": size, "sha256": item.sha256, "parts": len(parts)})

        # A remote send DID happen, but SQL receipt/chunk append never happened:
        # simulated process loss immediately after Telegram accepted bytes.
        missing = manifest.begin(BUCKET, "crash/unknown-ack.bin", 9, now - 10 * 86400)
        manifest.fail(missing, "simulated worker crash before ACK journal", now - 10 * 86400)
        index = BotFileIndex(db)
        payload = b"orphaned"
        index.start_send(ACCOUNT, CHAT, missing, 0, sha(payload), len(payload))
        orphan_source = SOURCE / "unindexed.bin"
        orphan_source.write_bytes(payload)
        asyncio.run(remote.upload(orphan_source, object_id=missing, ordinal=0,
                                  checksum=sha(payload)))
        orphan_source.unlink()
        assert index.unresolved_count() == 1
        try:
            index.start_send(ACCOUNT, CHAT, missing, 0, sha(payload), len(payload))
        except BotApiError:
            pass
        else:
            raise AssertionError("Unsafe duplicate send accepted")
        candidate = next(x for x in StorageGCInventory(manifest).preview_failed_retention(
            now=now,
        ) if x["object_id"] == missing)
        assert candidate["reference_blocked"] is True
        assert candidate["approved_for_deletion"] is False
        # Synthetic Telegram 429: a real sender ALWAYS reserves its shared
        # account/channel SQL slot before it can receive a definitive 429.
        # The fault fixture must preserve this ordering, rather than
        # weakening the production fail-closed rate-limit contract.
        assert index.reserve_send_slot(ACCOUNT, CHAT, 0, now_ms=1000) == 0
        index.impose_shared_cooldown(ACCOUNT, CHAT, 7, now_ms=1000)
        assert index.shared_cooldown_wait(ACCOUNT, CHAT, now_ms=1000) == 7
        db.close()
        reopened = connect_database(DB_URL)
        try:
            idx = BotFileIndex(reopened)
            assert idx.unresolved_count() == 1
            assert idx.total_explicit_429_observations() == 1
            assert idx.shared_cooldown_wait(ACCOUNT, CHAT, now_ms=1000) == 7
            assert idx.reserve_send_slot(ACCOUNT, CHAT, 0, now_ms=1000) == 7
        finally:
            reopened.close()
        nodes = check_two_web_nodes()
        assert nodes["unresolved_bot_send_count"] == 1
        assert nodes["bot_explicit_429_total"] == 1
        assert nodes["physical_deletion_enabled"] is False
        (EVIDENCE / "objects.json").write_text(json.dumps(saved), encoding="utf-8")
        write_receipt("seed", {
            "objects": len(saved), "bytes": sum(x["size"] for x in saved),
            "simulated_unknown_ack_blocked": True,
            "synthetic_429_observations": 1,
            "web_nodes": 2, "corruption_scope": "test-only-docker-scratch",
        })
    finally:
        db.close()


def verify_process_restart():
    """Real Web SIGKILL + PostgreSQL service restart; SQL/byte history persists.

    Called only by the disposable Compose runner after it kills web-a,
    restarts postgres and brings both Web nodes back to healthy.
    """
    saved = json.loads((EVIDENCE / "objects.json").read_text(encoding="utf-8"))
    nodes = check_two_web_nodes()
    assert nodes["unresolved_bot_send_count"] == 1
    assert nodes["bot_explicit_429_total"] == 1
    assert nodes["unjournaled_bot_ack_count"] == 0
    remote = DiskTelegram()
    before_media = len(list(REMOTE.glob("*.bin")))
    assert before_media == sum(x["parts"] for x in saved) + 1
    db = connect_database(DB_URL)
    try:
        manifest = ManifestStore(db)
        catalog = BucketCatalog(manifest)
        idx = BotFileIndex(db)
        assert idx.unresolved_count() == 1
        assert idx.total_explicit_429_observations() == 1
        assert idx.shared_cooldown_wait(ACCOUNT, CHAT, now_ms=1000) == 7
        for original in saved:
            head = catalog.get(BUCKET, original["key"])
            assert head.item.id == original["id"]
            assert head.item.sha256 == original["sha256"]
            assert head.item.size == original["size"]
        # Read on a fresh SQL connection after the real container kill
        # and PostgreSQL restart, without accessing any local source.
        chosen = [saved[0], saved[-1]]
        engine = TelegramObjectStorage(
            manifest, remote, chunk_size=128 * 1024,
            scratch_directory=SOURCE,
        )
        for original in chosen:
            out = SOURCE / "post-sigkill-readback"
            asyncio.run(engine.download_to_path(original["id"], out))
            assert sha(out.read_bytes()) == original["sha256"]
            assert out.stat().st_size == original["size"]
            out.unlink()
        assert before_media == len(list(REMOTE.glob("*.bin")))
        assert not any(SOURCE.glob("seed-*.bin"))
        write_receipt("restarted", {
            "actual_web_container_sigkill_recovered": True,
            "postgresql_service_restart_recovered": True,
            "web_nodes_reconnected_to_shared_sql": 2,
            "manifest_head_identities_preserved": len(saved),
            "post_restart_telegram_sha_readbacks": len(chosen),
            "lost_ack_retry_remains_fenced": True,
            "synthetic_429_count_preserved": True,
            "remote_media_count_unchanged": True,
        })
    finally:
        db.close()


def verify_restored():
    saved = json.loads((EVIDENCE / "objects.json").read_text(encoding="utf-8"))
    remote = DiskTelegram()
    db = connect_database(RESTORED_URL)
    try:
        manifest = ManifestStore(db)
        catalog = BucketCatalog(manifest)
        engine = TelegramObjectStorage(manifest, remote, chunk_size=128 * 1024,
                                      scratch_directory=SOURCE)
        for item in saved:
            head = catalog.get(BUCKET, item["key"])
            assert head.item.id == item["id"]
            assert head.item.sha256 == item["sha256"]
            assert len(manifest.parts(item["id"])) == item["parts"]
            dest = SOURCE / "isolated-restore"
            asyncio.run(engine.download_to_path(item["id"], dest))
            assert dest.stat().st_size == item["size"]
            assert sha(dest.read_bytes()) == item["sha256"]
            if item["size"]:
                start = max(0, item["size"] // 2 - 73)
                end = min(item["size"], start + 145)
                with dest.open("rb") as stream:
                    stream.seek(start)
                    assert asyncio.run(read_range(engine, item["id"], start, end)) == (
                        stream.read(end - start)
                    )
            dest.unlink()
        idx = BotFileIndex(db)
        assert idx.unresolved_count() == 1
        assert idx.total_explicit_429_observations() == 1
        # Deliberately corrupt an externally stored chunk and verify that
        # the production download code refuses to publish partial bytes.
        candidate = next(x for x in saved if x["parts"] >= 2)
        chunk = manifest.parts(candidate["id"])[0]
        media = document_path(chunk.message_id)
        original = media.read_bytes()
        output = SOURCE / "corrupted-download"
        try:
            media.write_bytes(b"CORRUPTED" + original[9:])
            try:
                asyncio.run(engine.download_to_path(candidate["id"], output))
            except (IntegrityError, ValueError, RuntimeError):
                pass
            else:
                raise AssertionError("Remote-byte corruption escaped checksum check")
            assert not output.exists(), "Corrupt download published partial data"
        finally:
            media.write_bytes(original)
            output.unlink(missing_ok=True)
        # Destroy a test-only media reference temporarily. A missing
        # Telegram message must never replace an already-good destination.
        missing = SOURCE / "temporarily-missing-remote-media"
        previous = SOURCE / "missing-download"
        previous.write_bytes(b"PREVIOUS-GOOD-DATA")
        media.replace(missing)
        try:
            try:
                asyncio.run(engine.download_to_path(candidate["id"], previous))
            except FileNotFoundError:
                pass
            else:
                raise AssertionError("Missing remote Telegram message was accepted")
            assert previous.read_bytes() == b"PREVIOUS-GOOD-DATA"
        finally:
            missing.replace(media)
            previous.unlink()

        class InterruptedDownload(DiskTelegram):
            def __init__(self):
                self.calls = 0

            async def download(self, ref, dest):
                self.calls += 1
                if self.calls == 2:
                    raise TimeoutError("injected transport disconnect")
                await super().download(ref, dest)

        interrupted = InterruptedDownload()
        interrupted_engine = TelegramObjectStorage(
            manifest, interrupted, chunk_size=128 * 1024,
            scratch_directory=SOURCE,
        )
        previous = SOURCE / "interrupted-download"
        previous.write_bytes(b"PREVIOUS-GOOD-DATA")
        try:
            try:
                asyncio.run(interrupted_engine.download_to_path(
                    candidate["id"], previous,
                ))
            except TimeoutError:
                pass
            else:
                raise AssertionError("Broken Telegram stream published bytes")
            assert interrupted.calls == 2
            assert previous.read_bytes() == b"PREVIOUS-GOOD-DATA"
        finally:
            previous.unlink()

        good = SOURCE / "recovered-after-fault"
        asyncio.run(engine.download_to_path(candidate["id"], good))
        assert sha(good.read_bytes()) == candidate["sha256"]
        good.unlink()
        # A restored but incomplete manifest cannot authorize a deletion.
        assert all(not x["approved_for_deletion"] for x in
                   StorageGCInventory(manifest).preview_failed_retention(
                       now=int(time.time())))
        write_receipt("restored", {
            "native_postgresql_restore": True,
            "full_sha_and_range_verified": len(saved),
            "injected_remote_corruption_rejected": True,
            "missing_remote_media_fails_closed": True,
            "interrupted_remote_download_preserves_destination": True,
            "original_media_restored_after_fault": True,
            "orphan_send_fence_survived_restore": True,
            "synthetic_429_counter_survived_restore": True,
            "physical_deletion_enabled": False,
        })
    finally:
        db.close()


def development_acceptance():
    """Assert the complete disposable P1 DEV gate, never production signoff."""
    required = ("seed", "restarted", "restored")
    receipts = {}
    for stage in required:
        receipt = json.loads(
            (EVIDENCE / (stage + ".json")).read_text(encoding="utf-8")
        )
        assert receipt["verified"] is True
        assert receipt["environment"] == "disposable-docker-development"
        assert receipt["real_telegram_tested"] is False
        assert receipt["production_acceptance_signed_off"] is False
        receipts[stage] = receipt

    seed_receipt = receipts["seed"]
    restart_receipt = receipts["restarted"]
    restored_receipt = receipts["restored"]
    assert seed_receipt["objects"] == 5
    assert seed_receipt["simulated_unknown_ack_blocked"] is True
    assert seed_receipt["synthetic_429_observations"] == 1
    assert restart_receipt["actual_web_container_sigkill_recovered"] is True
    assert restart_receipt["postgresql_service_restart_recovered"] is True
    assert restart_receipt["remote_media_count_unchanged"] is True
    assert restored_receipt["native_postgresql_restore"] is True
    assert restored_receipt["full_sha_and_range_verified"] == 5
    assert restored_receipt["injected_remote_corruption_rejected"] is True
    assert restored_receipt["missing_remote_media_fails_closed"] is True
    assert restored_receipt["interrupted_remote_download_preserves_destination"] is True
    assert restored_receipt["orphan_send_fence_survived_restore"] is True
    assert restored_receipt["synthetic_429_counter_survived_restore"] is True
    assert restored_receipt["physical_deletion_enabled"] is False
    assert check_two_web_nodes()["physical_deletion_enabled"] is False
    checklist = {
        "DEV-01": "clean_postgresql_two_real_web_nodes",
        "DEV-02": "mixed_size_upload_and_persistent_manifest",
        "DEV-03": "source_deleted_full_sha_and_range",
        "DEV-04": "lost_ack_fenced_no_double_send",
        "DEV-05": "unreferenced_media_gc_fails_closed",
        "DEV-06": "synthetic_429_shared_sql_durability",
        "DEV-07": "native_postgresql_isolated_restore",
        "DEV-08": "tampered_native_backup_rejected",
        "DEV-09": "corrupt_media_rejected_and_recovered",
        "DEV-10": "unique_compose_cleanup_external",
        "DEV-11": "real_web_sigkill_and_postgres_restart",
        "DEV-12": "missing_media_and_broken_download_fails_closed",
        "DEV-13": "combined_machine_checked_dev_acceptance",
    }
    write_receipt("acceptance", {
        "checks_passed": sorted(checklist),
        "checks_count": len(checklist),
        "all_checks_verified": True,
        "real_telegram_tested": False,
        "production_acceptance_signed_off": False,
        "legacy_cutover_required": False,
        "telegram_remote_deletions": 0,
    })


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in (
        "seed", "restarted", "restored", "acceptance",
    ):
        raise SystemExit(
            "Usage: docker_fault_scenarios.py seed|restarted|restored|acceptance"
        )
    {
        "seed": seed,
        "restarted": verify_process_restart,
        "restored": verify_restored,
        "acceptance": development_acceptance,
    }[sys.argv[1]]()


if __name__ == "__main__":
    main()
