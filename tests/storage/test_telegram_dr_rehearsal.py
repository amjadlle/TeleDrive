"""Read-only USER-MTProto DR restores a SQL snapshot and downloads real bytes.

Uses the production chunk engine plus a disk-backed Telethon stand-in,
and verifies that the archive and live SQL files are never modified.
There is no Bot API transport in the rehearsal path.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ops.backup_metadata import backup_sqlite
from scripts.ops.rehearse_telegram_dr import (
    ReadOnlyUserChannelTransport, verify_restored_telegram_object,
    verify_original_user_channel,
)
from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.manifest import ManifestStore
from teledrive.storage.telethon_adapter import TelethonChunkTransport


class FakeUserClient:
    """Disk-backed stand-in for a connected Telethon user client."""

    def __init__(self):
        self.documents: dict[int, bytes] = {}
        self.sent = 0
        self.reads = 0
        self.deletions = 0

    async def send_file(self, target, path, caption=None, force_document=False):
        self.sent += 1
        self.documents[self.sent] = Path(path).read_bytes()
        return SimpleNamespace(id=self.sent, chat_id=-10093744)

    async def get_peer_id(self, target):
        return -10093744

    async def get_messages(self, peer, ids):
        return SimpleNamespace(id=ids, media=True, chat_id=-10093744)

    async def download_media(self, message, file):
        self.reads += 1
        Path(file).write_bytes(self.documents[message.id])
        return str(file)

    async def connect(self):
        pass

    async def disconnect(self):
        pass


def seed(tmp_path, size, *, account="api-bot"):
    root = tmp_path / "live"
    root.mkdir()
    state = root / "manifest.db"
    source = root / "body.bin"
    payload = os.urandom(size)
    source.write_bytes(payload)
    remote = FakeUserClient()
    db = connect_database(str(state))
    try:
        catalog = BucketCatalog(ManifestStore(db))
        catalog.create_bucket("dr-test", int(time.time()))
        transport = TelethonChunkTransport({account: (remote, object())})
        stored = asyncio.run(TelegramObjectStorage(
            catalog.manifests, transport, chunk_size=64 * 1024,
            scratch_directory=root,
        ).put_file("dr-test", "restore/sample.bin", source))
        catalog.publish(stored.id, now=int(time.time()))
        assert stored.sha256 == hashlib.sha256(payload).hexdigest()
    finally:
        db.close()
    archive = tmp_path / "sealed-archive.db"
    checksum = backup_sqlite(state, archive)
    return state, archive, checksum, remote, payload


def reader_for(remote, *, account, chat_id):
    return ReadOnlyUserChannelTransport(
        remote, object(), account=account, chat_id=chat_id,
    )


@pytest.mark.parametrize("size", [0, 1, 65537, 262147])
def test_staging_dr_snapshot_and_user_retrieval_are_byte_exact(tmp_path, size):
    state, archive, receipt, remote, payload = seed(tmp_path, size)
    live_before = hashlib.sha256(state.read_bytes()).hexdigest()
    archive_before = archive.read_bytes()
    sent_before = remote.sent
    reader = reader_for(remote, account="owner-user", chat_id=-10093744)
    report = asyncio.run(verify_restored_telegram_object(
        archive, receipt, bucket="dr-test", key="restore/sample.bin",
        account="api-bot", chat_id=-10093744, transport=reader,
    ))
    assert report["verified"] and report["archive_sha256"] == receipt
    assert report["sha256"] == hashlib.sha256(payload).hexdigest()
    assert report["size"] == report["readback_bytes"] == size
    assert report["telegram_uploads"] == report["telegram_deletions"] == 0
    assert report["source_database_modified"] is False
    assert archive.read_bytes() == archive_before
    assert hashlib.sha256(state.read_bytes()).hexdigest() == live_before
    assert remote.sent == sent_before and remote.deletions == 0
    assert remote.reads >= (0 if size == 0 else 1)


@pytest.mark.parametrize("error", ["tampered-archive", "tampered-remote",
                                     "oversized-budget", "wrong-bucket"])
def test_staging_dr_denies_corrupt_and_unauthorized_inputs(tmp_path, error):
    state, archive, receipt, remote, payload = seed(tmp_path, 65537)
    sent_before = remote.sent
    reader = reader_for(remote, account="owner-user", chat_id=-10093744)
    kwargs = dict(bucket="dr-test", key="restore/sample.bin",
                  account="api-bot", chat_id=-10093744, transport=reader)
    if error == "tampered-archive":
        with archive.open("ab") as file:
            file.write(b"altered")
    elif error == "tampered-remote":
        remote.documents[1] = b"modified" * 1024
    elif error == "oversized-budget":
        kwargs["max_bytes"] = 65536
    else:
        kwargs["bucket"] = "another-bucket"
    with pytest.raises((RuntimeError, ValueError, LookupError, FileNotFoundError)):
        asyncio.run(verify_restored_telegram_object(
            archive, receipt, **kwargs,
        ))
    assert remote.sent == sent_before and remote.deletions == 0
    assert state.is_file()


def test_reader_scope_denies_foreign_account_and_wrong_channel(tmp_path):
    state, archive, receipt, remote, _ = seed(tmp_path, 65537, account="owner-user")
    for account, channel in [("other-account", -10093744),
                              ("owner-user", -10093745)]:
        reader = reader_for(remote, account=account, chat_id=channel)
        with pytest.raises(FileNotFoundError):
            asyncio.run(verify_restored_telegram_object(
                archive, receipt, bucket="dr-test", key="restore/sample.bin",
                account="owner-user", chat_id=-10093744, transport=reader,
            ))
    assert remote.deletions == 0


def test_reader_exposes_no_mutation_methods(tmp_path):
    state, archive, receipt, remote, _ = seed(tmp_path, 1)
    reader = reader_for(remote, account="owner-user", chat_id=-10093744)
    assert not hasattr(reader, "delete") and not hasattr(reader, "upload")
    report = asyncio.run(verify_restored_telegram_object(
        archive, receipt, bucket="dr-test", key="restore/sample.bin",
        account="api-bot", chat_id=-10093744, transport=reader,
    ))
    assert report["verified"] and remote.deletions == 0


def test_operator_mtproto_dr_refuses_bot_user_and_wrong_channel(monkeypatch, tmp_path):
    import telethon
    import telethon.sessions
    import telethon.tl.types

    state, archive, receipt, remote, payload = seed(tmp_path, 4097)
    session_file = tmp_path / "authorized-user.session"
    session_file.write_text("fake-authorized-session\n", encoding="ascii")

    class Channel:
        broadcast = True

    class FakeConnectedClient:
        identity_bot = False
        peer = -10093744

        def __init__(self, session, api_id, api_hash, **kwargs):
            self.options = kwargs

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def get_me(self):
            return SimpleNamespace(bot=type(self).identity_bot)

        async def iter_dialogs(self, limit):
            assert limit == 5000
            yield SimpleNamespace(id=-10093744, entity=Channel())

        async def get_peer_id(self, channel):
            return type(self).peer

        async def get_messages(self, channel, ids):
            return SimpleNamespace(id=ids, media=True)

        async def download_media(self, message, file):
            Path(file).write_bytes(remote.documents[message.id])
            return str(file)

    monkeypatch.setattr(telethon, "TelegramClient", FakeConnectedClient)
    monkeypatch.setattr(telethon.sessions, "StringSession", lambda s: s)
    monkeypatch.setattr(telethon.tl.types, "Channel", Channel)
    params = dict(bucket="dr-test", key="restore/sample.bin",
                  account="api-bot", chat_id=-10093744,
                  target="-10093744", api_id=12345,
                  api_hash="a" * 32, session_file=session_file)
    result = asyncio.run(verify_original_user_channel(archive, receipt, **params))
    assert result["verified"] and result["sha256"] == hashlib.sha256(payload).hexdigest()
    FakeConnectedClient.identity_bot = True
    with pytest.raises(ValueError, match="USER"):
        asyncio.run(verify_original_user_channel(archive, receipt, **params))
    FakeConnectedClient.identity_bot = False
    FakeConnectedClient.peer = -100987654321
    with pytest.raises(ValueError, match="identity mismatch"):
        asyncio.run(verify_original_user_channel(archive, receipt, **params))
    assert remote.deletions == 0
