"""Fail-closed GC reachability proof for real S3 object and multipart traffic."""
from __future__ import annotations

import pytest
from sqlalchemy import update

from tests.s3.test_s3_gateway import service, signed
from tests.s3.test_s3_copy import copy
from tests.s3.test_s3_multipart import begin, part
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.gc import StorageGCInventory
from teledrive.storage.manifest import ManifestStore, object_aliases


def audit(service):
    db = service[3]()
    try:
        return StorageGCInventory(ManifestStore(db)).inspect()
    finally:
        db.close()


def test_copy_alias_protects_original_telegram_chunks_after_source_delete(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/source", b"immutable").status_code == 200
    assert copy(service, "backup-bucket/source", "/backup-bucket/clone").status_code == 200
    assert signed(service, "DELETE", "/backup-bucket/source").status_code == 204
    inventory = audit(service)
    assert inventory.eligible_for_deletion == 0
    assert inventory.total_telegram_messages == 1
    assert inventory.copy_aliases == 1
    assert inventory.live_object_heads == 1
    assert inventory.protected_chunks[0].copied_aliases == 1
    assert inventory.protected_chunks[0].live_heads == 1
    assert signed(service, "GET", "/backup-bucket/clone").content == b"immutable"


def test_deleted_head_retains_history_and_uncommitted_multipart_part(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/old", b"historical-bytes").status_code == 200
    assert signed(service, "DELETE", "/backup-bucket/old").status_code == 204
    uid = begin(service, key="in-progress.bin")
    part(service, uid, 1, b"staged-payload", key="in-progress.bin")
    inventory = audit(service)
    assert inventory.total_telegram_messages == 2
    assert inventory.multipart_parts == 1
    assert inventory.live_object_heads == 0
    assert len(inventory.protected_chunks) == 2
    assert sorted(x.multipart_parts for x in inventory.protected_chunks) == [0, 1]
    assert inventory.eligible_for_deletion == 0


def test_corrupt_alias_cycle_cannot_produce_deletion_inventory(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/source", b"data").status_code == 200
    assert copy(service, "/backup-bucket/source", "/backup-bucket/clone").status_code == 200
    db = service[3]()
    try:
        manifests = ManifestStore(db)
        cloned = BucketCatalog(manifests).get("backup-bucket", "clone").item
        db.connection.execute(update(object_aliases).where(
            object_aliases.c.object_id == cloned.id
        ).values(source_object_id=cloned.id))
        db.commit()
    finally:
        db.close()
    with pytest.raises(RuntimeError, match="corrupt"):
        audit(service)
