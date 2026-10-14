"""Real SQL catalog tests for immutable version pointers and bucket behavior."""
from __future__ import annotations

from hashlib import sha256

import pytest

from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore


@pytest.fixture
def catalog(tmp_path):
    db = connect_database(str(tmp_path / "catalog.sqlite"))
    manifests = ManifestStore(db)
    try:
        yield BucketCatalog(manifests), manifests
    finally:
        db.close()


def ready(manifests, bucket, key, data, now):
    ident = manifests.begin(bucket, key, len(data), now)
    # Empty files are the only ones that can be published with no remote chunks.
    assert data == b""
    manifests.finish(ident, sha256(data).hexdigest(), now + 1)
    return ident


def test_bucket_crud_and_validation(catalog):
    cat, _ = catalog
    cat.create_bucket("my-bucket", 1)
    cat.create_bucket("other-123", 2)
    assert cat.list_buckets() == ["my-bucket", "other-123"]
    with pytest.raises(ValueError, match="already exists"):
        cat.create_bucket("my-bucket", 3)
    for name in ["UPPER", "a", "192.168.1.1", "-bad", "bad..name",
                 "a" * 64, "bad_.name", "bad.-name"]:
        with pytest.raises(ValueError):
            cat.create_bucket(name, 4)
    cat.delete_bucket("my-bucket")
    assert cat.list_buckets() == ["other-123"]


def test_atomic_publish_and_overwrite_keeps_old_manifest(catalog):
    cat, manifests = catalog
    cat.create_bucket("backup-store", 10)
    first = ready(manifests, "backup-store", "folder/foo.txt", b"", 11)
    head = cat.publish(first, now=12, expected_generation=0)
    assert head.generation == 1
    assert cat.get("backup-store", "folder/foo.txt").item.id == first
    second = ready(manifests, "backup-store", "folder/foo.txt", b"", 20)
    head = cat.publish(second, now=21, expected_generation=1)
    assert head.generation == 2 and head.item.id == second
    assert manifests.get(first).id == first
    with pytest.raises(ValueError, match="precondition"):
        cat.publish(first, now=22, expected_generation=1)
    assert cat.get("backup-store", "folder/foo.txt").item.id == second


def test_list_pagination_unicode_and_literal_prefix(catalog):
    cat, manifests = catalog
    cat.create_bucket("backup-store", 10)
    for i, key in enumerate(["prefix%literal", "prefix_a", "prefix_b",
                             "quotes/\"hello\"", "ümlaut", "prefix/z"]):
        item = ready(manifests, "backup-store", key, b"", i + 20)
        cat.publish(item, now=i + 20)
    first_page = cat.list_objects("backup-store", prefix="prefix", limit=2)
    assert [row.item.key for row in first_page] == ["prefix%literal", "prefix/z"]
    second_page = cat.list_objects("backup-store", prefix="prefix",
                                   after=first_page[-1].item.key, limit=2)
    assert [row.item.key for row in second_page] == ["prefix_a", "prefix_b"]
    assert [x.item.key for x in cat.list_objects("backup-store", prefix="prefix%")] == [
        "prefix%literal",
    ]
    assert [x.item.key for x in cat.list_objects("backup-store", prefix="prefix_")] == [
        "prefix_a", "prefix_b",
    ]


def test_delete_unlinks_only_current_pointer_and_cannot_destroy_history(catalog):
    cat, manifests = catalog
    cat.create_bucket("backup-store", 1)
    ident = ready(manifests, "backup-store", "test", b"", 2)
    cat.publish(ident, now=3)
    assert cat.remove_current("backup-store", "test", expected_generation=1)
    assert not cat.remove_current("backup-store", "test")
    with pytest.raises(FileNotFoundError):
        cat.get("backup-store", "test")
    assert manifests.get(ident).id == ident
    with pytest.raises(ValueError, match="history"):
        cat.delete_bucket("backup-store")


def test_uncommitted_object_cannot_be_published(catalog):
    cat, manifests = catalog
    cat.create_bucket("backup-store", 1)
    ident = manifests.begin("backup-store", "pending", 55, 2)
    with pytest.raises(FileNotFoundError):
        cat.publish(ident, now=3)
    assert cat.list_objects("backup-store") == []


def test_invalid_key_size_fails_before_catalog_write(catalog):
    cat, manifests = catalog
    cat.create_bucket("backup-store", 1)
    for key in ["", "ü" * 600]:
        ident = ready(manifests, "backup-store", key, b"", 2) if key else None
        if ident:
            with pytest.raises(ValueError, match="UTF-8"):
                cat.publish(ident, now=3)


def test_generation_guard_prevents_lost_update(catalog):
    cat, manifests = catalog
    cat.create_bucket("backup-store", 1)
    ids = [ready(manifests, "backup-store", "same", b"", i * 10 + 1) for i in range(3)]
    assert cat.publish(ids[0], now=5).generation == 1
    assert cat.publish(ids[1], now=6, expected_generation=1).generation == 2
    with pytest.raises(ValueError, match="precondition"):
        cat.publish(ids[2], now=7, expected_generation=1)
    assert cat.get("backup-store", "same").item.id == ids[1]


def test_catalog_metadata_survives_restart(tmp_path):
    url = str(tmp_path / "state.db")
    one = connect_database(url)
    try:
        manifests = ManifestStore(one)
        cat = BucketCatalog(manifests)
        cat.create_bucket("persisted-bucket", 1)
        ident = ready(manifests, "persisted-bucket", "some/key", b"", 2)
        cat.publish(ident, now=3)
    finally:
        one.close()
    two = connect_database(url)
    try:
        cat = BucketCatalog(ManifestStore(two))
        assert cat.get("persisted-bucket", "some/key").item.id == ident
    finally:
        two.close()
