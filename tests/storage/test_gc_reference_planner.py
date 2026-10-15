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
    "copy_alias", "multipart_part", "mtproto_send_intent_or_receipt",
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
    elif source == "multipart_part":
        from teledrive.s3.multipart import MultipartStore, parts
        upload = MultipartStore(cat).initiate(
            "gc-audit-test", "candidate", "ci-owner", now=now,
        )
        db.connection.execute(insert(parts).values(
            upload_id=upload, number=1, object_id=ident,
            etag="a" * 32, size=0, updated_at=now,
        ))
    elif source == "copy_alias":
        child = cat.manifests.begin("gc-audit-test", "alias", 0, now)
        db.connection.execute(insert(object_aliases).values(
            object_id=child, source_object_id=ident,
        ))
    else:
        from teledrive.storage.user_mode import UserSendJournal
        digest = sha256(b"payload").hexdigest()
        journal = UserSendJournal(db)
        journal.start("test", -100123, ident, 0, digest, 7)
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


def _active_job(cat, *, state, object_id=None, expected_head_id=None,
                bucket="gc-audit-test", key="failed"):
    """Insert one durable upload-job row in an active state."""
    from teledrive.storage.jobs import StorageUploadJobs, jobs
    StorageUploadJobs(cat)  # create_all for the jobs table
    now = int(time.time())
    cat.db.connection.execute(insert(jobs).values(
        id=f"job-{state}-{object_id or expected_head_id or key}",
        bucket=bucket, object_key=key, source_path="/uploads/source.bin",
        source_size=7, source_device=1, source_inode=2, source_mtime_ns=3,
        expected_generation=0, expected_head_id=expected_head_id,
        state=state, object_id=object_id, attempts=0,
        lease_token="lease", lease_until=now + 60,
        created_at=now, updated_at=now, last_error=None,
    ))
    cat.db.commit()


def test_active_durable_job_with_object_id_blocks_gc(cat):
    ident = old_failure(cat)
    _active_job(cat, state="running", object_id=ident)
    row = one(cat, ident)
    assert row["reference_blocked"] is True
    assert "durable_job" in row["reference_blockers"]
    assert row["deletion_permitted"] is False


def test_active_durable_job_for_same_bucket_key_blocks_gc_without_object_id(cat):
    ident = old_failure(cat)
    _active_job(cat, state="queued", expected_head_id=ident)
    row = one(cat, ident)
    assert row["reference_blocked"] is True
    assert "durable_job" in row["reference_blockers"]


def test_finished_job_does_not_mask_missing_head_reference(cat):
    ident = old_failure(cat)
    _active_job(cat, state="done", object_id=ident)
    row = one(cat, ident)
    assert row["reference_blocked"] is False
    assert "durable_job" not in row["reference_blockers"]


def test_active_job_changes_gc_fingerprint(cat):
    ident = old_failure(cat)
    first = one(cat, ident)
    _active_job(cat, state="staged", object_id=ident)
    later = one(cat, ident)
    assert later["sql_reference_fingerprint"] != first["sql_reference_fingerprint"]


def test_inventory_counts_active_upload_jobs(cat):
    _active_job(cat, state="queued", key="a")
    _active_job(cat, state="running", key="b")
    _active_job(cat, state="done", key="c", object_id="done-object")
    from teledrive.storage.gc import StorageGCInventory
    inventory = StorageGCInventory(cat.manifests).inspect()
    assert inventory.active_upload_jobs == 2
    assert inventory.eligible_for_deletion == 0
