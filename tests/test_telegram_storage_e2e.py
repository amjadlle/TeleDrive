"""Manual-only Telegram round trip. Requires dedicated test account credentials.

No other CI job requires these secrets. Do not point at a personal account.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

import pytest

from teledrive.db import connect_database
from teledrive.storage import ManifestStore, TelegramObjectStorage
from teledrive.storage.telethon_adapter import TelethonChunkTransport
from teledrive.storage.target_resolver import resolve_telegram_target


def test_real_telegram_upload_download_and_range():
    api_id = os.getenv("TELEDRIVE_E2E_API_ID", "")
    api_hash = os.getenv("TELEDRIVE_E2E_API_HASH", "")
    session = os.getenv("TELEDRIVE_E2E_SESSION", "")
    if not all((api_id, api_hash, session)):
        pytest.skip("Manual E2E requires dedicated Telegram test credentials")

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-e2e-") as folder:
            root = Path(folder)
            original = root / "input.bin"
            content = os.urandom(4097)
            original.write_bytes(content)
            conn = connect_database(str(root / "manifest.db"))
            store = ManifestStore(conn)
            client = TelegramClient(StringSession(session), int(api_id), api_hash)
            await client.connect()
            uploaded_refs = []
            target = None
            try:
                if not await client.is_user_authorized():
                    raise RuntimeError("E2E StringSession is expired or not authorized")
                target = await resolve_telegram_target(client, os.getenv("TELEDRIVE_E2E_TARGET"))
                backend = TelegramObjectStorage(
                    store, TelethonChunkTransport({"ci": (client, target)}),
                    chunk_size=2048,
                )
                item = await backend.put_file("ci", "ci/roundtrip.bin", original)
                uploaded_refs = store.parts(item.id)
                original.unlink()

                output = root / "restored.bin"
                await backend.download_to_path(item.id, output)
                assert output.read_bytes() == content
                assert hashlib.sha256(output.read_bytes()).hexdigest() == item.sha256

                range_bytes = bytearray()
                async for part in backend.iter_bytes(item.id, start=2047, end=2051):
                    range_bytes.extend(part)
                assert bytes(range_bytes) == content[2047:2051]
                assert len(uploaded_refs) == 3
            finally:
                # Cleanup is scoped to the test target and only messages
                # uploaded by this test invocation.
                for reference in uploaded_refs:
                    if target is not None:
                        await client.delete_messages(target, [reference.message_id])
                await client.disconnect()
                conn.close()

    asyncio.run(scenario())
