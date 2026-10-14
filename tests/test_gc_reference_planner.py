"""GC never authorizes deletion from incomplete SQL, stale writers or version data."""
from __future__ import annotations

import time
from hashlib import sha256

import pytest
from sqlalchemy import insert
from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog, heads, published_versions, removed_versions
from teledrive.storage.gc import StorageGCInventory
from teledrive.storage.manifest import ManifestStore, object_aliases
from teledrive.storage.bot_api_adapter import BotFileIndex


@pytest.fixture
def cat(tmp_path):
    db = connect_database(str(tmp_path / "gc.db"))
    catalog = BucketCatalog(ManifestStore(db))
    catalog.create_bucket("gc-audit-test", int(time.time()))
    try:
        yield catalog
    finally:
        db.close()


def old_failure(cat, key="failed"):
    old = int(time.time()) - 9 * 86400
    ident = cat.manifests.begin("gc-audit-test", key, 0, old)
    cat.manifests.fail(ident, "simulated interruption", old)
    return ident


def one(cat, ident):
    data = StorageGCInventory(cat.manifests).preview_failed_retention(
        now=int(time.time())
    )
    return next(x for x in data if x["object_id"] == ident)


def test_even_unreferenced_manifest_has_no_deletion_permission(cat):
    row = one(cat, old_failure(cat))
    assert row["reference_blocked"] is False
    assert row["reference_blockers"] == []
    assert len(row["sql_reference_fingerprint"]) == 64
    for name in ("approved_for_deletion", "deletion_permitted",
                 "writer_barrier_verified", "independent_payload_backup_verified",
                 "restore_drill_verified", "two_person_deletion_approval_verified"):
        assert row[name] is False


@pytest.mark.parametrize("source", [
    "head", "published_version", "removed_version", "writer_fence",
    "copy_alias", "bot_send_intent", "bot_receipt",
])
def test_reference_classes_always_block_gc(cat, source):
    ident = old_failure(cat, source)
    now = int(time.time())
    db = cat.db
    if source == "head":
        db.connection.execute(insert(heads).values(
            bucket="gc-audit-test", key_hash=cat._keyhash(source),
            object_id=ident, generation=1, updated_at=now,
        ))
    elif source == "published_version":
        db.connection.execute(insert(published_versions).values(
            object_id=ident, bucket="gc-audit-test",
            key_hash=cat._keyhash(source), committed_ns=now * 10**9,
        ))
    elif source == "removed_version":
        db.connection.execute(insert(removed_versions).values(
            object_id=ident, removed_ns=now * 10**9,
        ))
    elif source == "writer_fence":
        cat.manifests.start_writer(ident, "unreconciled-writer", now)
    elif source == "copy_alias":
        child = cat.manifests.begin("gc-audit-test", "alias", 0, now)
        db.connection.execute(insert(object_aliases).values(
            object_id=child, source_object_id=ident,
        ))
    else:
        bot = BotFileIndex(db)
        digest = sha256(b"payload").hexdigest()
        bot.start_send("test", -100123, ident, 0, digest, 7)
        if source == "bot_receipt":
            bot.put_receipt("test", -100123, 77, "fixture-file-id",
                            object_id=ident, ordinal=0, checksum=digest,
                            size=7, require_intent=True)
    db.commit()
    result = one(cat, ident)
    assert result["reference_blocked"] is True
    assert source in result["reference_blockers"]
    assert result["deletion_permitted"] is False


def test_transitive_copy_alias_updates_fingerprint(cat):
    ident = old_failure(cat)
    first = one(cat, ident)
    now = int(time.time())
    alias = cat.manifests.begin("gc-audit-test", "child", 0, now)
    grandchild = cat.manifests.begin("gc-audit-test", "grandchild", 0, now)
    cat.db.connection.execute(insert(object_aliases), [
        {"object_id": alias, "source_object_id": ident},
        {"object_id": grandchild, "source_object_id": alias},
    ])
    cat.db.commit()
    later = one(cat, ident)
    assert later["copy_alias_descendants"] == 2
    assert later["sql_reference_fingerprint"] != first["sql_reference_fingerprint"]


def test_corrupt_alias_cycle_fails_closed(cat):
    ident = old_failure(cat)
    other = cat.manifests.begin("gc-audit-test", "other", 0, int(time.time()))
    cat.db.connection.execute(insert(object_aliases), [
        {"object_id": ident, "source_object_id": other},
        {"object_id": other, "source_object_id": ident},
    ])
    cat.db.commit()
    with pytest.raises(RuntimeError, match="alias graph is corrupt"):
        one(cat, ident)


def test_incomplete_removed_version_schema_is_unsafe(cat):
    ident = old_failure(cat)
    cat.db.connection.exec_driver_sql("DROP TABLE storage_removed_versions")
    cat.db.commit()
    with pytest.raises(RuntimeError, match="Incomplete GC reference"):
        one(cat, ident)
