"""Live Telegram Bot API E2E round trip, opt-in via GitHub Actions Secrets.

Runs only against the dedicated bot and channel configured for CI.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

import pytest

from teledrive.db import connect_database
from teledrive.storage import ManifestStore, TelegramObjectStorage
from teledrive.storage.bot_api_adapter import (
    BotApiClient, BotApiChunkTransport, BotFileIndex,
)


def test_live_bot_upload_download_sha_range_and_cleanup():
    token = os.getenv("TELEDRIVE_E2E_BOT_TOKEN", "")
    chat_id = os.getenv("TELEDRIVE_E2E_BOT_CHAT_ID", "")
    if not token or not chat_id:
        pytest.skip("Live Bot API test requires a bot token and channel ID")

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-bot-e2e-") as folder:
            root = Path(folder)
            data = os.urandom(4097)
            original = root / "source.bin"
            original.write_bytes(data)
            database = connect_database(str(root / "manifest.db"))
            store = ManifestStore(database)
            transport = BotApiChunkTransport(
                BotApiClient(token), BotFileIndex(database),
                chat_id=int(chat_id), account="bot-ci",
            )
            refs = []
            try:
                await transport.verify_target()
                engine = TelegramObjectStorage(store, transport, chunk_size=2048)
                obj = await engine.put_file("ci", "bot/roundtrip.bin", original)
                refs = store.parts(obj.id)
                assert len(refs) == 3
                assert obj.sha256 == hashlib.sha256(data).hexdigest()
                original.unlink()
                output = root / "restored.bin"
                await engine.download_to_path(obj.id, output)
                assert output.read_bytes() == data
                received = bytearray()
                async for piece in engine.iter_bytes(obj.id, start=2047, end=2051):
                    received.extend(piece)
                assert bytes(received) == data[2047:2051]
            finally:
                # Limit deletion to message IDs recorded for THIS test invocation.
                try:
                    for ref in refs:
                        await transport.delete(ref)
                finally:
                    database.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.getenv("TELEDRIVE_E2E_MULTIPART") != "1",
    reason="5 MiB+ real Telegram multipart E2E is opt-in on manual workflow dispatch",
)
def test_live_bot_s3_multipart_moves_real_telegram_messages():
    """Manual S3 acceptance: preserve real Telegram messages across SQL reparenting.

    Completes two parts after reopening the SQL database; never duplicates
    messages on Telegram. This validates crash/restart durability, exact
    checksum and chunk reparenting behind the boto3+HTTP contract tests.
    """
    import time
    from teledrive.storage.catalog import BucketCatalog
    from teledrive.s3.multipart import MultipartStore
    token = os.getenv("TELEDRIVE_E2E_BOT_TOKEN", "")
    chat_id = os.getenv("TELEDRIVE_E2E_BOT_CHAT_ID", "")
    if not token or not chat_id:
        pytest.skip("Real Telegram multipart credentials missing")

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-bot-multipart-") as folder:
            root = Path(folder)
            database = connect_database(str(root / "state.db"))
            manifests = ManifestStore(database)
            catalog = BucketCatalog(manifests)
            transport = BotApiChunkTransport(
                BotApiClient(token), BotFileIndex(database),
                chat_id=int(chat_id), account="bot-ci-multipart",
                min_upload_interval=1.1,
            )
            refs = []
            try:
                await transport.verify_target()
                bucket = "ci-s3-multipart"
                catalog.create_bucket(bucket, int(time.time()))
                service = MultipartStore(catalog)
                upload_id = service.initiate(bucket, "live/large.bin", "ci-live-access")
                engine = TelegramObjectStorage(
                    manifests, transport, chunk_size=16 * 1024 * 1024,
                    scratch_directory=root,
                )
                first = os.urandom(5 * 1024 * 1024)
                last = os.urandom(1987)
                selection = []
                for number, payload in enumerate((first, last), start=1):
                    path = root / f"source-{number}.bin"
                    path.write_bytes(payload)
                    staged = await engine.put_file(bucket, "live/large.bin", path)
                    refs.extend(manifests.parts(staged.id))
                    etag = hashlib.md5(payload, usedforsecurity=False).hexdigest()
                    service.record_part(
                        upload_id, bucket, "live/large.bin", "ci-live-access",
                        number=number, object_id=staged.id, etag=etag,
                    )
                    selection.append((number, etag))
                    path.unlink()
                assert len(refs) == 2
                sent_messages = {(ref.account, ref.chat_id, ref.message_id)
                                 for ref in refs}
                # Simulate a process restart between Telegram ACK+SQL staging
                # and Complete. Persistent bot file IDs must survive too.
                database.close()
                database = connect_database(str(root / "state.db"))
                manifests = ManifestStore(database)
                catalog = BucketCatalog(manifests)
                service = MultipartStore(catalog)
                transport = BotApiChunkTransport(
                    BotApiClient(token), BotFileIndex(database),
                    chat_id=int(chat_id), account="bot-ci-multipart",
                    min_upload_interval=1.1,
                )
                engine = TelegramObjectStorage(
                    manifests, transport, chunk_size=16 * 1024 * 1024,
                    scratch_directory=root,
                )
                assert len(service.list_parts(
                    upload_id, bucket, "live/large.bin", "ci-live-access"
                )) == 2
                finished = await service.complete(
                    upload_id, bucket, "live/large.bin", "ci-live-access",
                    selection, engine,
                )
                assert finished.size == len(first) + len(last)
                final_refs = manifests.parts(finished.object_id)
                assert len(final_refs) == 2
                assert {(ref.account, ref.chat_id, ref.message_id)
                        for ref in final_refs} == sent_messages
                restored = root / "restored.bin"
                await engine.download_to_path(finished.object_id, restored)
                assert restored.read_bytes() == first + last
                assert hashlib.sha256(restored.read_bytes()).hexdigest() == (
                    hashlib.sha256(first + last).hexdigest()
                )
                assert catalog.get(bucket, "live/large.bin").item.id == finished.object_id
                # Complete on a fresh SQL connection is idempotent. The test
                # never sends a new Telegram upload during either completion.
                again = await service.complete(
                    upload_id, bucket, "live/large.bin", "ci-live-access",
                    selection, engine,
                )
                assert again.object_id == finished.object_id
            finally:
                # Explicitly clean only messages uploaded in this test. The
                # service itself never deletes them during multipart Complete.
                try:
                    for ref in refs:
                        await transport.delete(ref)
                finally:
                    database.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("payload_size,chunk_size", [
    pytest.param(3071, 2048, id="3KiB-two-chunks"),
    pytest.param(1024 * 1024 + 17, 256 * 1024, id="1MiB-plus-17-five-chunks"),
])
def test_live_bot_sqlite_snapshot_restores_remote_telegram_object(payload_size, chunk_size):
    """Real Bot API DR rehearsal: snapshot -> separate SQL restore -> download.

    Snapshot metadata is not itself a Telegram backup; restored Bot file IDs
    are checked against real still-existing channel messages before cleanup.
    """
    import time

    from scripts.backup_metadata import backup_sqlite
    from scripts.verify_metadata_backup import verify_snapshot
    from teledrive.storage.catalog import BucketCatalog

    token = os.getenv("TELEDRIVE_E2E_BOT_TOKEN", "")
    chat_id = os.getenv("TELEDRIVE_E2E_BOT_CHAT_ID", "")
    if not token or not chat_id:
        pytest.skip("Live Bot API disaster-recovery test requires E2E credentials")

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-bot-dr-") as folder:
            root = Path(folder)
            live_path = root / "live.sqlite"
            snapshot_path = root / "restore.sqlite"
            payload = os.urandom(payload_size)
            source = root / "source.bin"
            source.write_bytes(payload)
            live_db = connect_database(str(live_path))
            active_db = live_db
            refs = []
            transport = None
            try:
                catalog = BucketCatalog(ManifestStore(live_db))
                catalog.create_bucket("live-dr-ci", int(time.time()))
                transport = BotApiChunkTransport(
                    BotApiClient(token), BotFileIndex(live_db),
                    chat_id=int(chat_id), account="bot-ci-dr",
                )
                await transport.verify_target()
                uploaded = await TelegramObjectStorage(
                    catalog.manifests, transport, chunk_size=chunk_size,
                    scratch_directory=root,
                ).put_file("live-dr-ci", "snapshots/file.bin", source)
                refs = catalog.manifests.parts(uploaded.id)
                assert len(refs) == (payload_size + chunk_size - 1) // chunk_size
                catalog.publish(uploaded.id, now=int(time.time()))
                checksum = backup_sqlite(live_path, snapshot_path)
                validated = verify_snapshot(snapshot_path, checksum)
                assert validated["valid"] and validated["heads"] == 1
                assert validated["versions"] == 1
                assert validated["telegram_chunk_refs"] == len(refs)
                live_db.close()
                active_db = None

                restored_db = connect_database(str(snapshot_path))
                active_db = restored_db
                recovered_catalog = BucketCatalog(ManifestStore(restored_db))
                recovered = recovered_catalog.get("live-dr-ci", "snapshots/file.bin")
                assert recovered.item.id == uploaded.id
                assert recovered.item.sha256 == hashlib.sha256(payload).hexdigest()
                assert {r.message_id for r in recovered_catalog.manifests.parts(uploaded.id)} == {
                    r.message_id for r in refs
                }
                restored_transport = BotApiChunkTransport(
                    BotApiClient(token), BotFileIndex(restored_db),
                    chat_id=int(chat_id), account="bot-ci-dr",
                )
                transport = restored_transport
                await restored_transport.verify_target()
                restored_source = root / "restored.bin"
                restored_engine = TelegramObjectStorage(
                    recovered_catalog.manifests, restored_transport,
                    chunk_size=chunk_size, scratch_directory=root,
                )
                await restored_engine.download_to_path(uploaded.id, restored_source)
                assert restored_source.read_bytes() == payload
                received = bytearray()
                async for block in restored_engine.iter_bytes(
                    uploaded.id, start=chunk_size - 2, end=chunk_size + 2,
                ):
                    received.extend(block)
                assert bytes(received) == payload[chunk_size - 2:chunk_size + 2]
            finally:
                # Cleanup must remain possible even if the restored snapshot
                # cannot be opened. The original SQL ACK index is committed
                # before any fresh snapshot/reopen attempt.
                try:
                    if refs:
                        cleanup_db = connect_database(str(live_path))
                        try:
                            cleanup_transport = BotApiChunkTransport(
                                BotApiClient(token), BotFileIndex(cleanup_db),
                                chat_id=int(chat_id), account="bot-ci-dr",
                            )
                            for ref in refs:
                                await cleanup_transport.delete(ref)
                        finally:
                            cleanup_db.close()
                finally:
                    if active_db is not None:
                        active_db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("size,chunk_size", [
    pytest.param(0, 2048, id="empty-0B"),
    pytest.param(1, 2048, id="single-byte"),
    pytest.param(2047, 2048, id="chunk-minus-one"),
    pytest.param(2048, 2048, id="exact-chunk"),
    pytest.param(2049, 2048, id="chunk-plus-one"),
    pytest.param(8192, 8192, id="exact-8KiB-chunk"),
    pytest.param(65537, 16384, id="64KiB-plus-one"),
    pytest.param(1048593, 262144, id="1MiB-plus-17"),
    pytest.param(2 * 1024 * 1024 + 17, 1024 * 1024, id="2MiB-plus-17"),
    pytest.param(3 * 1024 * 1024 + 31, 1024 * 1024, id="3MiB-plus-31"),
])
def test_live_bot_real_payload_size_matrix_and_sql_restart(size, chunk_size):
    """Independent real send/getFile/download for empty, boundaries and MiB data.

    Reopen the SQL index before downloading, delete the original source, and
    verify the full SHA-256, every chunk digest, ranges and message ownership.
    """
    from sqlalchemy import select
    from types import SimpleNamespace
    from teledrive.storage.bot_api_adapter import _bot_files

    token = os.getenv("TELEDRIVE_E2E_BOT_TOKEN", "")
    chat_id = os.getenv("TELEDRIVE_E2E_BOT_CHAT_ID", "")
    if not token or not chat_id:
        pytest.skip("Dedicated real Bot API E2E channel not configured")

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-live-bot-sizes-") as folder:
            root = Path(folder)
            db_path = root / "size-matrix.db"
            source = root / "original.bin"
            # Alternating NUL/0xFF catches binary/text conversion bugs;
            # the other size cases remain independent high-entropy data.
            expected = ((bytes((0, 255)) * ((size + 1) // 2))[:size]
                        if size == 65537 else os.urandom(size))
            source.write_bytes(expected)
            database = connect_database(str(db_path))
            api = BotApiClient(token)
            transport = BotApiChunkTransport(
                api, BotFileIndex(database), chat_id=int(chat_id),
                account="bot-ci-size-matrix", min_upload_interval=1.1,
            )
            try:
                await transport.verify_target()
                # Distinct pytest parameter runs create new transport objects.
                # Leave time between their first Telegram posts to respect
                # Telegram's per-chat bot flood control.
                await asyncio.sleep(1.2)
                engine = TelegramObjectStorage(
                    ManifestStore(database), transport, chunk_size=chunk_size,
                    scratch_directory=root,
                )
                obj = await engine.put_file("live-size-ci", f"sizes/{size}.bin", source)
                assert obj.size == size
                assert obj.sha256 == hashlib.sha256(expected).hexdigest()
                references = engine.manifests.parts(obj.id)
                assert len(references) == (size + chunk_size - 1) // chunk_size
                assert sum(part.size for part in references) == size
                for n, part in enumerate(references):
                    assert part.ordinal == n and part.offset == n * chunk_size
                    assert part.sha256 == hashlib.sha256(
                        expected[part.offset:part.offset + part.size]
                    ).hexdigest()
                    assert part.chat_id == int(chat_id)

                # Genuine restart: only Telegram's remote bytes and the SQL
                # persisted file IDs survive after the local source is gone.
                database.close()
                database = connect_database(str(db_path))
                restored_transport = BotApiChunkTransport(
                    BotApiClient(token), BotFileIndex(database),
                    chat_id=int(chat_id), account="bot-ci-size-matrix",
                )
                transport = restored_transport
                restored_engine = TelegramObjectStorage(
                    ManifestStore(database), restored_transport,
                    chunk_size=chunk_size, scratch_directory=root,
                )
                source.unlink()
                recovered = root / "recovered.bin"
                await restored_engine.download_to_path(obj.id, recovered)
                assert recovered.read_bytes() == expected
                assert hashlib.sha256(recovered.read_bytes()).hexdigest() == obj.sha256
                boundaries = [
                    (0, min(size, 17)),
                    (min(size, max(0, chunk_size - 3)), min(size, chunk_size + 5)),
                    (max(0, size - 1), size),
                    (size, size),
                ]
                for start, end in boundaries:
                    blocks = [part async for part in restored_engine.iter_bytes(
                        obj.id, start=start, end=end, block_size=7,
                    )]
                    assert b"".join(blocks) == expected[start:end]
            finally:
                # All file IDs here belong to this disposable per-case SQL DB.
                # This also cleans up messages if an upload fails after ACK but
                # before the main immutable chunk journal was committed.
                try:
                    records = database.connection.execute(select(
                        _bot_files.c.account, _bot_files.c.chat_id,
                        _bot_files.c.message_id,
                    )).all()
                    for account, peer, message_id in records:
                        await transport.delete(SimpleNamespace(
                            account=account, chat_id=int(peer), message_id=int(message_id),
                        ))
                finally:
                    database.close()

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.getenv("TELEDRIVE_E2E_MULTIPART") != "1",
    reason="5/16 MiB real Bot API download-boundary stress is explicitly opt-in",
)
@pytest.mark.parametrize("size", [
    pytest.param(5 * 1024 * 1024 + 1, id="5MiB-plus-one"),
    pytest.param(16 * 1024 * 1024 + 37, id="16MiB-plus-37"),
])
def test_live_bot_large_single_chunk_roundtrip_below_download_cap(size):
    """Real Telegram large-chunk path: verify a single 5/16 MiB Bot document.

    Bot file downloads are capped at 20 MiB; these cases deliberately stay
    below that limit while crossing S3's 5 MiB multipart part boundary.
    """
    token = os.getenv("TELEDRIVE_E2E_BOT_TOKEN", "")
    chat_id = os.getenv("TELEDRIVE_E2E_BOT_CHAT_ID", "")
    if not token or not chat_id:
        pytest.skip("Dedicated real Bot API E2E channel not configured")
    async def scenario():
        from sqlalchemy import select
        from types import SimpleNamespace
        from teledrive.storage.bot_api_adapter import _bot_files
        with tempfile.TemporaryDirectory(prefix="teledrive-live-bot-large-") as folder:
            root = Path(folder)
            data = os.urandom(size)
            original = root / "large.bin"
            original.write_bytes(data)
            conn = connect_database(str(root / "large.db"))
            transport = BotApiChunkTransport(
                BotApiClient(token), BotFileIndex(conn),
                chat_id=int(chat_id), account="bot-ci-large",
            )
            try:
                await transport.verify_target()
                engine = TelegramObjectStorage(
                    ManifestStore(conn), transport, chunk_size=19 * 1024 * 1024,
                    scratch_directory=root,
                )
                obj = await engine.put_file("ci-large", "size-boundary.bin", original)
                refs = engine.manifests.parts(obj.id)
                assert len(refs) == 1 and refs[0].size == size
                assert refs[0].sha256 == hashlib.sha256(data).hexdigest()
                original.unlink()
                destination = root / "fetched.bin"
                await engine.download_to_path(obj.id, destination)
                assert destination.read_bytes() == data
                for start, end in ((0, 1), (size // 2, size // 2 + 17),
                                   (size - 19, size), (size, size)):
                    window = b"".join([block async for block in engine.iter_bytes(
                        obj.id, start=start, end=end,
                    )])
                    assert window == data[start:end]
            finally:
                try:
                    ids = conn.connection.execute(select(
                        _bot_files.c.account, _bot_files.c.chat_id,
                        _bot_files.c.message_id,
                    )).all()
                    for account, peer, mid in ids:
                        await transport.delete(SimpleNamespace(
                            account=account, chat_id=int(peer), message_id=int(mid),
                        ))
                finally:
                    conn.close()
    asyncio.run(scenario())


def test_live_bot_durable_job_queue_restart_and_source_fencing():
    """Actual Bot API, 256 KiB+13 payload, durable SQL worker and fail-closed job.

    The DB is reopened before claiming and again before download. Changing a
    second queued source cannot send new Telegram messages or publish HEAD.
    """
    import time
    from sqlalchemy import select
    from types import SimpleNamespace
    from teledrive.storage.bot_api_adapter import _bot_files
    from teledrive.storage.catalog import BucketCatalog
    from teledrive.storage.jobs import StorageUploadJobs, UploadJobError

    token = os.getenv("TELEDRIVE_E2E_BOT_TOKEN", "")
    chat_id = os.getenv("TELEDRIVE_E2E_BOT_CHAT_ID", "")
    if not token or not chat_id:
        pytest.skip("Dedicated real Bot API test credentials missing")

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-live-job-") as folder:
            root = Path(folder)
            database_path = root / "queue.db"
            payload = os.urandom(256 * 1024 + 13)
            source = root / "job-source.bin"
            source.write_bytes(payload)
            mutated = root / "mutated.bin"
            mutated.write_bytes(b"before-enqueue")
            conn = connect_database(str(database_path))
            transport = None
            try:
                catalog = BucketCatalog(ManifestStore(conn))
                catalog.create_bucket("ci-queued", int(time.time()))
                jobs = StorageUploadJobs(catalog)
                correct_id = jobs.enqueue(
                    "ci-queued", "durable/payload.bin", source, allowed_root=root,
                )
                bad_id = jobs.enqueue(
                    "ci-queued", "durable/mutated.bin", mutated, allowed_root=root,
                )
                assert correct_id != bad_id
                with pytest.raises(FileNotFoundError):
                    catalog.get("ci-queued", "durable/payload.bin")
                # Source mutation rejection is proved after queue snapshot.
                mutated.write_bytes(b"after-enqueue-changed-data")

                conn.close()
                conn = connect_database(str(database_path))
                transport = BotApiChunkTransport(
                    BotApiClient(token), BotFileIndex(conn),
                    chat_id=int(chat_id), account="bot-ci-durable-jobs",
                    min_upload_interval=1.1,
                )
                queue = StorageUploadJobs(BucketCatalog(ManifestStore(conn)))
                # Claim order is deterministic by created_at and then job ID;
                # do not rely on random UUID ordering across equal timestamps.
                results = []
                failures = []
                for _ in range(2):
                    try:
                        results.append(await queue.run_next(
                            transport, allowed_root=root, scratch_directory=root,
                            chunk_size=64 * 1024,
                        ))
                    except UploadJobError as error:
                        failures.append(error)
                assert len(results) == 1 and results[0]["id"] == correct_id
                assert results[0]["state"] == "done"
                assert len(failures) == 1
                assert "Source changed" in str(failures[0])
                states = {row["id"]: row for row in queue.list_jobs()}
                assert states[correct_id]["state"] == "done"
                assert states[bad_id]["state"] == "failed"
                assert states[correct_id]["attempts"] == 1
                assert states[bad_id]["attempts"] == 1
                assert await queue.run_next(
                    transport, allowed_root=root, scratch_directory=root,
                    chunk_size=64 * 1024,
                ) is None

                conn.close()
                conn = connect_database(str(database_path))
                reopened = BucketCatalog(ManifestStore(conn))
                published = reopened.get("ci-queued", "durable/payload.bin")
                assert published.item.id == results[0]["object_id"]
                assert published.item.sha256 == hashlib.sha256(payload).hexdigest()
                assert len(reopened.manifests.parts(published.item.id)) == 5
                with pytest.raises(FileNotFoundError):
                    reopened.get("ci-queued", "durable/mutated.bin")
                source.unlink()
                transport = BotApiChunkTransport(
                    BotApiClient(token), BotFileIndex(conn),
                    chat_id=int(chat_id), account="bot-ci-durable-jobs",
                )
                recovered = root / "recovered.bin"
                await TelegramObjectStorage(
                    reopened.manifests, transport, chunk_size=64 * 1024,
                    scratch_directory=root,
                ).download_to_path(published.item.id, recovered)
                assert recovered.read_bytes() == payload
            finally:
                try:
                    if transport is not None:
                        for account, peer, mid in conn.connection.execute(select(
                            _bot_files.c.account, _bot_files.c.chat_id,
                            _bot_files.c.message_id,
                        )).all():
                            await transport.delete(SimpleNamespace(
                                account=account, chat_id=int(peer), message_id=int(mid),
                            ))
                finally:
                    conn.close()

    asyncio.run(scenario())


def test_live_bot_ack_receipt_recovers_unjournaled_chunk_without_resend():
    """Real Telegram ACK, process crash before manifest append, fenced resume.

    The already-uploaded chunk is fetched and independently compared to the
    immutable source. Resume must send *only* the remaining tail to Telegram.
    """
    import time
    from sqlalchemy import select, update
    from types import SimpleNamespace
    from teledrive.storage.bot_api_adapter import _bot_files
    from teledrive.storage.manifest import writer_fences

    token = os.getenv("TELEDRIVE_E2E_BOT_TOKEN", "")
    chat = os.getenv("TELEDRIVE_E2E_BOT_CHAT_ID", "")
    if not token or not chat:
        pytest.skip("Dedicated Telegram Bot API channel not configured")

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-live-ack-") as folder:
            root = Path(folder)
            db_path = root / "ack.db"
            size = 8192 + 17
            data = os.urandom(size)
            local = root / "source.bin"
            local.write_bytes(data)
            db = connect_database(str(db_path))
            transport = BotApiChunkTransport(
                BotApiClient(token), BotFileIndex(db),
                chat_id=int(chat), account="bot-ci-ack-recovery",
            )
            try:
                await transport.verify_target()
                manifests = ManifestStore(db)
                object_id = manifests.begin(
                    "live-ack", "crash-after-ack.bin", size, int(time.time()),
                )
                manifests.start_writer(
                    object_id, "killed-writer", int(time.time()), ttl=60,
                )
                first_bytes = data[:8192]
                part = root / "first.bin"
                part.write_bytes(first_bytes)
                account, chat_id, message_id = await transport.upload(
                    part, object_id=object_id, ordinal=0,
                    checksum=hashlib.sha256(first_bytes).hexdigest(),
                )
                assert account == "bot-ci-ack-recovery" and chat_id == int(chat)
                assert manifests.recovery_parts(object_id) == []
                # The bot receipt is already durable; the manifest journal
                # and old writer's last heartbeat simulate an abrupt kill.
                db.connection.execute(
                    update(writer_fences).where(
                        writer_fences.c.object_id == object_id,
                    ).values(lease_until=0)
                )
                db.commit()
                db.close()
                db = connect_database(str(db_path))
                transport = BotApiChunkTransport(
                    BotApiClient(token), BotFileIndex(db),
                    chat_id=int(chat), account="bot-ci-ack-recovery",
                )
                resumed = await TelegramObjectStorage(
                    ManifestStore(db), transport, chunk_size=8192,
                    scratch_directory=root,
                ).resume_file("live-ack", "crash-after-ack.bin", local, object_id)
                references = ManifestStore(db).parts(object_id)
                assert resumed.sha256 == hashlib.sha256(data).hexdigest()
                assert len(references) == 2
                assert references[0].message_id == message_id
                assert references[0].sha256 == hashlib.sha256(first_bytes).hexdigest()
                assert len({ref.message_id for ref in references}) == 2
                # Exactly two remote messages exist: the original recovered
                # receipt and one newly transmitted 17-byte tail.
                entries = db.connection.execute(select(
                    _bot_files.c.message_id
                )).scalars().all()
                assert len(entries) == 2 and set(entries) == {
                    ref.message_id for ref in references
                }
                local.unlink()
                destination = root / "restored.bin"
                await TelegramObjectStorage(
                    ManifestStore(db), transport, chunk_size=8192,
                    scratch_directory=root,
                ).download_to_path(object_id, destination)
                assert destination.read_bytes() == data
            finally:
                try:
                    # ACK IDs are collected even on a crash before appending
                    # a chunk; cleanup never touches unrelated channel posts.
                    for account, cid, mid in db.connection.execute(select(
                        _bot_files.c.account, _bot_files.c.chat_id,
                        _bot_files.c.message_id,
                    )).all():
                        await transport.delete(SimpleNamespace(
                            account=account, chat_id=int(cid), message_id=int(mid),
                        ))
                finally:
                    db.close()
    asyncio.run(scenario())


def test_live_bot_rejected_429_then_remote_chunk_sha_and_sql_restart():
    """Inject *only* the 429; actual retry/uploads/readback hit Telegram.

    Do not deliberately flood Telegram to induce a real 429. The first
    sendDocument attempt is rejected locally and never reaches the channel.
    The retry and following chunk are real, indexed and independently read.
    """
    import time
    from sqlalchemy import select
    from types import SimpleNamespace
    from teledrive.storage.bot_api_adapter import (
        BotApiRateLimited, _bot_files,
    )

    token = os.getenv("TELEDRIVE_E2E_BOT_TOKEN", "")
    chat_id = os.getenv("TELEDRIVE_E2E_BOT_CHAT_ID", "")
    if not token or not chat_id:
        pytest.skip("Dedicated Telegram Bot API channel not configured")

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-live-429-") as folder:
            root = Path(folder)
            db_path = root / "rate-limited.db"
            source = root / "source.bin"
            original = os.urandom(128 * 1024 + 13)
            source.write_bytes(original)
            database = connect_database(str(db_path))
            real_api = BotApiClient(token)

            class RejectedFirst:
                def __init__(self):
                    self.attempts = 0
                    self.rejections = 0

                async def call(self, method, fields, source=None):
                    if method == "sendDocument":
                        self.attempts += 1
                        if self.rejections == 0:
                            self.rejections = 1
                            raise BotApiRateLimited(1)
                    return await real_api.call(method, fields, source)

                async def download_file(self, path, destination):
                    return await real_api.download_file(path, destination)

            injected = RejectedFirst()
            transport = BotApiChunkTransport(
                injected, BotFileIndex(database), chat_id=int(chat_id),
                account="bot-ci-429-fencing", min_upload_interval=1.1,
            )
            try:
                await transport.verify_target()
                obj = await TelegramObjectStorage(
                    ManifestStore(database), transport,
                    chunk_size=128 * 1024, scratch_directory=root,
                ).put_file("ci-429", "retry/boundary.bin", source)
                assert injected.rejections == 1
                # Exactly one refused attempt, two confirmed channel posts.
                assert injected.attempts == 3
                refs = ManifestStore(database).parts(obj.id)
                assert len(refs) == 2
                assert [(ref.ordinal, ref.offset, ref.size) for ref in refs] == [
                    (0, 0, 128 * 1024), (1, 128 * 1024, 13),
                ]
                assert len({ref.message_id for ref in refs}) == 2
                assert all(ref.chat_id == int(chat_id) for ref in refs)
                assert obj.sha256 == hashlib.sha256(original).hexdigest()
                persisted = database.connection.execute(select(
                    _bot_files.c.message_id,
                )).scalars().all()
                assert len(persisted) == 2
                assert set(persisted) == {ref.message_id for ref in refs}

                database.close()
                database = connect_database(str(db_path))
                source.unlink()
                # Cleanup must use this reopened BotFileIndex, not the
                # transport bound to the closed pre-restart connection.
                transport = BotApiChunkTransport(
                    real_api, BotFileIndex(database), chat_id=int(chat_id),
                    account="bot-ci-429-fencing",
                )
                after_restart = TelegramObjectStorage(
                    ManifestStore(database), transport,
                    chunk_size=128 * 1024, scratch_directory=root,
                )
                restored = root / "restored.bin"
                await after_restart.download_to_path(obj.id, restored)
                assert restored.read_bytes() == original
                data = b"".join([block async for block in after_restart.iter_bytes(
                    obj.id, start=128 * 1024 - 3, end=128 * 1024 + 5,
                )])
                assert data == original[128 * 1024 - 3:128 * 1024 + 5]
            finally:
                try:
                    for account, peer, mid in database.connection.execute(select(
                        _bot_files.c.account, _bot_files.c.chat_id,
                        _bot_files.c.message_id,
                    )).all():
                        await transport.delete(SimpleNamespace(
                            account=account, chat_id=int(peer),
                            message_id=int(mid),
                        ))
                finally:
                    database.close()

    asyncio.run(scenario())



def test_live_bot_versioned_real_payload_copy_and_revocation_without_remote_resend():
    """Live payload versions, marker, alias restore and logical deletion.

    Multiple actual Telegram uploads of different sizes are recovered using
    only SQL journal + channel file IDs. A server-side historical CopyObject
    must not issue any Telegram send, even when the source VersionId is
    subsequently deleted. The copy's immutable alias remains reachable.
    """
    import time
    from types import SimpleNamespace
    from sqlalchemy import select
    from teledrive.storage.catalog import BucketCatalog
    from teledrive.storage.copy import copy_object
    from teledrive.storage.manifest import object_aliases
    from teledrive.storage.bot_api_adapter import _bot_files
    from teledrive.s3.tagging import ObjectTagStore, TaggingError

    token = os.getenv("TELEDRIVE_E2E_BOT_TOKEN", "")
    chat = os.getenv("TELEDRIVE_E2E_BOT_CHAT_ID", "")
    if not token or not chat:
        pytest.skip("Dedicated real Telegram Bot API channel not configured")

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-live-versions-") as folder:
            root = Path(folder)
            db_path = root / "versions.db"
            payloads = [
                b"",
                os.urandom(65_537),
                os.urandom(262_144 + 19),
            ]
            conn = connect_database(str(db_path))
            bucket, key = "ci-real-versions", "history/İstanbul.bin"
            transport = BotApiChunkTransport(
                BotApiClient(token), BotFileIndex(conn),
                chat_id=int(chat), account="bot-ci-real-versions",
                min_upload_interval=1.1,
            )
            try:
                await transport.verify_target()
                cat = BucketCatalog(ManifestStore(conn))
                cat.create_bucket(bucket, int(time.time()))
                engine = TelegramObjectStorage(
                    cat.manifests, transport, chunk_size=128 * 1024,
                    scratch_directory=root,
                )
                versions = []
                for idx, payload in enumerate(payloads):
                    source = root / f"source-{idx}.bin"
                    source.write_bytes(payload)
                    uploaded = await engine.put_file(bucket, key, source)
                    assert uploaded.sha256 == hashlib.sha256(payload).hexdigest()
                    cat.publish(uploaded.id, now=int(time.time()))
                    versions.append(uploaded.id)
                    source.unlink()
                original_messages = conn.connection.execute(
                    select(_bot_files.c.message_id)
                ).scalars().all()
                assert len(original_messages) == 4
                assert len(set(original_messages)) == 4
                marker = cat.delete_marker(bucket, key)
                assert cat.current_marker(bucket, key) == marker
                assert cat.is_visible_version(bucket, key, versions[1])

                copied = copy_object(
                    cat, bucket, key, bucket, key,
                    source_version_id=versions[1],
                )
                assert copied.source_object_id == versions[1]
                assert cat.get(bucket, key).item.id == copied.object_id
                assert cat.get(bucket, key).item.sha256 == (
                    hashlib.sha256(payloads[1]).hexdigest()
                )
                assert cat.delete_version(bucket, key, versions[1])
                assert not cat.is_visible_version(bucket, key, versions[1])
                with pytest.raises(TaggingError) as denied:
                    ObjectTagStore(cat).replace_published(
                        bucket, key, versions[1], [("tag", "forbidden")]
                    )
                assert denied.value.code == "NoSuchVersion"
                assert conn.connection.execute(select(
                    object_aliases.c.source_object_id
                ).where(object_aliases.c.object_id == copied.object_id)).scalar_one() == versions[1]
                assert conn.connection.execute(
                    select(_bot_files.c.message_id)
                ).scalars().all() == original_messages

                conn.close()
                conn = connect_database(str(db_path))
                transport = BotApiChunkTransport(
                    BotApiClient(token), BotFileIndex(conn),
                    chat_id=int(chat), account="bot-ci-real-versions",
                )
                cat = BucketCatalog(ManifestStore(conn))
                assert cat.get(bucket, key).item.id == copied.object_id
                assert cat.is_visible_version(bucket, key, versions[0])
                assert cat.is_visible_version(bucket, key, versions[2])
                assert not cat.is_visible_version(bucket, key, versions[1])
                engine = TelegramObjectStorage(
                    cat.manifests, transport, chunk_size=128 * 1024,
                    scratch_directory=root,
                )
                for ident, expected in [
                    (versions[0], payloads[0]),
                    (versions[2], payloads[2]),
                    (copied.object_id, payloads[1]),
                ]:
                    recovered = root / (ident + ".bin")
                    await engine.download_to_path(ident, recovered)
                    assert recovered.read_bytes() == expected
                    assert hashlib.sha256(recovered.read_bytes()).hexdigest() == (
                        hashlib.sha256(expected).hexdigest()
                    )
                    for start, stop in (
                        (0, min(len(expected), 9)),
                        (max(0, len(expected) - 8), len(expected)),
                        (len(expected), len(expected)),
                    ):
                        fragment = b"".join([piece async for piece in engine.iter_bytes(
                            ident, start=start, end=stop, block_size=7,
                        )])
                        assert fragment == expected[start:stop]
            finally:
                try:
                    # Collect only messages indexed by this disposable test
                    # database, including an ACK before journal persistence.
                    cleanup_conn = conn
                    for account, peer, mid in cleanup_conn.connection.execute(select(
                        _bot_files.c.account, _bot_files.c.chat_id,
                        _bot_files.c.message_id,
                    )).all():
                        await transport.delete(SimpleNamespace(
                            account=account, chat_id=int(peer),
                            message_id=int(mid),
                        ))
                finally:
                    conn.close()
    asyncio.run(scenario())



def test_live_bot_five_durable_jobs_three_sql_workers_different_sizes():
    """Bounded real Telegram workload, independent SQL workers and byte readback.

    Three workers share a durable SQLite WAL database but own separate SQL
    connections and Bot transports. Send operations are throttled *across*
    those transports in this E2E test to stay within channel flood limits;
    job claims and SQL publication genuinely overlap during Telegram I/O.
    """
    import time
    from sqlalchemy import select
    from types import SimpleNamespace
    from teledrive.storage.catalog import BucketCatalog
    from teledrive.storage.jobs import StorageUploadJobs
    from teledrive.storage.bot_api_adapter import _bot_files

    token = os.getenv("TELEDRIVE_E2E_BOT_TOKEN", "")
    chat = os.getenv("TELEDRIVE_E2E_BOT_CHAT_ID", "")
    if not token or not chat:
        pytest.skip("Dedicated Telegram Bot API E2E channel not configured")

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-live-3workers-") as folder:
            root = Path(folder)
            dbpath = root / "workers.db"
            payloads = {f"batch/{size}.bin": os.urandom(size)
                        for size in (0, 1, 8193, 65537, 131073)}
            seed_db = connect_database(str(dbpath))
            try:
                catalog = BucketCatalog(ManifestStore(seed_db))
                catalog.create_bucket("ci-real-workers", int(time.time()))
                manager = StorageUploadJobs(catalog)
                for key, content in payloads.items():
                    filename = root / key.replace("/", "-")
                    filename.write_bytes(content)
                    manager.enqueue(
                        "ci-real-workers", key, filename,
                        allowed_root=root,
                    )
            finally:
                seed_db.close()

            api = BotApiClient(token)
            gate = asyncio.Lock()
            posted = []

            class SerializedTelegram:
                async def call(self, method, fields, source=None):
                    if method != "sendDocument":
                        return await api.call(method, fields, source)
                    async with gate:
                        result = await api.call(method, fields, source)
                        posted.append(int(result["message_id"]))
                        await asyncio.sleep(1.3)
                        return result

                async def download_file(self, remote_path, destination):
                    await api.download_file(remote_path, destination)

            try:
                connections = []
                results = []
                cleanup = None
                try:
                    for _ in range(3):
                        connection = connect_database(str(dbpath))
                        connections.append(connection)
                        # Schema initialization completes before workers race.
                        StorageUploadJobs(BucketCatalog(ManifestStore(connection)))
                        connection.commit()
                    transport_api = SerializedTelegram()
                    await BotApiChunkTransport(
                        transport_api, BotFileIndex(connections[0]),
                        chat_id=int(chat), account="bot-ci-3workers",
                    ).verify_target()

                    async def worker(connection):
                        queue = StorageUploadJobs(BucketCatalog(ManifestStore(connection)))
                        transport = BotApiChunkTransport(
                            transport_api, BotFileIndex(connection),
                            chat_id=int(chat), account="bot-ci-3workers",
                            min_upload_interval=0,
                        )
                        owned = []
                        for _ in range(8):
                            result = await queue.run_next(
                                transport, allowed_root=root,
                                scratch_directory=root, chunk_size=65536,
                            )
                            if result is None:
                                break
                            owned.append(result)
                        return owned

                    produced = await asyncio.gather(
                        *(worker(connection) for connection in connections)
                    )
                    results = [item for worker_rows in produced for item in worker_rows]
                    assert len(results) == len(payloads), results
                    assert len({item["id"] for item in results}) == len(payloads)
                    assert all(item["state"] == "done" for item in results)
                    assert len(posted) == 7 and len(set(posted)) == 7
                finally:
                    for connection in connections:
                        connection.close()

                cleanup = connect_database(str(dbpath))
                try:
                    catalog = BucketCatalog(ManifestStore(cleanup))
                    queue = StorageUploadJobs(catalog)
                    jobs = queue.list_jobs(limit=100)
                    assert len(jobs) == 5
                    assert all(job["state"] == "done" and job["attempts"] == 1
                               for job in jobs)
                    transport = BotApiChunkTransport(
                        api, BotFileIndex(cleanup), chat_id=int(chat),
                        account="bot-ci-3workers",
                    )
                    engine = TelegramObjectStorage(
                        catalog.manifests, transport, chunk_size=65536,
                        scratch_directory=root,
                    )
                    for key, expected in payloads.items():
                        obj = catalog.get("ci-real-workers", key).item
                        assert obj.sha256 == hashlib.sha256(expected).hexdigest()
                        (root / key.replace("/", "-")).unlink()
                        restored = root / ("restored-" + str(len(expected)) + ".bin")
                        await engine.download_to_path(obj.id, restored)
                        assert restored.read_bytes() == expected
                        for start, stop in (
                            (0, min(13, len(expected))),
                            (max(0, len(expected)-7), len(expected)),
                            (len(expected), len(expected)),
                        ):
                            window = b"".join([block async for block in engine.iter_bytes(
                                obj.id, start=start, end=stop, block_size=7,
                            )])
                            assert window == expected[start:stop]
                finally:
                    # The independent receipt index, not test-local filenames or
                    # guessed Telegram history, strictly scopes physical deletion.
                    try:
                        if cleanup is not None:
                            transport = BotApiChunkTransport(
                                api, BotFileIndex(cleanup), chat_id=int(chat),
                                account="bot-ci-3workers",
                            )
                            for account, peer, message in cleanup.connection.execute(select(
                                _bot_files.c.account, _bot_files.c.chat_id,
                                _bot_files.c.message_id,
                            )).all():
                                await transport.delete(SimpleNamespace(
                                    account=account, chat_id=int(peer),
                                    message_id=int(message),
                                ))
                    finally:
                        if cleanup is not None:
                            cleanup.close()

            finally:
                # Always retry test-owned cleanup, including a worker error
                # before result verification. No Telegram history scan or
                # broad channel deletion is ever performed.
                emergency_db = connect_database(str(dbpath))
                try:
                    emergency_transport = BotApiChunkTransport(
                        api, BotFileIndex(emergency_db), chat_id=int(chat),
                        account="bot-ci-3workers",
                    )
                    pending_ids = emergency_db.connection.execute(select(
                        _bot_files.c.account, _bot_files.c.chat_id,
                        _bot_files.c.message_id,
                    )).all()
                    for account, peer, message in pending_ids:
                        await emergency_transport.delete(SimpleNamespace(
                            account=account, chat_id=int(peer),
                            message_id=int(message),
                        ))
                finally:
                    emergency_db.close()

    asyncio.run(scenario())

def test_live_bot_accepted_upload_lost_ack_is_fenced_until_proven():
    """Real channel ACK-loss drill: persist intent, never send same part twice.

    The deliberately withheld ACK is stored ONLY by this test's fault
    injector. Its original Telegram file ID must be independently
    downloaded and matched before explicitly recording a recovery receipt.
    Production Bot API alone cannot reconstruct arbitrary lost channel ACKs.
    """
    import time
    from sqlalchemy import select, update
    from teledrive.storage.manifest import writer_fences
    from teledrive.storage.bot_api_adapter import BotApiError, _bot_files

    token = os.getenv("TELEDRIVE_E2E_BOT_TOKEN", "")
    chat = os.getenv("TELEDRIVE_E2E_BOT_CHAT_ID", "")
    if not token or not chat:
        pytest.skip("Dedicated real Telegram Bot API channel not configured")

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-live-lost-ack-") as folder:
            root = Path(folder)
            db_path = root / "intents.db"
            payload = os.urandom(64 * 1024 + 73)
            part_size = 64 * 1024
            source = root / "original.bin"
            source.write_bytes(payload)
            part = root / "first.bin"
            part.write_bytes(payload[:part_size])
            checksum = hashlib.sha256(part.read_bytes()).hexdigest()
            real = BotApiClient(token)
            remote_ack = []
            sends = []

            class WithholdOneAck:
                async def call(self, method, fields, source=None):
                    result = await real.call(method, fields, source)
                    if method == "sendDocument":
                        sends.append(int(result["message_id"]))
                        if not remote_ack:
                            remote_ack.append(result)
                            raise BotApiError("Bot API sendDocument request failed")
                    return result

                async def download_file(self, remote_path, destination):
                    return await real.download_file(remote_path, destination)

            db = connect_database(str(db_path))
            try:
                manifests = ManifestStore(db)
                object_id = manifests.begin(
                    "live-intent", "ACK-lost.bin", len(payload), int(time.time()),
                )
                manifests.start_writer(
                    object_id, "interrupted-writer", int(time.time()), ttl=60,
                )
                transport = BotApiChunkTransport(
                    WithholdOneAck(), BotFileIndex(db), chat_id=int(chat),
                    account="bot-live-lost-ack", min_upload_interval=1.1,
                )
                await transport.verify_target()
                with pytest.raises(BotApiError, match="request failed"):
                    await transport.upload(
                        part, object_id=object_id, ordinal=0, checksum=checksum,
                    )
                assert len(sends) == 1
                assert BotFileIndex(db).unresolved_count() == 1
                assert manifests.recovery_parts(object_id) == []

                db.connection.execute(update(writer_fences).where(
                    writer_fences.c.object_id == object_id,
                ).values(lease_until=0))
                db.commit()
                db.close()
                db = connect_database(str(db_path))
                transport = BotApiChunkTransport(
                    real, BotFileIndex(db), chat_id=int(chat),
                    account="bot-live-lost-ack", min_upload_interval=1.1,
                )
                index = transport.index
                with pytest.raises(BotApiError, match="Unresolved Bot API send intent"):
                    index.find_receipt(
                        object_id, 0, account=transport.account, chat_id=int(chat),
                        checksum=checksum, size=part_size,
                    )
                with pytest.raises(BotApiError, match="Unresolved Bot API send intent"):
                    await transport.upload(
                        part, object_id=object_id, ordinal=0, checksum=checksum,
                    )
                assert len(sends) == 1

                # External operator proof is simulated here using the ACK the
                # injector had retained. Fetch actual Telegram bytes, verify
                # checksum and channel ID, THEN durably record the receipt.
                acknowledged = remote_ack[0]
                assert int(acknowledged["chat"]["id"]) == int(chat)
                file_id = acknowledged["document"]["file_id"]
                remote_meta = await real.call("getFile", {"file_id": file_id})
                remote_path = root / "proof.bin"
                await real.download_file(remote_meta["file_path"], remote_path)
                assert remote_path.read_bytes() == payload[:part_size]
                assert hashlib.sha256(remote_path.read_bytes()).hexdigest() == checksum
                index.put_receipt(
                    transport.account, int(chat), int(acknowledged["message_id"]),
                    file_id, object_id=object_id, ordinal=0,
                    checksum=checksum, size=part_size,
                )
                assert index.unresolved_count() == 0
                completed = await TelegramObjectStorage(
                    ManifestStore(db), transport, chunk_size=part_size,
                    scratch_directory=root,
                ).resume_file("live-intent", "ACK-lost.bin", source, object_id)
                assert completed.sha256 == hashlib.sha256(payload).hexdigest()
                refs = ManifestStore(db).parts(object_id)
                assert len(refs) == 2
                assert refs[0].message_id == acknowledged["message_id"]
                assert refs[0].sha256 == checksum

                db.close()
                db = connect_database(str(db_path))
                source.unlink()
                final_transport = BotApiChunkTransport(
                    real, BotFileIndex(db), chat_id=int(chat),
                    account="bot-live-lost-ack",
                )
                restored = root / "verified-remote.bin"
                await TelegramObjectStorage(
                    ManifestStore(db), final_transport, chunk_size=part_size,
                    scratch_directory=root,
                ).download_to_path(object_id, restored)
                assert restored.read_bytes() == payload
                assert len(db.connection.execute(select(_bot_files.c.message_id)).all()) == 2
            finally:
                try:
                    # Only this invocation's two positively known Telegram IDs.
                    # The first message is recorded before a simulated lost ACK.
                    ids = set(sends)
                    for _, _, message_id in db.connection.execute(select(
                        _bot_files.c.account, _bot_files.c.chat_id,
                        _bot_files.c.message_id,
                    )).all():
                        ids.add(int(message_id))
                    for message_id in sorted(ids):
                        await real.call("deleteMessage", {
                            "chat_id": int(chat), "message_id": message_id,
                        })
                finally:
                    db.close()

    asyncio.run(scenario())

@pytest.mark.skipif(
    os.getenv("TELEDRIVE_E2E_SOAK") != "1",
    reason="Bounded real Bot API mixed-payload soak is manual-only",
)
def test_live_bot_bounded_mixed_binary_soak_sql_reopens_and_range_integrity():
    """Manual real test: twelve distinct objects, multiple SQL generations.

    Telegram posts are tracked before SQL persistence. This test intentionally
    avoids uncontrolled 429 spam or physical deletion of non-test messages.
    """
    import time
    from sqlalchemy import select
    from teledrive.storage.bot_api_adapter import _bot_files

    token = os.getenv("TELEDRIVE_E2E_BOT_TOKEN", "")
    chat = os.getenv("TELEDRIVE_E2E_BOT_CHAT_ID", "")
    if not token or not chat:
        pytest.skip("Dedicated Bot API test channel not configured")

    sizes = (
        0, 1, 2047, 2048, 2049, 65537, 262144 + 13,
        1024 * 1024 - 1, 1024 * 1024, 1024 * 1024 + 17,
        2 * 1024 * 1024 + 17, 3 * 1024 * 1024 + 31,
    )
    chunk_size = 1024 * 1024

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-live-soak-") as folder:
            root = Path(folder)
            db_path = root / "soak.db"
            db = connect_database(str(db_path))
            real_api = BotApiClient(token)
            uploaded_ids = set()

            class OwnedTracked:
                async def call(self, method, fields, source=None):
                    response = await real_api.call(method, fields, source)
                    if method == "sendDocument":
                        assert int(response["chat"]["id"]) == int(chat)
                        uploaded_ids.add(int(response["message_id"]))
                    return response

                async def download_file(self, remote_path, destination):
                    return await real_api.download_file(remote_path, destination)

            def setup(current):
                transport = BotApiChunkTransport(
                    OwnedTracked(), BotFileIndex(current),
                    chat_id=int(chat), account="bot-ci-bounded-soak",
                    min_upload_interval=1.1,
                )
                engine = TelegramObjectStorage(
                    ManifestStore(current), transport,
                    chunk_size=chunk_size, scratch_directory=root,
                )
                return transport, engine

            objects = []
            try:
                transport, engine = setup(db)
                await transport.verify_target()
                for n, size in enumerate(sizes):
                    if n and n % 3 == 0:
                        db.close()
                        db = connect_database(str(db_path))
                        transport, engine = setup(db)
                    content = (bytes((0, 255)) * ((size + 1) // 2))[:size] if n % 3 == 0 else os.urandom(size)
                    key = f"soak/İstanbul-{n}-{size}.bin"
                    source = root / f"source-{n}.bin"
                    source.write_bytes(content)
                    item = await engine.put_file("ci-mixed-soak", key, source)
                    assert item.sha256 == hashlib.sha256(content).hexdigest()
                    parts = ManifestStore(db).parts(item.id)
                    assert len(parts) == (size + chunk_size - 1) // chunk_size
                    assert sum(part.size for part in parts) == size
                    assert [(p.ordinal, p.offset) for p in parts] == [
                        (idx, idx * chunk_size) for idx in range(len(parts))
                    ]
                    source.unlink()
                    objects.append((item.id, content, item.sha256))
                # Reopen once more: readback requires only durable SQL file IDs
                # plus Telegram's actual remote document bytes.
                db.close()
                db = connect_database(str(db_path))
                transport, engine = setup(db)
                for object_id, content, checksum in objects:
                    destination = root / f"{object_id}.download"
                    await engine.download_to_path(object_id, destination)
                    assert destination.read_bytes() == content
                    assert hashlib.sha256(destination.read_bytes()).hexdigest() == checksum
                    size = len(content)
                    for start, end in (
                        (0, min(17, size)),
                        (max(0, chunk_size - 3), min(size, chunk_size + 5)),
                        (max(0, size - 31), size),
                        (size, size),
                    ):
                        if start > end:
                            continue
                        received = b"".join([
                            chunk async for chunk in engine.iter_bytes(
                                object_id, start=start, end=end, block_size=7,
                            )
                        ])
                        assert received == content[start:end]
                indexed = set(db.connection.execute(
                    select(_bot_files.c.message_id),
                ).scalars().all())
                assert indexed == uploaded_ids
                assert BotFileIndex(db).unresolved_count() == 0
            finally:
                try:
                    # Own-message-only cleanup also covers an upload that
                    # was ACKed but failed during SQL journal persistence.
                    for mid in sorted(uploaded_ids):
                        await real_api.call("deleteMessage", {
                            "chat_id": int(chat), "message_id": int(mid),
                        })
                finally:
                    db.close()

    asyncio.run(scenario())
