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
    transport = BotApiChunkTransport(api, BotFileIndex(database), chat_id=api.chat_id, min_upload_interval=0)
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
    new_transport = BotApiChunkTransport(api, BotFileIndex(database), chat_id=api.chat_id, min_upload_interval=0)
    dest2 = tmp_path / "new_transport.bin"
    run(new_transport.download(parts[0], dest2))
    assert dest2.read_bytes() == payload[:2048]
    for part in parts:
        run(new_transport.delete(part))
    assert len(api.deleted) == 3
    with pytest.raises(FileNotFoundError):
        run(new_transport.download(parts[0], tmp_path / "no-file"))
    # Physical deletion of test-owned messages also removes ACK receipts:
    # future recoveries must never adopt an ID that was already deleted.
    assert new_transport.index.find_receipt(
        obj.id, 0, account="bot", chat_id=api.chat_id,
        checksum=parts[0].sha256, size=parts[0].size,
    ) is None


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


@pytest.mark.parametrize("destination", [
    "http://api.telegram.org/file",
    "https://evil.example/steal",
    "https://api.telegram.org.evil.example/file",
    "file:///etc/passwd",
    "https://api.telegram.org:444/file",
    "https://user:password@api.telegram.org/file",
])
def test_token_bearing_requests_reject_redirects_outside_telegram_https(destination):
    import urllib.request
    from teledrive.storage.bot_api_adapter import _TelegramHTTPSRedirects

    request = urllib.request.Request("https://api.telegram.org/bot123/test")
    with pytest.raises(BotApiError, match="disallowed"):
        _TelegramHTTPSRedirects().redirect_request(
            request, None, 302, "Found", {}, destination
        )


def test_token_bearing_request_allows_only_telegram_https_redirect():
    import urllib.request
    from teledrive.storage.bot_api_adapter import _TelegramHTTPSRedirects

    request = urllib.request.Request("https://api.telegram.org/bot123/test")
    result = _TelegramHTTPSRedirects().redirect_request(
        request, None, 302, "Found", {}, "https://api.telegram.org/file/bot123/data"
    )
    assert result.full_url.startswith("https://api.telegram.org/")


def test_bot_api_multipart_contains_valid_headers_and_binary_payload(tmp_path):
    blob = tmp_path / "payload.bin"
    blob.write_bytes(b"\x00\xff\x13actual-binary")
    body, content_type = BotApiClient._multipart(
        {"chat_id": "-1001234567890", "caption": "teledrive:test:0:abcdef"}, blob
    )
    boundary = content_type.partition("boundary=")[2].encode("ascii")
    assert boundary
    assert b'Content-Disposition: form-data; name="document"; filename="chunk.bin"\r\n' in body
    assert b'Content-Type: application/octet-stream\r\n\r\n' in body
    assert b"\r\n\r\n-1001234567890\r\n" in body
    assert b"\x00\xff\x13actual-binary" in body
    assert body.endswith(b"--" + boundary + b"--\r\n")


def test_bot_corruption_never_publishes_partial_download(tmp_path, storage):
    from teledrive.storage.engine import IntegrityError

    _, api, _, engine = storage
    payload = b"original-chunk-payload"
    source = tmp_path / "source.bin"
    source.write_bytes(payload)
    obj = run(engine.put_file("ci", "bot/corrupt", source))
    api.documents["bot-file-1"] = b"damaged!"
    output = tmp_path / "must-not-exist"
    with pytest.raises(IntegrityError, match="checksum mismatch"):
        run(engine.download_to_path(obj.id, output))
    assert not output.exists()


def test_bot_rejects_upload_response_for_other_chat(storage, tmp_path):
    _, api, transport, _ = storage
    api.chat_id = -1002222222222
    source = tmp_path / "source.bin"
    source.write_bytes(b"abc")
    with pytest.raises(BotApiError, match="unexpected upload target"):
        run(transport.upload(source, object_id="test", ordinal=0, checksum="f" * 64))


def test_missing_telegram_document_and_missing_file_path_fail_closed(storage, tmp_path):
    _, api, transport, engine = storage
    source = tmp_path / "source.bin"
    source.write_bytes(b"abc")
    obj = run(engine.put_file("ci", "bot/missing", source))
    part = engine.manifests.parts(obj.id)[0]

    original_call = api.call

    async def invalid_file(method, fields, source=None):
        if method == "getFile":
            return {}
        return await original_call(method, fields, source)
    api.call = invalid_file

    with pytest.raises(BotApiError, match="no download path"):
        run(transport.download(part, tmp_path / "no-file"))


def test_bot_uploads_are_paced_per_channel(tmp_path):
    import time

    class TimedBot(FakeBotApi):
        def __init__(self):
            super().__init__()
            self.sent_at = []

        async def call(self, method, fields, source=None):
            if method == "sendDocument":
                self.sent_at.append(time.monotonic())
            return await super().call(method, fields, source)

    database = connect_database(str(tmp_path / "rate-index.db"))
    api = TimedBot()
    transport = BotApiChunkTransport(
        api, BotFileIndex(database), chat_id=api.chat_id,
        min_upload_interval=0.03,
    )
    chunk = tmp_path / "chunk.bin"
    chunk.write_bytes(b"tiny")
    try:
        run(transport.upload(chunk, object_id="rate-test", ordinal=0, checksum="a" * 64))
        run(transport.upload(chunk, object_id="rate-test", ordinal=1, checksum="b" * 64))
        assert len(api.sent_at) == 2
        assert api.sent_at[1] - api.sent_at[0] >= 0.025
    finally:
        database.close()


def test_bot_upload_interval_validation(storage):
    database, api, _, _ = storage
    with pytest.raises(ValueError, match="interval"):
        BotApiChunkTransport(
            api, BotFileIndex(database), chat_id=api.chat_id, min_upload_interval=-1,
        )


def test_bot_download_does_not_delete_preexisting_destination(tmp_path, monkeypatch):
    from teledrive.storage import bot_api_adapter

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            return b"new data"

    class Opener:
        def open(self, request, timeout):
            return Response()

    monkeypatch.setattr(bot_api_adapter, "_telegram_https_opener", lambda: Opener())
    destination = tmp_path / "existing.bin"
    destination.write_bytes(b"important original")
    api = BotApiClient("123:fakeToken")
    with pytest.raises(BotApiError, match="download failed"):
        api._download_sync("documents/file.bin", destination)
    assert destination.read_bytes() == b"important original"


def _uncommitted_bot_ack(tmp_path, *, payload=None, copies=1):
    """A crash after BotFileIndex ACK, before ManifestStore.append()."""
    import time
    from sqlalchemy import update
    from teledrive.storage.manifest import writer_fences
    database_path = str(tmp_path / "ack.sqlite")
    db = connect_database(database_path)
    api = FakeBotApi()
    transport = BotApiChunkTransport(
        api, BotFileIndex(db), chat_id=api.chat_id,
        min_upload_interval=0,
    )
    manifests = ManifestStore(db)
    content = payload if payload is not None else b"receipt-resume-" * 185
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    object_id = manifests.begin("bot-ci", "recovered.bin", len(content), int(time.time()))
    manifests.start_writer(object_id, "original-writer", int(time.time()), ttl=120)
    chunk = tmp_path / "first-chunk.bin"
    chunk.write_bytes(content[:2048])
    for _ in range(copies):
        run(transport.upload(chunk, object_id=object_id, ordinal=0,
                             checksum=hashlib.sha256(content[:2048]).hexdigest()))
    assert manifests.recovery_parts(object_id) == []
    db.connection.execute(update(writer_fences).where(
        writer_fences.c.object_id == object_id
    ).values(lease_until=0))
    db.commit()
    db.close()
    return database_path, api, source, object_id


def test_bot_ack_receipt_survives_sql_restart_and_reuses_telegram_bytes(tmp_path):
    import time
    path, api, source, object_id = _uncommitted_bot_ack(tmp_path)
    uploads_before = api.counter
    reopened = connect_database(path)
    try:
        transport = BotApiChunkTransport(
            api, BotFileIndex(reopened), chat_id=api.chat_id,
            min_upload_interval=0,
        )
        manifest = ManifestStore(reopened)
        restored = run(TelegramObjectStorage(
            manifest, transport, chunk_size=2048,
        ).resume_file("bot-ci", "recovered.bin", source, object_id))
        # First Telegram chunk had ACK but no chunk journal. Only the tail
        # is sent after verifying the existing Telegram payload against source.
        assert api.counter == uploads_before + 1
        assert restored.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
        assert [p.ordinal for p in manifest.parts(object_id)] == [0, 1]
        assert manifest.parts(object_id)[0].message_id == 1
        assert restored.id == object_id
    finally:
        reopened.close()


def test_bot_ack_recovery_rejects_corrupt_remote_data_before_reupload(tmp_path):
    from teledrive.storage.engine import IntegrityError
    path, api, source, object_id = _uncommitted_bot_ack(tmp_path)
    api.documents["bot-file-1"] = b"corrupt"
    reopened = connect_database(path)
    try:
        engine = TelegramObjectStorage(
            ManifestStore(reopened),
            BotApiChunkTransport(api, BotFileIndex(reopened),
                                 chat_id=api.chat_id, min_upload_interval=0),
            chunk_size=2048,
        )
        with pytest.raises(IntegrityError, match="Recovered ACK bytes"):
            run(engine.resume_file("bot-ci", "recovered.bin", source, object_id))
        assert api.counter == 1
        assert engine.manifests.recovery_parts(object_id) == []
    finally:
        reopened.close()


def test_bot_ack_recovery_refuses_ambiguous_duplicate_receipts(tmp_path):
    path, api, source, object_id = _uncommitted_bot_ack(tmp_path, copies=2)
    reopened = connect_database(path)
    try:
        transport = BotApiChunkTransport(
            api, BotFileIndex(reopened), chat_id=api.chat_id,
            min_upload_interval=0,
        )
        with pytest.raises(BotApiError, match="Ambiguous"):
            run(TelegramObjectStorage(
                ManifestStore(reopened), transport, chunk_size=2048,
            ).resume_file("bot-ci", "recovered.bin", source, object_id))
        assert api.counter == 2
        assert ManifestStore(reopened).recovery_parts(object_id) == []
    finally:
        reopened.close()


def test_bot_ack_receipt_rejects_foreign_checksum_and_bad_source(tmp_path):
    path, api, source, object_id = _uncommitted_bot_ack(tmp_path)
    reopened = connect_database(path)
    try:
        index = BotFileIndex(reopened)
        with pytest.raises(BotApiError, match="does not match"):
            index.find_receipt(
                object_id, 0, account="another", chat_id=api.chat_id,
                checksum=hashlib.sha256(source.read_bytes()[:2048]).hexdigest(),
                size=2048,
            )
        with pytest.raises(BotApiError, match="does not match"):
            index.find_receipt(
                object_id, 0, account="bot", chat_id=api.chat_id,
                checksum="0" * 64, size=2048,
            )
    finally:
        reopened.close()


def test_bot_ack_receipt_and_file_id_commit_atomically(tmp_path):
    """Injected receipt SQL failure must not persist a misleading file ID."""
    from sqlalchemy.exc import IntegrityError

    database = connect_database(str(tmp_path / "receipt-atomicity.db"))
    try:
        index = BotFileIndex(database)
        database.connection.exec_driver_sql(
            "CREATE TRIGGER abort_bot_receipt BEFORE INSERT "
            "ON storage_bot_upload_receipts "
            "BEGIN SELECT RAISE(ABORT, 'inject SQL receipt failure'); END"
        )
        database.commit()
        with pytest.raises(IntegrityError):
            index.put_receipt(
                "bot", -1001234567890, 999, "file-id-must-not-survive",
                object_id="test-object", ordinal=0, checksum="a" * 64, size=5,
            )
        with pytest.raises(FileNotFoundError):
            index.get("bot", -1001234567890, 999)
        assert index.find_receipt(
            "test-object", 0, account="bot", chat_id=-1001234567890,
            checksum="a" * 64, size=5,
        ) is None
    finally:
        database.close()


@pytest.mark.parametrize("size,expected_chunks", [
    pytest.param(0, 0, id="zero-bytes-no-Telegram-send"),
    pytest.param(1, 1, id="one-byte"),
    pytest.param(16 * 1024 * 1024 - 1, 1, id="16MiB-minus-one"),
    pytest.param(16 * 1024 * 1024, 1, id="exact-16MiB"),
    pytest.param(16 * 1024 * 1024 + 1, 2, id="16MiB-plus-one"),
])
def test_bot_offline_16mib_boundary_chunking_and_full_integrity(
    tmp_path, size, expected_chunks,
):
    """Synthetic binary data exercises large Bot API chunks without network.

    Real dedicated-channel stress uses the separate credentialed E2E suite.
    """
    db = connect_database(str(tmp_path / "large-boundary.db"))
    fake = FakeBotApi()
    transport = BotApiChunkTransport(
        fake, BotFileIndex(db), chat_id=fake.chat_id,
        min_upload_interval=0,
    )
    engine = TelegramObjectStorage(
        ManifestStore(db), transport, chunk_size=16 * 1024 * 1024,
        scratch_directory=tmp_path,
    )
    source = tmp_path / "source.bin"
    source.write_bytes(os.urandom(size))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    try:
        obj = run(engine.put_file("large-ci", f"limits/{size}.bin", source))
        assert obj.size == size and obj.sha256 == digest
        refs = engine.manifests.parts(obj.id)
        assert len(refs) == expected_chunks
        assert fake.counter == expected_chunks
        assert sum(p.size for p in refs) == size
        source.unlink()
        restored = tmp_path / "download.bin"
        run(engine.download_to_path(obj.id, restored))
        assert restored.stat().st_size == size
        assert hashlib.sha256(restored.read_bytes()).hexdigest() == digest
        for reference in refs:
            run(transport.delete(reference))
        assert len(fake.deleted) == expected_chunks
    finally:
        db.close()
