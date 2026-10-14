"""Staging metadata restore verification is read-only and fail-closed."""
from __future__ import annotations

import hashlib
import sqlite3
import time

import pytest

from scripts.backup_metadata import backup_sqlite
from scripts.verify_metadata_backup import InvalidSnapshot, verify_snapshot
from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore


def seed(path):
    db = connect_database(str(path))
    try:
        catalog = BucketCatalog(ManifestStore(db))
        catalog.create_bucket("verify-ci", int(time.time()))
        ident = catalog.manifests.begin("verify-ci", "a.bin", 0, int(time.time()))
        catalog.manifests.finish(ident, hashlib.sha256(b"").hexdigest(), int(time.time()))
        catalog.publish(ident, now=int(time.time()))
    finally:
        db.close()


def test_backup_verification_accepts_valid_snapshot_and_checksum(tmp_path):
    live, image = tmp_path / "live.db", tmp_path / "image.db"
    seed(live)
    receipt = backup_sqlite(live, image)
    result = verify_snapshot(image, receipt)
    assert result["valid"] and result["objects"] == 1
    assert result["versions"] == result["heads"] == 1
    assert result["physical_deletion_performed"] is False
    with pytest.raises(InvalidSnapshot, match="receipt"):
        verify_snapshot(image, "0" * 64)


def test_backup_verification_rejects_corrupted_heads_and_versions(tmp_path):
    live = tmp_path / "live.db"
    seed(live)
    with sqlite3.connect(live) as db:
        db.execute("UPDATE storage_object_heads SET key_hash=?", ("0" * 64,))
        db.commit()
    with pytest.raises(InvalidSnapshot, match="HEAD"):
        verify_snapshot(live)
    second = tmp_path / "second.db"
    seed(second)
    with sqlite3.connect(second) as db:
        db.execute("UPDATE storage_published_versions SET key_hash=?", ("0" * 64,))
        db.commit()
    with pytest.raises(InvalidSnapshot, match="Published"):
        verify_snapshot(second)


def test_snapshot_verification_refuses_symlink_and_non_sqlite(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite database")
    with pytest.raises(InvalidSnapshot):
        verify_snapshot(bad)
    alias = tmp_path / "link.db"
    alias.symlink_to(bad)
    with pytest.raises(InvalidSnapshot, match="regular"):
        verify_snapshot(alias)



def test_verifier_includes_removed_versions_and_rejects_head_to_revoked_id(tmp_path):
    db_path = tmp_path / "history.db"
    seed(db_path)
    conn = connect_database(str(db_path))
    try:
        cat = BucketCatalog(ManifestStore(conn))
        original = cat.get("verify-ci", "a.bin").item.id
        marker = cat.delete_marker("verify-ci", "a.bin")
        assert cat.delete_version("verify-ci", "a.bin", marker)
        assert cat.is_visible_version("verify-ci", "a.bin", original)
    finally:
        conn.close()
    snapshot = tmp_path / "valid-history.db"
    sha = backup_sqlite(db_path, snapshot)
    record = verify_snapshot(snapshot, sha)
    assert record["removed_versions"] == 1
    assert record["versions"] == 2
    assert record["heads"] == 1
    with sqlite3.connect(snapshot) as raw:
        raw.execute(
            "UPDATE storage_object_heads SET object_id=? WHERE bucket='verify-ci'",
            (marker,),
        )
        raw.commit()
    with pytest.raises(InvalidSnapshot, match="revoked"):
        verify_snapshot(snapshot)


def test_verifier_rejects_orphan_removed_version_entry_even_when_sqlite_integrity_ok(tmp_path):
    db_path = tmp_path / "orphans.db"
    seed(db_path)
    with sqlite3.connect(db_path) as raw:
        raw.execute(
            "INSERT INTO storage_removed_versions(object_id, removed_ns) "
            "VALUES(?, ?)", ("unknown-version-id", 12),
        )
        raw.commit()
        assert raw.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    with pytest.raises(InvalidSnapshot, match="published history"):
        verify_snapshot(db_path)
