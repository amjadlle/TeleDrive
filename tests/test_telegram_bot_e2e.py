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

    Completes two parts; never duplicates them on Telegram. This validates the
    storage integration behind the boto3+HTTP multipart contract tests.
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
                finished = await service.complete(
                    upload_id, bucket, "live/large.bin", "ci-live-access",
                    selection, engine,
                )
                assert finished.size == len(first) + len(last)
                assert len(manifests.parts(finished.object_id)) == 2
                restored = root / "restored.bin"
                await engine.download_to_path(finished.object_id, restored)
                assert restored.read_bytes() == first + last
                assert catalog.get(bucket, "live/large.bin").item.id == finished.object_id
            finally:
                # Explicitly clean only messages uploaded in this test. The
                # service itself never deletes them during multipart Complete.
                try:
                    for ref in refs:
                        await transport.delete(ref)
                finally:
                    database.close()

    asyncio.run(scenario())
