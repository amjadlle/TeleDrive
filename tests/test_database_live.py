"""Live database integration tests; skipped unless CI supplies an external DB URL."""
from __future__ import annotations

import os
import time

import pytest

from teledrive.db import connect_database


@pytest.mark.parametrize("env_name", ["DB_TEST_POSTGRES_URL", "DB_TEST_MYSQL_URL"])
def test_external_db_schema_queue_lease_and_account_history(env_name):
    url = os.getenv(env_name)
    if not url:
        pytest.skip(f"{env_name} not configured")
    first = connect_database(url)
    second = connect_database(url)
    now = int(time.time())
    token = f"ci-{env_name}-{now}"
    path = f"/uploads/{token}.bin"
    try:
        first.insert_pending(path, 123, 1.0, now)
        first.commit()

        assert first.claim_file(path, token + "-a", now, 3600)
        assert not second.claim_file(path, token + "-b", now, 3600)

        first.mark_uploaded(path, 99, "primary", now)
        first.set_account_state(
            "primary", "ready", last_error=None,
            cooldown_until=None, last_success_ts=now, now=now,
        )
        first.commit()

        history = first.list_files("history", 200)
        uploaded = next(row for row in history if row["path"] == path)
        assert uploaded["tg_account"] == "primary"
        assert uploaded["tg_message_id"] == 99
        assert first.uploaded_counts_by_account()["primary"] >= 1
        assert first.all_account_states()["primary"]["state"] == "ready"

        assert first.acquire_lease(token, "holder-a", now, 30)
        assert not second.acquire_lease(token, "holder-b", now, 30)
        assert first.renew_lease(token, "holder-a", now, 30)
        first.release_lease(token, "holder-a")
        assert second.acquire_lease(token, "holder-b", now, 30)
        second.release_lease(token, "holder-b")
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("env_name", ["DB_TEST_POSTGRES_URL", "DB_TEST_MYSQL_URL"])
def test_storage_manifest_round_trip_on_real_database(env_name):
    """Verify the new chunk journal and ready visibility against a real SQL server."""
    url = os.getenv(env_name)
    if not url:
        pytest.skip(f"{env_name} not configured")
    from hashlib import sha256
    from teledrive.storage.manifest import ManifestStore, ChunkReference

    connection = connect_database(url)
    manifests = ManifestStore(connection)
    now = int(time.time())
    try:
        ident = manifests.begin("ci-bucket", f"test/{env_name}/{now}", 8, now)
        with pytest.raises(FileNotFoundError):
            manifests.get(ident)
        payload = b"abcdefgh"
        manifests.append(ident, ChunkReference(
            ordinal=0, offset=0, size=8, sha256=sha256(payload).hexdigest(),
            account="ci", chat_id=123456, message_id=int(now * 100) % 2000000000,
        ), now)
        ready = manifests.finish(ident, sha256(payload).hexdigest(), now)
        assert ready.size == 8
        assert ready.sha256 == sha256(payload).hexdigest()
        assert len(manifests.parts(ident)) == 1
    finally:
        connection.close()


@pytest.mark.parametrize("env_name", ["DB_TEST_POSTGRES_URL", "DB_TEST_MYSQL_URL"])
def test_failed_chunk_cleanup_transaction_is_durable(env_name):
    """Real DB: recovery transitions failed -> cleaned and cannot alter READY data."""
    url = os.getenv(env_name)
    if not url:
        pytest.skip(f"{env_name} not configured")
    import asyncio
    from hashlib import sha256
    from uuid import uuid4
    from teledrive.storage.manifest import ManifestStore, ChunkReference
    from teledrive.storage.recovery import Discovery, RecoveryManager, UnsafeCleanup

    class MissingRemote:
        async def discover(self, identifier, *, scan_limit):
            return Discovery((), True, 0)

        async def probe(self, part, *, object_id):
            return "missing"

        async def delete_verified(self, part, *, object_id):
            raise AssertionError("Deleted missing remote media")

    older = int(time.time()) - 7200
    connection = connect_database(url)
    try:
        manifests = ManifestStore(connection)
        unique = uuid4().hex
        object_id = manifests.begin("ci", f"cleanup/{unique}", 4, older)
        part = ChunkReference(
            ordinal=0, offset=0, size=4, sha256=sha256(b"data").hexdigest(),
            account="ci", chat_id=123456, message_id=(int(unique[:8], 16) % 2000000000) + 1,
        )
        manifests.append(object_id, part, older)
        manifests.fail(object_id, "simulated timeout", older)
        service = RecoveryManager(manifests, MissingRemote(), min_age_seconds=3600)
        preview = asyncio.run(service.cleanup(object_id))
        assert preview.missing == 1 and preview.deleted == 0 and preview.dry_run
        result = asyncio.run(service.cleanup(object_id, dry_run=False))
        assert result.cleaned and result.deleted == 0
        assert object_id not in [row[0] for row in manifests.interrupted()]
        with pytest.raises(UnsafeCleanup):
            asyncio.run(service.cleanup(object_id, dry_run=False))
    finally:
        connection.close()


@pytest.mark.parametrize("env_name", ["DB_TEST_POSTGRES_URL", "DB_TEST_MYSQL_URL"])
def test_writer_fence_blocks_stale_commit_on_separate_connection(env_name):
    url = os.getenv(env_name)
    if not url:
        pytest.skip(f"{env_name} not configured")
    from hashlib import sha256
    from uuid import uuid4
    from teledrive.storage.manifest import ManifestStore, ManifestError

    a = connect_database(url)
    b = connect_database(url)
    try:
        now = int(time.time()) - 3600
        first = ManifestStore(a)
        second = ManifestStore(b)
        ident = first.begin("ci", f"live-writer/{uuid4().hex}", 0, now)
        first.start_writer(ident, "worker-before-crash", now, ttl=120)
        assert not second.heartbeat_writer(ident, "wrong-owner", now + 1)
        abandoned = second.abandon_stale_pending(now=now + 3601, min_age_seconds=60)
        assert ident in abandoned
        assert not first.heartbeat_writer(ident, "worker-before-crash", now + 3602)
        with pytest.raises(ManifestError):
            first.finish(ident, sha256(b"").hexdigest(), now + 3602,
                         writer_token="worker-before-crash")
        assert (ident, "failed") in first.interrupted()
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("env_name", ["DB_TEST_POSTGRES_URL", "DB_TEST_MYSQL_URL"])
def test_bucket_heads_visible_across_live_database_connections(env_name):
    url = os.getenv(env_name)
    if not url:
        pytest.skip(f"{env_name} not configured")
    from hashlib import sha256
    from uuid import uuid4
    from teledrive.storage.manifest import ManifestStore
    from teledrive.storage.catalog import BucketCatalog

    a = connect_database(url)
    b = connect_database(url)
    try:
        first = ManifestStore(a)
        second = ManifestStore(b)
        one = BucketCatalog(first)
        two = BucketCatalog(second)
        bucket = "ci-" + uuid4().hex[:20]
        one.create_bucket(bucket, int(time.time()))
        key = "backup/ü/snapshot.bin"
        ident = first.begin(bucket, key, 0, int(time.time()))
        first.finish(ident, sha256(b"").hexdigest(), int(time.time()))
        first_head = one.publish(ident, now=int(time.time()), expected_generation=0)
        assert first_head.generation == 1
        assert two.get(bucket, key).item.id == ident

        next_id = second.begin(bucket, key, 0, int(time.time()))
        second.finish(next_id, sha256(b"").hexdigest(), int(time.time()))
        assert two.publish(next_id, now=int(time.time()), expected_generation=1).generation == 2
        with pytest.raises(ValueError):
            one.publish(ident, now=int(time.time()), expected_generation=1)
        assert one.get(bucket, key).item.id == next_id
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("env_name", ["DB_TEST_POSTGRES_URL", "DB_TEST_MYSQL_URL"])
def test_s3_multipart_atomic_chunk_reparent_on_live_sql(env_name, tmp_path):
    """Move existing Telegram message references without a duplicate upload.

    This exercises the actual PostgreSQL/MySQL unique-message constraint,
    multi-row object/chunk updates and committed head visibility.
    """
    url = os.getenv(env_name)
    if not url:
        pytest.skip(f"{env_name} not configured")
    import asyncio
    import hashlib
    from uuid import uuid4

    from teledrive.s3.multipart import MultipartStore
    from teledrive.storage.catalog import BucketCatalog
    from teledrive.storage.manifest import ChunkReference, ManifestStore
    from teledrive.storage.engine import TelegramObjectStorage

    data1 = b"A" * (5 * 1024 * 1024)
    data2 = b"tail"
    payloads = {}
    class FakeTelegram:
        async def upload(self, *args, **kwargs):
            raise AssertionError("Multipart Complete must not upload Telegram data")

        async def download(self, reference, destination):
            destination.write_bytes(payloads[reference.message_id])

    db = connect_database(url)
    second = connect_database(url)
    try:
        manifests = ManifestStore(db)
        catalog = BucketCatalog(manifests)
        mpu = MultipartStore(catalog)
        name = "ci-" + uuid4().hex[:20]
        key = "multipart/atomic.bin"
        now = int(time.time())
        catalog.create_bucket(name, now)
        upload_id = mpu.initiate(name, key, "ci-access-key")
        selected = []
        for idx, data in enumerate((data1, data2), start=1):
            manifest_id = manifests.begin(name, key, len(data), now)
            message_id = int(uuid4().hex[:8], 16) % 2_000_000_000 + 1
            payloads[message_id] = data
            manifests.append(manifest_id, ChunkReference(
                ordinal=0, offset=0, size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                account="ci", chat_id=-10012345, message_id=message_id,
            ), now)
            manifests.finish(manifest_id, hashlib.sha256(data).hexdigest(), now)
            etag = hashlib.md5(data, usedforsecurity=False).hexdigest()
            mpu.record_part(upload_id, name, key, "ci-access-key", number=idx,
                            object_id=manifest_id, etag=etag)
            selected.append((idx, etag))
        engine = TelegramObjectStorage(manifests, FakeTelegram(),
                                       chunk_size=1024 * 1024, scratch_directory=tmp_path)
        done = asyncio.run(mpu.complete(upload_id, name, key, "ci-access-key",
                                        selected, engine))
        assert done.size == len(data1) + len(data2)
        # Verify durable committed read through a different DB connection.
        other_catalog = BucketCatalog(ManifestStore(second))
        ready = other_catalog.get(name, key).item
        assert ready.id == done.object_id
        assert ready.sha256 == hashlib.sha256(data1 + data2).hexdigest()
        assert len(ManifestStore(second).parts(ready.id)) == 2
        async def read():
            other_engine = TelegramObjectStorage(
                ManifestStore(second), FakeTelegram(), scratch_directory=tmp_path
            )
            return b"".join([piece async for piece in other_engine.iter_bytes(ready.id)])
        assert asyncio.run(read()) == data1 + data2
    finally:
        db.close()
        second.close()
