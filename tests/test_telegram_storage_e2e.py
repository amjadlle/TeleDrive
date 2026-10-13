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
                target = await resolve_telegram_target(
                    client,
                    os.getenv("TELEDRIVE_E2E_TARGET"),
                    os.getenv("TELEDRIVE_E2E_CHANNEL_NAME"),
                )
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


def test_real_telegram_unindexed_chunk_cleanup():
    """Manual E2E: Telegram accepted upload, DB journal commit was lost.

    Only the message created in this test is eligible for deletion.
    """
    import time
    from teledrive.storage.recovery import RecoveryManager

    api_id = os.getenv("TELEDRIVE_E2E_API_ID", "")
    api_hash = os.getenv("TELEDRIVE_E2E_API_HASH", "")
    session = os.getenv("TELEDRIVE_E2E_SESSION", "")
    if not all((api_id, api_hash, session)):
        pytest.skip("Manual E2E requires dedicated Telegram test credentials")

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-recovery-e2e-") as folder:
            root = Path(folder)
            upload = root / "orphan.bin"
            body = os.urandom(87)
            upload.write_bytes(body)
            conn = connect_database(str(root / "manifest.db"))
            store = ManifestStore(conn)
            client = TelegramClient(StringSession(session), int(api_id), api_hash)
            await client.connect()
            uploaded_id = None
            target = None
            try:
                if not await client.is_user_authorized():
                    raise RuntimeError("E2E Telegram test session not authorized")
                target = await resolve_telegram_target(
                    client,
                    os.getenv("TELEDRIVE_E2E_TARGET"),
                    os.getenv("TELEDRIVE_E2E_CHANNEL_NAME"),
                )
                transport = TelethonChunkTransport({"ci": (client, target)})
                old = int(time.time()) - 7200
                identifier = store.begin("ci", "unindexed-test", len(body), old)
                digest = hashlib.sha256(body).hexdigest()
                account, chat_id, uploaded_id = await transport.upload(
                    upload, object_id=identifier, ordinal=0, checksum=digest
                )
                assert account == "ci" and chat_id != 0 and uploaded_id > 0
                # Simulate a crash between Telegram's successful response
                # and SQL append, without relying on mocks.
                store.fail(identifier, "simulated metadata commit crash", old)
                manager = RecoveryManager(store, transport, min_age_seconds=3600)
                preview = await manager.cleanup(identifier)
                assert preview.orphaned == 1 and preview.deleted == 0
                assert await client.get_messages(target, ids=uploaded_id) is not None
                done = await manager.cleanup(identifier, dry_run=False)
                assert done.cleaned and done.deleted == 1
                assert await client.get_messages(target, ids=uploaded_id) is None
            finally:
                if uploaded_id is not None and target is not None:
                    await client.delete_messages(target, [uploaded_id], revoke=True)
                await client.disconnect()
                conn.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("size,chunk_size", [
    pytest.param(0, 2048, id="empty-0B"),
    pytest.param(1, 2048, id="single-byte"),
    pytest.param(2048, 2048, id="exact-chunk"),
    pytest.param(2049, 2048, id="chunk-plus-one"),
    pytest.param(65537, 32768, id="64KiB-plus-one"),
    pytest.param(1048593, 524288, id="1MiB-plus-17"),
])
def test_real_telethon_varied_payload_lengths_with_remote_readback(size, chunk_size):
    """Live user-account Telegram: SHA of every remote part and crossed ranges.

    Every send ACK is tracked *before* the manifest journal: even an assertion
    failure during publish attempts to delete only this test's new messages.
    """
    api_id = os.getenv("TELEDRIVE_E2E_API_ID", "")
    api_hash = os.getenv("TELEDRIVE_E2E_API_HASH", "")
    session = os.getenv("TELEDRIVE_E2E_SESSION", "")
    if not all((api_id, api_hash, session)):
        pytest.skip("Dedicated real Telegram user account E2E not configured")

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-user-size-matrix-") as folder:
            root = Path(folder)
            source = root / "source.bin"
            expected = os.urandom(size)
            source.write_bytes(expected)
            db_path = root / "manifest.db"
            conn = connect_database(str(db_path))
            client = TelegramClient(StringSession(session), int(api_id), api_hash)
            target = None
            uploaded = []
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError("E2E Telegram test session expired")
                target = await resolve_telegram_target(
                    client, os.getenv("TELEDRIVE_E2E_TARGET"),
                    os.getenv("TELEDRIVE_E2E_CHANNEL_NAME"),
                )
                class TrackedTelegram(TelethonChunkTransport):
                    async def upload(self, path, *, object_id, ordinal, checksum):
                        result = await super().upload(
                            path, object_id=object_id,
                            ordinal=ordinal, checksum=checksum,
                        )
                        uploaded.append(result)
                        # Avoid uncontrolled bursts to a shared CI channel.
                        await asyncio.sleep(0.75)
                        return result

                transport = TrackedTelegram({"ci": (client, target)})
                engine = TelegramObjectStorage(
                    ManifestStore(conn), transport, chunk_size=chunk_size,
                    scratch_directory=root,
                )
                obj = await engine.put_file("ci", f"live/sizes/{size}.bin", source)
                assert obj.size == size
                assert obj.sha256 == hashlib.sha256(expected).hexdigest()
                parts = engine.manifests.parts(obj.id)
                assert len(parts) == (size + chunk_size - 1) // chunk_size
                assert len(uploaded) == len(parts)
                assert sum(p.size for p in parts) == size
                for index, part in enumerate(parts):
                    assert part.ordinal == index and part.offset == index * chunk_size
                    assert part.sha256 == hashlib.sha256(
                        expected[part.offset:part.offset + part.size]
                    ).hexdigest()
                    assert (part.account, part.chat_id, part.message_id) == uploaded[index]

                # Reconstruct from SQL + Telegram on a clean database session.
                conn.close()
                conn = connect_database(str(db_path))
                source.unlink()
                recreated = TelegramObjectStorage(
                    ManifestStore(conn), TrackedTelegram({"ci": (client, target)}),
                    chunk_size=chunk_size, scratch_directory=root,
                )
                destination = root / "downloaded.bin"
                await recreated.download_to_path(obj.id, destination)
                assert destination.read_bytes() == expected
                for start, end in (
                    (0, min(size, 15)),
                    (min(size, max(0, chunk_size - 2)), min(size, chunk_size + 9)),
                    (max(0, size - 1), size),
                    (size, size),
                ):
                    blocks = [b async for b in recreated.iter_bytes(
                        obj.id, start=start, end=end, block_size=7,
                    )]
                    assert b"".join(blocks) == expected[start:end]
            finally:
                try:
                    if target is not None:
                        for _, chat, message in uploaded:
                            assert int(await client.get_peer_id(target)) == chat
                            await client.delete_messages(
                                target, [message], revoke=True,
                            )
                finally:
                    await client.disconnect()
                    conn.close()

    asyncio.run(scenario())
