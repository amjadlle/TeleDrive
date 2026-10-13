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
    monkeypatch.setattr(webapp, "storage_bot_transport", lambda db: remote)
    run = client.post(url + "/run-next", auth=auth,
                      headers={"x-teledrive-csrf": "1"})
    assert run.status_code == 200, run.text
    assert run.json()["job"]["state"] == "done"
    assert remote.uploads == 1
    assert client.get(url, auth=auth).json()["jobs"][0]["state"] == "done"
    retry = client.post(url + "/" + start.json()["id"] + "/retry",
                        auth=auth, headers={"x-teledrive-csrf": "1"})
    assert retry.status_code == 409
