"""Dedicated real Telegram test-channel candidate inventory, never production history."""
from __future__ import annotations
import asyncio
import hashlib
import os
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from telethon import TelegramClient
from telethon.sessions import StringSession
from teledrive.storage.history_inventory import inventory_channel
from teledrive.storage.telethon_adapter import TelethonChunkTransport
from teledrive.storage.target_resolver import resolve_telegram_target


def test_real_test_channel_discovery_without_inventory_mutations():
    api_id = os.getenv("TELEDRIVE_E2E_API_ID", "")
    api_hash = os.getenv("TELEDRIVE_E2E_API_HASH", "")
    session = os.getenv("TELEDRIVE_E2E_SESSION", "")
    if not all((api_id, api_hash, session)):
        pytest.skip("Dedicated E2E session is not configured")

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-discovery-ci-") as folder:
            source = Path(folder) / "fixture.bin"
            body = b"CI owned marker fixture " + os.urandom(32)
            source.write_bytes(body)
            client = TelegramClient(StringSession(session), int(api_id), api_hash)
            target, sent = None, None
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError("Telegram test session is not authorized")
                target = await resolve_telegram_target(
                    client, os.getenv("TELEDRIVE_E2E_TARGET"),
                    os.getenv("TELEDRIVE_E2E_CHANNEL_NAME"),
                )
                ident = str(uuid4())
                digest = hashlib.sha256(body).hexdigest()
                transport = TelethonChunkTransport({"ci": (client, target)})
                sent = await transport.upload(
                    source, object_id=ident, ordinal=0, checksum=digest,
                )
                report = await inventory_channel(
                    client, target, account="ci", scan_limit=40,
                )
                candidate = next(o for o in report["candidate_objects"]
                                 if o["object_id"] == ident)
                assert candidate["candidate_ordinals_contiguous"] is True
                assert candidate["chunks"] == [{
                    "ordinal": 0, "checksum_prefix": digest[:16],
                    "account": sent[0], "chat_id": sent[1], "message_id": sent[2],
                }]
                assert report["payload_downloads"] == 0
                assert report["telegram_mutations"] == 0
                assert report["restorable_without_independent_manifest"] is False
                assert await client.get_messages(target, ids=sent[2]) is not None
            finally:
                try:
                    if target is not None and sent is not None:
                        assert int(await client.get_peer_id(target)) == sent[1]
                        await client.delete_messages(target, [sent[2]], revoke=True)
                finally:
                    await client.disconnect()

    asyncio.run(scenario())
