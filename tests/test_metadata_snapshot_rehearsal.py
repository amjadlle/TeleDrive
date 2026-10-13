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
