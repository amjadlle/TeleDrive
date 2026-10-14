"""Rebuild only from original, fully checksummed Telegram chunk evidence."""
from __future__ import annotations
import asyncio
import hashlib
from uuid import uuid4
import pytest

from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore
from teledrive.storage.rebuild import restore_isolated_catalog


def sha(data): return hashlib.sha256(data).hexdigest()


class TelegramSource:
    def __init__(self, chunks): self.chunks = chunks; self.reads = 0
    async def probe(self, chunk, *, object_id):
        return "verified"

    async def download(self, ref, destination):
        self.reads += 1
        destination.write_bytes(self.chunks[ref.message_id])


def make_plan(parts):
    original = b"".join(parts)
    return {
        "object_id": str(uuid4()), "bucket": "rebuild-test",
        "key": "original/object.bin", "size": len(original),
        "sha256": sha(original),
        "chunks": [
            {"ordinal": i, "account": "original", "chat_id": -100123456,
             "message_id": 100+i, "size": len(data), "sha256": sha(data)}
            for i, data in enumerate(parts)
        ],
    }


@pytest.mark.parametrize("parts", [[], [b"hello"], [b"one", b"two" * 1000]])
def test_original_bytes_create_isolated_manifest_and_head(tmp_path, parts):
    plan = make_plan(parts)
    original = {100+i: p for i,p in enumerate(parts)}
    db_path = tmp_path / "recovered.db"
    result = asyncio.run(restore_isolated_catalog(plan, TelegramSource(original), db_path))
    assert result["verified"] and result["telegram_deletions"] == 0
    db = connect_database(str(db_path))
    try:
        catalog = BucketCatalog(ManifestStore(db))
        item = catalog.get("rebuild-test", "original/object.bin").item
        assert item.id == plan["object_id"] and item.sha256 == plan["sha256"]
        assert sum(x.size for x in catalog.manifests.parts(item.id)) == plan["size"]
    finally:
        db.close()


def test_original_media_corruption_never_creates_sql_db(tmp_path):
    plan = make_plan([b"known", b"good"])
    dest = tmp_path / "recovered.db"
    with pytest.raises(ValueError, match="hash/length"):
        asyncio.run(restore_isolated_catalog(
            plan, TelegramSource({100: b"known", 101: b"bad"}), dest))
    assert not dest.exists()


def test_rebuild_never_overwrites_existing_database(tmp_path):
    plan = make_plan([b"a"])
    dest = tmp_path / "existing.db"
    dest.write_bytes(b"original SQL data")
    with pytest.raises(FileExistsError):
        asyncio.run(restore_isolated_catalog(
            plan, TelegramSource({100:b"a"}), dest))
    assert dest.read_bytes() == b"original SQL data"


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(sha256="0"*64),
    lambda p: p["chunks"][0].update(ordinal=2),
    lambda p: p["chunks"][0].update(size=8),
    lambda p: p["chunks"].append(dict(p["chunks"][0])),
    lambda p: p.update(bucket="../prod"),
])
def test_rebuild_rejects_unproven_identity_metadata(tmp_path, mutation):
    plan = make_plan([b"hello"])
    mutation(plan)
    dest = tmp_path / "recovered.db"
    with pytest.raises((ValueError, TypeError)):
        asyncio.run(restore_isolated_catalog(
            plan, TelegramSource({100:b"hello"}), dest))
    assert not dest.exists()


def test_rebuild_rejects_unverified_remote_ownership_even_with_correct_bytes(tmp_path):
    plan = make_plan([b"exact-bytes"])
    class WrongOwner(TelegramSource):
        async def probe(self, chunk, *, object_id):
            return "mismatch"
    dest = tmp_path / "blocked.db"
    with pytest.raises(ValueError, match="ownership"):
        asyncio.run(restore_isolated_catalog(plan, WrongOwner({100:b"exact-bytes"}), dest))
    assert not dest.exists()
