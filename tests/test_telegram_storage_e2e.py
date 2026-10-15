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
    pytest.param(2047, 2048, id="chunk-minus-one"),
    pytest.param(2048, 2048, id="exact-chunk"),
    pytest.param(2049, 2048, id="chunk-plus-one"),
    pytest.param(65537, 32768, id="64KiB-plus-one"),
    pytest.param(1048593, 524288, id="1MiB-plus-17"),
    pytest.param(2 * 1024 * 1024 + 17, 1024 * 1024, id="2MiB-plus-17"),
    pytest.param(3 * 1024 * 1024 + 31, 1024 * 1024, id="3MiB-plus-31"),
    pytest.param(4 * 1024 * 1024 + 1, 1024 * 1024, id="4MiB-plus-one-five-chunks"),
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
            # Binary 0x00/0xFF alternation supplements random payloads.
            expected = ((bytes((0, 255)) * ((size + 1) // 2))[:size]
                        if size == 65537 else os.urandom(size))
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


@pytest.mark.skipif(
    os.getenv("TELEDRIVE_E2E_STRESS") != "1",
    reason="5/16 MiB live Telethon size stress explicitly opt-in",
)
@pytest.mark.parametrize("size", [
    pytest.param(5 * 1024 * 1024 + 1, id="5MiB-plus-one"),
    pytest.param(16 * 1024 * 1024 + 17, id="16MiB-plus-17"),
])
def test_live_telethon_large_payload_chunk_boundaries_and_remote_restore(size):
    """Real user Telegram uploads >5MiB, downloads every chunk after SQL restart."""
    api_id = os.getenv("TELEDRIVE_E2E_API_ID", "")
    api_hash = os.getenv("TELEDRIVE_E2E_API_HASH", "")
    session = os.getenv("TELEDRIVE_E2E_SESSION", "")
    if not all((api_id, api_hash, session)):
        pytest.skip("Dedicated test account and channel required")
    chunk_size = 4 * 1024 * 1024

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-user-large-") as folder:
            root = Path(folder)
            db_path = root / "large.db"
            original = root / "payload.bin"
            payload = os.urandom(size)
            original.write_bytes(payload)
            db = connect_database(str(db_path))
            client = TelegramClient(StringSession(session), int(api_id), api_hash)
            target = None
            acked = []
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError("Telethon test session is no longer authorized")
                target = await resolve_telegram_target(
                    client, os.getenv("TELEDRIVE_E2E_TARGET"),
                    os.getenv("TELEDRIVE_E2E_CHANNEL_NAME"),
                )

                class OwnedRemote(TelethonChunkTransport):
                    async def upload(self, path, *, object_id, ordinal, checksum):
                        result = await super().upload(
                            path, object_id=object_id,
                            ordinal=ordinal, checksum=checksum,
                        )
                        acked.append(result)
                        await asyncio.sleep(0.75)
                        return result

                transport = OwnedRemote({"ci": (client, target)})
                engine = TelegramObjectStorage(
                    ManifestStore(db), transport, chunk_size=chunk_size,
                    scratch_directory=root,
                )
                item = await engine.put_file("ci", "p1/large.bin", original)
                parts = engine.manifests.parts(item.id)
                assert len(parts) == (size + chunk_size - 1) // chunk_size
                assert len(parts) == len(acked)
                assert item.sha256 == hashlib.sha256(payload).hexdigest()
                for index, part in enumerate(parts):
                    fragment = payload[part.offset:part.offset + part.size]
                    assert part.ordinal == index and part.size == len(fragment)
                    assert part.offset == index * chunk_size
                    assert part.sha256 == hashlib.sha256(fragment).hexdigest()
                    assert (part.account, part.chat_id, part.message_id) == acked[index]

                # No original file or in-memory source from the first SQL
                # session is used by this recovery path.
                db.close()
                db = connect_database(str(db_path))
                original.unlink()
                new_engine = TelegramObjectStorage(
                    ManifestStore(db), OwnedRemote({"ci": (client, target)}),
                    chunk_size=chunk_size, scratch_directory=root,
                )
                target_path = root / "restored.bin"
                await new_engine.download_to_path(item.id, target_path)
                assert target_path.stat().st_size == size
                assert hashlib.sha256(target_path.read_bytes()).hexdigest() == item.sha256
                for start, end in (
                    (chunk_size - 3, chunk_size + 5),
                    (max(0, size - 64), size),
                    (size, size),
                ):
                    blocks = [block async for block in new_engine.iter_bytes(
                        item.id, start=start, end=end,
                        block_size=7,
                    )]
                    assert b"".join(blocks) == payload[start:end]
            finally:
                try:
                    if target is not None:
                        for _, peer, mid in acked:
                            assert int(await client.get_peer_id(target)) == peer
                            await client.delete_messages(target, [mid], revoke=True)
                finally:
                    await client.disconnect()
                    db.close()
    asyncio.run(scenario())


def test_live_user_session_unified_transport_and_history_marker(monkeypatch):
    """Real owner-authorized USER session: unified engine and read-only history.

    This test uses the dedicated CI channel only and removes only the message
    uploaded by its own fixture. It never touches production Telegram media.
    """
    api_id = os.getenv("TELEDRIVE_E2E_API_ID", "")
    api_hash = os.getenv("TELEDRIVE_E2E_API_HASH", "")
    session = os.getenv("TELEDRIVE_E2E_SESSION", "")
    if not all((api_id, api_hash, session)):
        pytest.skip("Dedicated user Telegram E2E session is not configured")

    from teledrive.storage.user_mode import (
        UserSessionRuntime, UserSendJournal, UnifiedUserTransport,
    )
    from teledrive.storage.history_inventory import inventory_channel
    from teledrive.storage.gc_safety import GCReferenceGuard

    async def scenario():
        with tempfile.TemporaryDirectory(prefix="teledrive-user-transport-") as folder:
            root = Path(folder)
            session_file = root / "mtproto-user.session"
            session_file.write_text(session + "\n", encoding="ascii")
            session_file.chmod(0o600)
            # Resolve the channel with the same trusted, preauthorized user.
            resolver = TelegramClient(StringSession(session), int(api_id), api_hash)
            await resolver.connect()
            try:
                target = await resolve_telegram_target(
                    resolver, os.getenv("TELEDRIVE_E2E_TARGET"),
                    os.getenv("TELEDRIVE_E2E_CHANNEL_NAME"),
                )
                channel_id = int(await resolver.get_peer_id(target))
            finally:
                await resolver.disconnect()
            monkeypatch.setenv("TELEDRIVE_STORAGE_TRANSPORT", "telethon")
            monkeypatch.setenv("TELEDRIVE_STORAGE_MTPROTO_API_ID", api_id)
            monkeypatch.setenv("TELEDRIVE_STORAGE_MTPROTO_API_HASH", api_hash)
            monkeypatch.setenv("TELEDRIVE_STORAGE_MTPROTO_CHANNEL", str(channel_id))
            monkeypatch.setenv("TELEDRIVE_STORAGE_MTPROTO_SESSION_FILE", str(session_file))
            runtime = UserSessionRuntime()
            await runtime.start()
            db = connect_database(str(root / "isolated.db"))
            published = []
            try:
                content = os.urandom(4221)
                source = root / "source.bin"
                source.write_bytes(content)
                backend = TelegramObjectStorage(
                    ManifestStore(db),
                    UnifiedUserTransport(runtime, UserSendJournal(db)),
                    chunk_size=8192,
                )
                item = await backend.put_file("ci", "unified-user-test", source)
                published = backend.manifests.parts(item.id)
                source.unlink()
                output = root / "verified.bin"
                await backend.download_to_path(item.id, output)
                assert output.read_bytes() == content
                report = await inventory_channel(
                    runtime.client,
                    runtime.transport.accounts[runtime.account][1],
                    account=runtime.account, scan_limit=100,
                )
                assert any(
                    obj["object_id"] == item.id
                    for obj in report["candidate_objects"]
                )
                safety = GCReferenceGuard(backend.manifests).evidence(item.id)
                assert not safety["deletion_permitted"]
                assert "mtproto_send_intent_or_receipt" in safety["reference_blockers"]
            finally:
                for part in published:
                    await runtime.client.delete_messages(
                        runtime.transport.accounts[runtime.account][1],
                        [part.message_id],
                    )
                db.close()
                await runtime.stop()
    asyncio.run(scenario())
