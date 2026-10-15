"""Crash recovery safety contracts for S3 multipart completion.

These assertions use real SQL transactions and the same authenticated admin
HTTP endpoints deployed by WebUI; no production Telegram messages are touched.
"""
from __future__ import annotations

import hashlib
import time

from sqlalchemy import select, update

from tests.s3.test_s3_admin import admin
from tests.s3.test_s3_gateway import service, signed
from tests.s3.test_s3_multipart import setup, begin, part, complete
from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore
from teledrive.s3.multipart import MultipartStore, completion_leases, uploads


def _store(url):
    db = connect_database(url)
    return db, MultipartStore(BucketCatalog(ManifestStore(db)))


def test_expired_completion_requires_explicit_admin_execute(admin):
    client, url, _ = admin
    now = int(time.time())
    db, manager = _store(url)
    try:
        manager.catalog.create_bucket("recovery-bucket", now)
        uid = manager.initiate("recovery-bucket", "data/file.bin", "creator")
        token = manager._claim_completion(uid, now - 7200, 120)
        assert manager.stale_completions(min_age_seconds=3600)[0]["id"] == uid
    finally:
        db.close()

    endpoint = f"/api/v1/admin/s3-multipart/{uid}/recover"
    auth = ("testadmin", "secure-auth-password")
    assert client.post(endpoint, json={"execute": True}).status_code in (401, 403)
    assert client.post(endpoint, json={"execute": True}, auth=auth).status_code == 403
    preview = client.get("/api/v1/admin/s3-multipart/stale", auth=auth)
    assert preview.status_code == 200
    assert len(preview.json()["uploads"]) == 1
    assert preview.json()["uploads"][0]["id"] == uid
    # POST defaults to dry-run: missing execute cannot reset the lease.
    check = client.post(endpoint, json={}, auth=auth, headers={"x-teledrive-csrf": "1"})
    assert check.status_code == 200
    assert check.json() == {
        "upload_id": uid, "eligible": True, "recovered": False, "dry_run": True
    }
    db, manager = _store(url)
    try:
        assert manager._renew_completion(uid, token, now, 120) is False
        assert db.connection.execute(select(uploads.c.state).where(
            uploads.c.id == uid
        )).scalar_one() == "completing"
    finally:
        db.close()
    restored = client.post(endpoint, json={"execute": True}, auth=auth,
                           headers={"x-teledrive-csrf": "1"})
    assert restored.status_code == 200
    assert restored.json()["recovered"] is True
    db, manager = _store(url)
    try:
        assert manager.require(uid, "recovery-bucket", "data/file.bin", "creator")["state"] == "active"
        assert not manager._renew_completion(uid, token, now, 120)
        assert not manager._reset_owned_completion(uid, token)
        assert manager.stale_completions() == []
    finally:
        db.close()
    repeat = client.post(endpoint, json={"execute": True}, auth=auth,
                         headers={"x-teledrive-csrf": "1"})
    assert repeat.status_code == 200
    assert repeat.json()["recovered"] is False


def test_no_recovery_for_live_or_legacy_unfenced_completion(admin):
    _, url, _ = admin
    now = int(time.time())
    db, mpu = _store(url)
    try:
        mpu.catalog.create_bucket("recovery-bucket", now)
        live = mpu.initiate("recovery-bucket", "live", "creator")
        live_token = mpu._claim_completion(live, now, 120)
        assert mpu.recover_stale_completion(live, dry_run=True) is False
        assert mpu.recover_stale_completion(live, dry_run=False) is False
        assert mpu._renew_completion(live, live_token, now, 120)

        old = mpu.initiate("recovery-bucket", "legacy", "creator")
        db.connection.execute(update(uploads).where(uploads.c.id == old)
                              .values(state="completing"))
        db.commit()
        assert mpu.recover_stale_completion(
            old, dry_run=False, now=now + 100000
        ) is False
        assert db.connection.execute(select(uploads.c.state)
                                     .where(uploads.c.id == old)).scalar_one() == "completing"
    finally:
        db.close()


def test_recovery_fences_old_writer_and_preserves_staged_parts(service):
    setup(service)
    uid = begin(service)
    etag = part(service, uid, 1, b"telegram-survives-recovery")
    db = service[3]()
    try:
        mpu = MultipartStore(BucketCatalog(ManifestStore(db)))
        old = int(time.time()) - 7200
        token = mpu._claim_completion(uid, old, 120)
        assert mpu.recover_stale_completion(uid, dry_run=True, min_age_seconds=3600)
        assert mpu.recover_stale_completion(uid, dry_run=False, min_age_seconds=3600)
        assert mpu._renew_completion(uid, token, int(time.time()), 120) is False
        assert mpu._reset_owned_completion(uid, token) is False
        assert mpu.list_parts(uid, "backup-bucket", "asset.bin", service[1].access_key)[0].etag == etag.strip('"')
    finally:
        db.close()
    done = complete(service, uid, [(1, etag)])
    assert done.status_code == 200, done.text
    got = signed(service, "GET", "/backup-bucket/asset.bin")
    assert got.status_code == 200
    assert got.content == b"telegram-survives-recovery"
    assert service[2].uploads == 1


def test_ownership_token_must_be_live_for_commit(service):
    setup(service)
    uid = begin(service)
    part(service, uid, 1, b"content")
    db = service[3]()
    try:
        mpu = MultipartStore(BucketCatalog(ManifestStore(db)))
        token = mpu._claim_completion(uid, int(time.time()) - 7200, 120)
        now = int(time.time())
        assert not mpu._renew_completion(uid, token, now, 120)
        assert mpu.recover_stale_completion(uid, now=now, min_age_seconds=3600)
        assert mpu.recover_stale_completion(uid, now=now,
                                           min_age_seconds=3600, dry_run=False)
        # The abandoned uploader must not modify the recovered upload.
        assert not mpu._reset_owned_completion(uid, token)
        assert mpu.require(uid, "backup-bucket", "asset.bin", service[1].access_key)["state"] == "active"
    finally:
        db.close()
