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
