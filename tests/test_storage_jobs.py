"""Durable upload queue with lease fencing, root confinement and restart retry."""
from __future__ import annotations

import asyncio
import os
import time

import pytest

from test_storage_engine import MemoryTelegram
from test_s3_admin import admin
from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.jobs import StorageUploadJobs, UploadJobError, jobs
from teledrive.storage.manifest import ManifestStore
import web.app as webapp


@pytest.fixture
def queue(tmp_path):
    source_root = tmp_path / "allowed"
    source_root.mkdir()
    db_path = str(tmp_path / "metadata.sqlite")
    conn = connect_database(db_path)
    catalog = BucketCatalog(ManifestStore(conn))
    catalog.create_bucket("job-bucket", int(time.time()))
    try:
        yield StorageUploadJobs(catalog), source_root, db_path
    finally:
        conn.close()


def test_job_roundtrip_survives_sql_reopen_and_prevents_double_publish(queue):
    first, root, path = queue
    data = bytes(range(250)) * 17
    source = root / "payload.bin"
    source.write_bytes(data)
    ident = first.enqueue("job-bucket", "backup/data.bin", source, allowed_root=root)
    assert first.list_jobs()[0]["id"] == ident
    assert "lease_token" not in first.list_jobs()[0]
    assert first.list_jobs()[0]["expected_generation"] == 0
    remote = MemoryTelegram()
    one = connect_database(path)
    try:
        runner = StorageUploadJobs(BucketCatalog(ManifestStore(one)))
        result = asyncio.run(runner.run_next(
            remote, allowed_root=root, scratch_directory=root, chunk_size=2048,
        ))
        assert result["id"] == ident and result["state"] == "done"
        assert remote.uploads == 3
    finally:
        one.close()
    two = connect_database(path)
    try:
        runner = StorageUploadJobs(BucketCatalog(ManifestStore(two)))
        assert runner.run_next is not None
        assert asyncio.run(runner.run_next(
            remote, allowed_root=root, scratch_directory=root,
        )) is None
        obj = runner.catalog.get("job-bucket", "backup/data.bin").item
        assert obj.id == result["object_id"]
        restored = root / "restored.bin"
        asyncio.run(TelegramObjectStorage(
            runner.catalog.manifests, remote
        ).download_to_path(obj.id, restored))
        assert restored.read_bytes() == data
        assert runner.list_jobs()[0]["state"] == "done"
    finally:
        two.close()


def test_upload_jobs_reject_outside_root_symlinks_and_changed_source(queue, tmp_path):
    manager, root, _ = queue
    outside = tmp_path / "not-allowed.bin"
    outside.write_bytes(b"secret")
    with pytest.raises(UploadJobError, match="outside"):
        manager.enqueue("job-bucket", "secret", outside, allowed_root=root)
    link = root / "linked.bin"
    link.symlink_to(outside)
    with pytest.raises(UploadJobError, match="Symlink"):
        manager.enqueue("job-bucket", "secret", link, allowed_root=root)
    source = root / "upload.bin"
    source.write_bytes(b"old")
    identifier = manager.enqueue("job-bucket", "new", source, allowed_root=root)
    source.write_bytes(b"changed source")
    remote = MemoryTelegram()
    with pytest.raises(UploadJobError, match="Source changed"):
        asyncio.run(manager.run_next(remote, allowed_root=root, scratch_directory=root))
    assert remote.uploads == 0
    assert manager.list_jobs()[0]["state"] == "failed"
    assert manager.retry(identifier)
    assert not manager.retry(identifier)
    assert manager.list_jobs()[0]["state"] == "queued"


def test_staged_job_restarts_without_reupload(queue):
    manager, root, path = queue
    source = root / "stage.bin"
    source.write_bytes(b"staged" * 32)
    identifier = manager.enqueue("job-bucket", "staged.bin", source, allowed_root=root)
    claim = manager.claim_next(now=int(time.time()), ttl=120)
    assert claim["id"] == identifier
    remote = MemoryTelegram()
    engine = TelegramObjectStorage(manager.catalog.manifests, remote, chunk_size=64,
                                   scratch_directory=root)
    ready = asyncio.run(engine.put_file("job-bucket", "staged.bin", source))
    assert manager.mark_staged(identifier, claim["lease_token"], ready.id,
                               now=int(time.time()))
    sent_before_restart = remote.uploads
    # Simulate a hard-killed runner: the persisted READY manifest and job
    # staging record survive, but its owner lease expires.
    manager.db.connection.execute(
        jobs.update().where(jobs.c.id == identifier).values(lease_until=0)
    )
    manager.db.commit()
    db = connect_database(path)
    try:
        resumed = StorageUploadJobs(BucketCatalog(ManifestStore(db)))
        outcome = asyncio.run(resumed.run_next(
            remote, allowed_root=root, scratch_directory=root, chunk_size=64,
        ))
        assert outcome == {"id": identifier, "state": "done", "object_id": ready.id}
        assert remote.uploads == sent_before_restart
        assert resumed.catalog.get("job-bucket", "staged.bin").item.id == ready.id
        assert resumed.list_jobs()[0]["state"] == "done"
    finally:
        db.close()


def test_one_worker_lease_and_head_generation_precondition(queue):
    manager, root, path = queue
    source = root / "cas.bin"
    source.write_bytes(b"delayed")
    identifier = manager.enqueue("job-bucket", "cas.bin", source, allowed_root=root)
    claim = manager.claim_next(now=int(time.time()), ttl=120)
    assert claim and claim["id"] == identifier
    assert manager.claim_next() is None
    assert not manager.renew(identifier, "not-the-token", now=int(time.time()))
    assert manager.renew(identifier, claim["lease_token"], now=int(time.time()))
    manager.fail(identifier, claim["lease_token"], "injected failure", now=int(time.time()))
    assert manager.retry(identifier)

    # Another writer creates the key after enqueue. The old job cannot
    # replace it because expected_generation=0 is checked at final publish.
    external = manager.catalog.manifests.begin(
        "job-bucket", "cas.bin", 0, int(time.time())
    )
    from hashlib import sha256
    manager.catalog.manifests.finish(external, sha256(b"").hexdigest(), int(time.time()))
    manager.catalog.publish(external, now=int(time.time()))
    with pytest.raises(ValueError, match="generation precondition"):
        asyncio.run(manager.run_next(
            MemoryTelegram(), allowed_root=root,
            scratch_directory=root, chunk_size=4,
        ))
    assert manager.catalog.get("job-bucket", "cas.bin").item.id == external
    assert manager.list_jobs()[0]["state"] == "failed"


def test_inactive_jobs_can_be_cancelled_without_upload_or_losing_history(queue):
    manager, root, path = queue
    source = root / "cancel.bin"
    source.write_bytes(b"safe")
    first = manager.enqueue("job-bucket", "cancelled.bin", source, allowed_root=root)
    assert manager.cancel(first) is True
    assert manager.cancel(first) is False
    assert manager.retry(first) is False
    assert manager.claim_next() is None

    second = manager.enqueue("job-bucket", "claimed.bin", source, allowed_root=root)
    owner = manager.claim_next()
    assert owner and owner["id"] == second
    assert not manager.cancel(second), "Never cancel an active writer"
    assert manager.fail(second, owner["lease_token"], "planned fail", now=int(time.time()))
    assert manager.cancel(second)
    assert not manager.retry(second)

    reopened = connect_database(path)
    try:
        jobs_after_restart = StorageUploadJobs(BucketCatalog(ManifestStore(reopened)))
        states = {item["id"]: item["state"] for item in jobs_after_restart.list_jobs()}
        assert states == {first: "cancelled", second: "cancelled"}
        assert jobs_after_restart.claim_next() is None
        assert jobs_after_restart.catalog.list_buckets() == ["job-bucket"]
    finally:
        reopened.close()


def test_admin_job_endpoints_require_root_auth_csrf_and_do_not_expose_tokens(
    admin, monkeypatch, tmp_path
):
    client, db_path, _ = admin
    root = tmp_path / "uploads"
    root.mkdir()
    source = root / "actual.bin"
    source.write_bytes(b"safe-bot-upload")
    conn = connect_database(db_path)
    try:
        BucketCatalog(ManifestStore(conn)).create_bucket("job-admin", int(time.time()))
    finally:
        conn.close()
    url = "/api/v1/admin/storage/jobs"
    auth = ("testadmin", "secure-auth-password")
    assert client.get(url).status_code in (401, 403)
    payload = {"bucket": "job-admin", "key": "single.bin",
               "source_path": str(source)}
    monkeypatch.delenv("TELEDRIVE_JOB_SOURCE_ROOT", raising=False)
    assert client.post(url, auth=auth, json=payload).status_code == 403
    assert client.post(url, auth=auth, json=payload,
                       headers={"x-teledrive-csrf": "1"}).status_code == 503
    monkeypatch.setenv("TELEDRIVE_JOB_SOURCE_ROOT", str(root))
    monkeypatch.setenv("TELEDRIVE_STORAGE_SCRATCH_DIR", str(tmp_path / "scratch"))
    start = client.post(url, auth=auth, json=payload,
                        headers={"x-teledrive-csrf": "1"})
    assert start.status_code == 201, start.text
    entry = client.get(url, auth=auth)
    assert entry.status_code == 200 and "lease_token" not in entry.text
    remote = MemoryTelegram()
    monkeypatch.setattr(webapp, "storage_transport", lambda db: remote)
    run = client.post(url + "/run-next", auth=auth,
                      headers={"x-teledrive-csrf": "1"})
    assert run.status_code == 200, run.text
    assert run.json()["job"]["state"] == "done"
    assert remote.uploads == 1
    assert client.get(url, auth=auth).json()["jobs"][0]["state"] == "done"
    retry = client.post(url + "/" + start.json()["id"] + "/retry",
                        auth=auth, headers={"x-teledrive-csrf": "1"})
    assert retry.status_code == 409


def test_cancel_job_endpoint_requires_root_and_csrf_and_fences_active_jobs(
    admin, monkeypatch, tmp_path
):
    client, db_path, _ = admin
    root = tmp_path / "allowed-cancel"
    root.mkdir()
    source = root / "one.bin"
    source.write_bytes(b"must-not-be-uploaded")
    conn = connect_database(db_path)
    try:
        BucketCatalog(ManifestStore(conn)).create_bucket("cancel-admin", int(time.time()))
    finally:
        conn.close()
    monkeypatch.setenv("TELEDRIVE_JOB_SOURCE_ROOT", str(root))
    auth = ("testadmin", "secure-auth-password")
    response = client.post("/api/v1/admin/storage/jobs",
                           auth=auth, headers={"x-teledrive-csrf": "1"},
                           json={"bucket": "cancel-admin", "key": "one.bin",
                                 "source_path": str(source)})
    assert response.status_code == 201, response.text
    job_id = response.json()["id"]
    url = f"/api/v1/admin/storage/jobs/{job_id}/cancel"
    assert client.post(url).status_code in (401, 403)
    assert client.post(url, auth=auth).status_code == 403
    success = client.post(url, auth=auth, headers={"x-teledrive-csrf": "1"})
    assert success.status_code == 200, success.text
    assert success.json() == {"id": job_id, "state": "cancelled"}
    assert client.post(url, auth=auth,
                       headers={"x-teledrive-csrf": "1"}).status_code == 409
    assert client.get("/api/v1/admin/storage/jobs", auth=auth).json()["jobs"][0]["state"] == "cancelled"
    assert client.post("/api/v1/admin/storage/jobs/" + job_id + "/retry",
                       auth=auth, headers={"x-teledrive-csrf": "1"}).status_code == 409


def _simulate_crash_after_first_journaled_chunk(manager, root, remote):
    """Leave the SQL job and object with expired independent worker fences."""
    import hashlib
    from sqlalchemy import update
    from teledrive.storage.manifest import ChunkReference, writer_fences

    payload = bytes(range(31))
    source = root / "resume-source.bin"
    source.write_bytes(payload)
    job_id = manager.enqueue("job-bucket", "resumable.bin", source, allowed_root=root)
    claim = manager.claim_next()
    assert claim["id"] == job_id
    now = int(time.time())
    manifests = manager.catalog.manifests
    identifier = manifests.begin("job-bucket", "resumable.bin", len(payload), now)
    manifests.start_writer(identifier, "crashed-owner", now, ttl=120)
    assert manager.mark_pending(job_id, claim["lease_token"], identifier, now=now)
    first = root / "sent-before-crash.bin"
    first.write_bytes(payload[:5])
    checksum = hashlib.sha256(payload[:5]).hexdigest()
    account, chat, message = asyncio.run(remote.upload(
        first, object_id=identifier, ordinal=0, checksum=checksum,
    ))
    part = ChunkReference(0, 0, 5, checksum, account, chat, message)
    manifests.append(identifier, part, now, writer_token="crashed-owner")
    manager.db.connection.execute(
        update(writer_fences).where(writer_fences.c.object_id == identifier)
        .values(lease_until=0)
    )
    manager.db.connection.execute(
        update(jobs).where(jobs.c.id == job_id).values(lease_until=0)
    )
    manager.db.commit()
    return job_id, identifier, payload, part


def test_resume_journaled_chunks_after_crash_and_db_restart(queue):
    manager, root, db_path = queue
    remote = MemoryTelegram()
    job_id, object_id, payload, preserved = _simulate_crash_after_first_journaled_chunk(
        manager, root, remote,
    )
    original_uploads = remote.uploads
    assert original_uploads == 1
    assert manager.catalog.manifests.recovery_parts(object_id) == [preserved]

    new_connection = connect_database(db_path)
    try:
        runner = StorageUploadJobs(BucketCatalog(ManifestStore(new_connection)))
        result = asyncio.run(runner.run_next(
            remote, allowed_root=root, scratch_directory=root, chunk_size=5,
        ))
        assert result == {"id": job_id, "state": "done", "object_id": object_id}
        assert remote.uploads == 1 + (len(payload) - 5 + 4) // 5
        assert remote.downloads == 1, "Old Telegram chunk must be verified"
        refs = runner.catalog.manifests.parts(object_id)
        assert refs[0] == preserved
        assert len(refs) == remote.uploads
        assert runner.catalog.get("job-bucket", "resumable.bin").item.id == object_id
        restored = root / "resumed-output.bin"
        asyncio.run(TelegramObjectStorage(
            runner.catalog.manifests, remote
        ).download_to_path(object_id, restored))
        assert restored.read_bytes() == payload
        assert runner.list_jobs()[0]["state"] == "done"
    finally:
        new_connection.close()


def test_resume_corrupt_remote_fails_closed_and_retry_preserves_orphan(queue):
    manager, root, db_path = queue
    remote = MemoryTelegram()
    job_id, object_id, payload, preserved = _simulate_crash_after_first_journaled_chunk(
        manager, root, remote,
    )
    remote.data[preserved.account, preserved.chat_id, preserved.message_id] = b"bad!"
    reopened = connect_database(db_path)
    try:
        runner = StorageUploadJobs(BucketCatalog(ManifestStore(reopened)))
        from teledrive.storage.engine import IntegrityError
        with pytest.raises(IntegrityError):
            asyncio.run(runner.run_next(
                remote, allowed_root=root, scratch_directory=root, chunk_size=5,
            ))
        assert remote.uploads == 1
        assert runner.list_jobs()[0]["state"] == "failed"
        assert runner.catalog.manifests.recovery_parts(object_id) == [preserved]
        with pytest.raises(FileNotFoundError):
            runner.catalog.get("job-bucket", "resumable.bin")
        assert runner.retry(job_id)
        assert runner.list_jobs()[0]["object_id"] is None
        fixed = asyncio.run(runner.run_next(
            remote, allowed_root=root, scratch_directory=root, chunk_size=5,
        ))
        assert fixed["state"] == "done"
        assert fixed["object_id"] != object_id
        assert (preserved.account, preserved.chat_id, preserved.message_id) in remote.data
        restored = root / "after-retry.bin"
        asyncio.run(TelegramObjectStorage(
            runner.catalog.manifests, remote
        ).download_to_path(fixed["object_id"], restored))
        assert restored.read_bytes() == payload
    finally:
        reopened.close()


def test_pending_writer_reclaim_rejects_live_and_unfenced_manifests(queue):
    manager, root, _ = queue
    now = int(time.time())
    store = manager.catalog.manifests
    identifier = store.begin("job-bucket", "guard.bin", 0, now)
    store.start_writer(identifier, "current", now, ttl=120)
    assert not store.reclaim_writer(identifier, "intruder", now)
    assert store.heartbeat_writer(identifier, "current", now)
    legacy = store.begin("job-bucket", "legacy-guard.bin", 0, now - 86400)
    assert not store.reclaim_writer(legacy, "intruder", now)
    assert store.reclaim_writer(identifier, "recovered", now + 121)
    assert not store.heartbeat_writer(identifier, "current", now + 122)
