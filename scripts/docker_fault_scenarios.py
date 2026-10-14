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
        # Synthetic Telegram 429: shared SQL counter, not a real Telegram event.
        index.impose_shared_cooldown(ACCOUNT, CHAT, 7, now_ms=1000)
        db.close()
        reopened = connect_database(DB_URL)
        try:
            idx = BotFileIndex(reopened)
            assert idx.unresolved_count() == 1
            assert idx.total_explicit_429_observations() == 1
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
            "original_media_restored_after_fault": True,
            "orphan_send_fence_survived_restore": True,
            "synthetic_429_counter_survived_restore": True,
            "physical_deletion_enabled": False,
        })
    finally:
        db.close()


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("seed", "restored"):
        raise SystemExit("Usage: docker_fault_scenarios.py seed|restored")
    if sys.argv[1] == "seed":
        seed()
    else:
        verify_restored()


if __name__ == "__main__":
    main()
