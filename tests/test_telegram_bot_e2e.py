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


def test_live_bot_sqlite_snapshot_restores_remote_telegram_object():
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
            payload = os.urandom(3071)
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
                    catalog.manifests, transport, chunk_size=2048,
                    scratch_directory=root,
                ).put_file("live-dr-ci", "snapshots/file.bin", source)
                refs = catalog.manifests.parts(uploaded.id)
                assert len(refs) == 2
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
                    chunk_size=2048, scratch_directory=root,
                )
                await restored_engine.download_to_path(uploaded.id, restored_source)
                assert restored_source.read_bytes() == payload
                received = bytearray()
                async for block in restored_engine.iter_bytes(
                    uploaded.id, start=2047, end=2051,
                ):
                    received.extend(block)
                assert bytes(received) == payload[2047:2051]
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
    pytest.param(65537, 16384, id="64KiB-plus-one"),
    pytest.param(1048593, 262144, id="1MiB-plus-17"),
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
            expected = os.urandom(size)
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
