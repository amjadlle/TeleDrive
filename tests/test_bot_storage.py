"""Offline Bot API storage tests: no Telegram credentials or network required."""

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from teledrive.db import connect_database
from teledrive.storage import ManifestStore, TelegramObjectStorage
from teledrive.storage.bot_api_adapter import (
    BOT_DOWNLOAD_LIMIT, BotApiChunkTransport, BotApiClient, BotApiError,
    BotFileIndex,
)
from teledrive.storage.manifest import ChunkReference


def run(coro):
    return asyncio.run(coro)


class FakeBotApi:
    def __init__(self, chat_id=-1001234567890):
        self.chat_id = chat_id
        self.counter = 0
        self.documents = {}
        self.deleted = []
        self.permission = True
        self.chat_type = "channel"

    async def call(self, method, fields, source=None):
        if method == "getMe":
            return {"id": 42, "is_bot": True}
        if method == "getChat":
            return {"id": self.chat_id, "type": self.chat_type}
        if method == "getChatMember":
            return {"status": "administrator" if self.permission else "member",
                    "can_post_messages": self.permission}
        if method == "sendDocument":
            self.counter += 1
            fid = f"bot-file-{self.counter}"
            self.documents[fid] = source.read_bytes()
            return {"message_id": self.counter, "chat": {"id": self.chat_id},
                    "document": {"file_id": fid}}
        if method == "getFile":
            data = self.documents[fields["file_id"]]
            return {"file_path": fields["file_id"], "file_size": len(data)}
        if method == "deleteMessage":
            self.deleted.append((fields["chat_id"], fields["message_id"]))
            return {}
        raise AssertionError(method)

    async def download_file(self, path, destination):
        destination.write_bytes(self.documents[path])


@pytest.fixture
def storage(tmp_path):
    database = connect_database(str(tmp_path / "bot-manifest.db"))
    api = FakeBotApi()
    transport = BotApiChunkTransport(api, BotFileIndex(database), chat_id=api.chat_id)
    engine = TelegramObjectStorage(ManifestStore(database), transport, chunk_size=2048)
    try:
        yield database, api, transport, engine
    finally:
        database.close()


def test_bot_roundtrip_sha256_range_and_cleanup(tmp_path, storage):
    database, api, transport, engine = storage
    payload = os.urandom(4097)
    src = tmp_path / "source.bin"
    src.write_bytes(payload)
    run(transport.verify_target())
    obj = run(engine.put_file("ci", "bot/roundtrip", src))
    assert obj.size == len(payload)
    assert obj.sha256 == hashlib.sha256(payload).hexdigest()
    parts = engine.manifests.parts(obj.id)
    assert len(parts) == 3
    assert [p.chat_id for p in parts] == [api.chat_id] * 3
    src.unlink()
    destination = tmp_path / "download.bin"
    run(engine.download_to_path(obj.id, destination))
    assert destination.read_bytes() == payload

    async def read_range():
        result = bytearray()
        async for block in engine.iter_bytes(obj.id, start=2047, end=2051):
            result.extend(block)
        return bytes(result)

    assert run(read_range()) == payload[2047:2051]
    # The Bot API file index is backed by SQL, not transient Python state.
    new_transport = BotApiChunkTransport(api, BotFileIndex(database), chat_id=api.chat_id)
    dest2 = tmp_path / "new_transport.bin"
    run(new_transport.download(parts[0], dest2))
    assert dest2.read_bytes() == payload[:2048]
    for part in parts:
        run(new_transport.delete(part))
    assert len(api.deleted) == 3
    with pytest.raises(FileNotFoundError):
        run(new_transport.download(parts[0], tmp_path / "no-file"))


def test_bot_requires_admin_post_permission(storage):
    _, api, transport, _ = storage
    api.permission = False
    with pytest.raises(BotApiError, match="administrator"):
        run(transport.verify_target())
    api.permission = True
    api.chat_type = "supergroup"
    with pytest.raises(BotApiError, match="channel"):
        run(transport.verify_target())


def test_bot_rejects_cross_account_and_cross_channel_download(storage, tmp_path):
    _, api, transport, _ = storage
    valid = ChunkReference(
        ordinal=0, offset=0, size=1, sha256="0" * 64,
        account="bot", chat_id=api.chat_id, message_id=1,
    )
    with pytest.raises(FileNotFoundError):
        run(transport.download(valid, tmp_path / "missing"))
    other_account = ChunkReference(
        ordinal=0, offset=0, size=1, sha256="0" * 64,
        account="another", chat_id=api.chat_id, message_id=1,
    )
    with pytest.raises(FileNotFoundError):
        run(transport.download(other_account, tmp_path / "other"))


def test_bot_upload_limit_checked_before_remote_upload(storage, tmp_path):
    _, api, transport, _ = storage
    too_large = tmp_path / "oversize.bin"
    with too_large.open("wb") as stream:
        stream.truncate(BOT_DOWNLOAD_LIMIT + 1)
    with pytest.raises(ValueError, match="20 MiB"):
        run(transport.upload(too_large, object_id="test", ordinal=0, checksum="0" * 64))
    assert api.counter == 0


@pytest.mark.parametrize("path", [
    "../secret", "/absolute", "a/../b", "https://evil.example/file",
    "x\\y", "", "./x", "a//b",
])
def test_bot_download_paths_cannot_escape_telegram_host(path):
    with pytest.raises(BotApiError, match="unsafe"):
        BotApiClient._safe_file_path(path)


def test_bot_token_validation():
    with pytest.raises(ValueError, match="Invalid"):
        BotApiClient("not-a-token")
