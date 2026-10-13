"""Offline Bot API storage tests: no Telegram credentials or network required."""

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from teledrive.db import connect_database
from teledrive.storage import ManifestStore, TelegramObjectStorage
from teledrive.storage.bot_api_adapter import (
    BOT_DOWNLOAD_LIMIT, BotApiChunkTransport, BotApiClient, BotApiError, BotApiRateLimited,
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


def test_bot_exact_download_cap_allowed_and_cap_plus_one_rejected_without_send(storage, tmp_path):
    """Verify both sides of the 20 MiB Bot API getFile/download boundary."""
    _, api, transport, _ = storage
    source = tmp_path / "exact-20mib.bin"
    data = os.urandom(BOT_DOWNLOAD_LIMIT)
    source.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    account, chat, mid = run(transport.upload(
        source, object_id="cap-test", ordinal=0, checksum=digest,
    ))
    assert api.counter == 1 and chat == api.chat_id
    reference = ChunkReference(
        ordinal=0, offset=0, size=len(data), sha256=digest,
        account=account, chat_id=chat, message_id=mid,
    )
    fetched = tmp_path / "exact-20mib-remote.bin"
    run(transport.download(reference, fetched))
    assert hashlib.sha256(fetched.read_bytes()).hexdigest() == digest
    with source.open("ab") as stream:
        stream.write(b"!")
    with pytest.raises(ValueError, match="20 MiB"):
        run(transport.upload(
            source, object_id="cap-test", ordinal=1,
            checksum=hashlib.sha256(data + b"!").hexdigest(),
        ))
    assert api.counter == 1
    run(transport.delete(reference))


def test_explicit_telegram_429_retries_once_without_duplicate_remote_media(storage, tmp_path):
    database, remote, _, _ = storage
    class RejectedOnce(FakeBotApi):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def call(self, method, fields, source=None):
            if method == "sendDocument":
                self.attempts += 1
                if self.attempts == 1:
                    raise BotApiRateLimited(1)
            return await super().call(method, fields, source)

    api = RejectedOnce()
    transport = BotApiChunkTransport(
        api, BotFileIndex(database), chat_id=api.chat_id,
        min_upload_interval=0,
    )
    source = tmp_path / "rate-limited.bin"
    payload = os.urandom(257)
    source.write_bytes(payload)
    obj = run(TelegramObjectStorage(
        ManifestStore(database), transport, chunk_size=1024,
        scratch_directory=tmp_path,
    ).put_file("ci", "safe-retry", source))
    assert api.attempts == 2 and api.counter == 1
    ref = ManifestStore(database).parts(obj.id)[0]
    assert transport.index.find_receipt(
        obj.id, 0, account="bot", chat_id=api.chat_id,
        checksum=hashlib.sha256(payload).hexdigest(), size=len(payload),
    ).message_id == ref.message_id
    restored = tmp_path / "rate-limited-restored.bin"
    run(transport.download(ref, restored))
    assert restored.read_bytes() == payload


@pytest.mark.parametrize("cooldowns,expected_attempts", [
    ([1, 1], 2), ([61], 1),
])
def test_telegram_429_is_bounded_and_never_creates_ambiguous_receipts(
    storage, tmp_path, cooldowns, expected_attempts,
):
    db, _, _, _ = storage

    class AlwaysRejected(FakeBotApi):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def call(self, method, fields, source=None):
            if method == "sendDocument":
                pause = cooldowns[min(self.calls, len(cooldowns)-1)]
                self.calls += 1
                raise BotApiRateLimited(pause)
            return await super().call(method, fields, source)

    remote = AlwaysRejected()
    transport = BotApiChunkTransport(
        remote, BotFileIndex(db), chat_id=remote.chat_id,
        min_upload_interval=0,
    )
    payload = tmp_path / "blocked.bin"
    payload.write_bytes(b"definitely-not-accepted")
    with pytest.raises(BotApiRateLimited):
        run(transport.upload(
            payload, object_id="fenced-ci", ordinal=0,
            checksum=hashlib.sha256(payload.read_bytes()).hexdigest(),
        ))
    assert remote.calls == expected_attempts
    assert remote.counter == 0
    assert transport.index.unjournaled_count() == 0


def test_bot_api_http_429_extracts_bounded_retry_after_without_leaking_token(monkeypatch):
    import io
    import json
    import urllib.error
    import teledrive.storage.bot_api_adapter as module
    token = "123456:TEST_SECRET_ONLY"
    bot = BotApiClient(token)

    class Denied:
        def open(self, request, timeout):
            body = json.dumps({
                "ok": False, "error_code": 429,
                "parameters": {"retry_after": 3},
            }).encode()
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests",
                None, io.BytesIO(body),
            )

    monkeypatch.setattr(module, "_telegram_https_opener", lambda: Denied())
    with pytest.raises(BotApiRateLimited) as caught:
        bot._call_sync("sendDocument", {"chat_id": -1001234})
    assert caught.value.retry_after == 3
    assert token not in str(caught.value)


def test_bot_api_network_uncertainty_does_not_retry_send(storage, tmp_path):
    db, _, _, _ = storage

    class Indeterminate(FakeBotApi):
        attempts = 0

        async def call(self, method, fields, source=None):
            if method == "sendDocument":
                self.attempts += 1
                raise BotApiError("Bot API sendDocument request failed")
            return await super().call(method, fields, source)

    api = Indeterminate()
    transport = BotApiChunkTransport(
        api, BotFileIndex(db), chat_id=api.chat_id,
        min_upload_interval=0,
    )
    source = tmp_path / "uncertain.bin"
    source.write_bytes(b"may-have-been-accepted")
    with pytest.raises(BotApiError, match="request failed"):
        run(transport.upload(
            source, object_id="uncertain", ordinal=0,
            checksum=hashlib.sha256(source.read_bytes()).hexdigest(),
        ))
    assert api.attempts == 1
    assert transport.index.unjournaled_count() == 0

def test_send_intent_committed_before_network_and_cleared_by_atomic_receipt(tmp_path):
    """Even the first HTTP send observes a committed SQL intent."""
    db = connect_database(str(tmp_path / "intent-before-send.db"))
    try:
        index = BotFileIndex(db)

        class InspectSend(FakeBotApi):
            async def call(self, method, fields, source=None):
                if method == "sendDocument":
                    # A new database connection sees the record before any
                    # remote bytes can be accepted.
                    other = connect_database(str(tmp_path / "intent-before-send.db"))
                    try:
                        assert BotFileIndex(other).unresolved_count() == 1
                    finally:
                        other.close()
                return await super().call(method, fields, source)

        api = InspectSend()
        transport = BotApiChunkTransport(
            api, index, chat_id=api.chat_id, min_upload_interval=0,
        )
        source = tmp_path / "body.bin"
        source.write_bytes(bytes(range(256)) * 5)
        checksum = hashlib.sha256(source.read_bytes()).hexdigest()
        run(transport.upload(source, object_id="send-intent", ordinal=0, checksum=checksum))
        assert api.counter == 1
        assert index.unresolved_count() == 0
        assert index.find_receipt(
            "send-intent", 0, account="bot", chat_id=api.chat_id,
            checksum=checksum, size=source.stat().st_size,
        ) is not None
    finally:
        db.close()


def test_lost_ack_prevents_duplicate_send_across_sql_restart_and_can_reconcile(tmp_path):
    """A real accepted remote send without ACK is fenced until owner proof.

    The test retains the actual ACK in its fake remote. An operator cannot
    recover those fields from Bot API channel history automatically.
    """
    import time
    from sqlalchemy import update
    from teledrive.storage.manifest import writer_fences

    db_path = str(tmp_path / "ambiguous.db")
    db = connect_database(db_path)

    class LostAck(FakeBotApi):
        received_ack = None

        async def call(self, method, fields, source=None):
            result = await super().call(method, fields, source)
            if method == "sendDocument" and self.received_ack is None:
                self.received_ack = result
                raise BotApiError("Bot API sendDocument request failed")
            return result

    api = LostAck()
    transport = BotApiChunkTransport(
        api, BotFileIndex(db), chat_id=api.chat_id, min_upload_interval=0,
    )
    content = os.urandom(2048 + 19)
    source = tmp_path / "ambiguous-source.bin"
    source.write_bytes(content)
    manifest = ManifestStore(db)
    object_id = manifest.begin("ci", "lost-ack.bin", len(content), int(time.time()))
    manifest.start_writer(object_id, "dead-worker", int(time.time()), ttl=60)
    part = tmp_path / "first.bin"
    part.write_bytes(content[:2048])
    digest = hashlib.sha256(part.read_bytes()).hexdigest()
    with pytest.raises(BotApiError, match="request failed"):
        run(transport.upload(part, object_id=object_id, ordinal=0, checksum=digest))
    assert api.counter == 1 and transport.index.unresolved_count() == 1
    assert manifest.recovery_parts(object_id) == []
    db.connection.execute(update(writer_fences).where(
        writer_fences.c.object_id == object_id,
    ).values(lease_until=0))
    db.commit()
    db.close()

    recovered_db = connect_database(db_path)
    try:
        recovered_index = BotFileIndex(recovered_db)
        recovered_transport = BotApiChunkTransport(
            api, recovered_index, chat_id=api.chat_id, min_upload_interval=0,
        )
        with pytest.raises(BotApiError, match="Unresolved Bot API send intent"):
            recovered_index.find_receipt(
                object_id, 0, account="bot", chat_id=api.chat_id,
                checksum=digest, size=2048,
            )
        with pytest.raises(BotApiError, match="Unresolved Bot API send intent"):
            run(recovered_transport.upload(
                part, object_id=object_id, ordinal=0, checksum=digest,
            ))
        assert api.counter == 1  # no duplicate upload after SQL restart

        # Simulate an operator who independently obtained the original ACK,
        # verified channel identity and content, and explicitly persisted it.
        original_ack = api.received_ack
        assert api.documents[original_ack["document"]["file_id"]] == content[:2048]
        recovered_index.put_receipt(
            "bot", api.chat_id, original_ack["message_id"],
            original_ack["document"]["file_id"],
            object_id=object_id, ordinal=0, checksum=digest, size=2048,
        )
        assert recovered_index.unresolved_count() == 0
        completed = run(TelegramObjectStorage(
            ManifestStore(recovered_db), recovered_transport, chunk_size=2048,
            scratch_directory=tmp_path,
        ).resume_file("ci", "lost-ack.bin", source, object_id))
        assert completed.sha256 == hashlib.sha256(content).hexdigest()
        assert api.counter == 2  # only 19-byte tail is newly sent
        refs = ManifestStore(recovered_db).parts(object_id)
        assert [ref.message_id for ref in refs] == [1, 2]
        assert refs[0].sha256 == digest
    finally:
        recovered_db.close()


def test_ack_sql_receipt_failure_leaves_intent_for_manual_recovery(tmp_path):
    """Never clear the only proof of an uncertain send on SQL error."""
    db = connect_database(str(tmp_path / "ack-intent-failure.db"))
    try:
        index = BotFileIndex(db)
        api = FakeBotApi()
        transport = BotApiChunkTransport(
            api, index, chat_id=api.chat_id, min_upload_interval=0,
        )
        db.connection.exec_driver_sql(
            "CREATE TRIGGER abort_receipt BEFORE INSERT "
            "ON storage_bot_upload_receipts "
            "BEGIN SELECT RAISE(ABORT, 'forced lost receipt'); END"
        )
        db.commit()
        source = tmp_path / "first.bin"
        source.write_bytes(os.urandom(4097))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        with pytest.raises(Exception, match="forced lost receipt"):
            run(transport.upload(
                source, object_id="receipt-crash", ordinal=0, checksum=digest,
            ))
        assert api.counter == 1
        assert index.unresolved_count() == 1
        with pytest.raises(BotApiError, match="Unresolved Bot API send intent"):
            run(transport.upload(
                source, object_id="receipt-crash", ordinal=0, checksum=digest,
            ))
        assert api.counter == 1
    finally:
        db.close()


def test_definitive_429_rejection_clears_intent_for_safe_retry(tmp_path):
    db = connect_database(str(tmp_path / "definite-reject.db"))
    try:
        class RejectOnce(FakeBotApi):
            failures = 0

            async def call(self, method, fields, source=None):
                if method == "sendDocument" and not self.failures:
                    self.failures += 1
                    raise BotApiRateLimited(61)
                return await super().call(method, fields, source)

        api = RejectOnce()
        transport = BotApiChunkTransport(
            api, BotFileIndex(db), chat_id=api.chat_id, min_upload_interval=0,
        )
        source = tmp_path / "429.bin"
        source.write_bytes(b"known-rejected")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        with pytest.raises(BotApiRateLimited):
            run(transport.upload(
                source, object_id="explicit-rejection", ordinal=0,
                checksum=digest,
            ))
        assert api.counter == 0 and transport.index.unresolved_count() == 0
        run(transport.upload(
            source, object_id="explicit-rejection", ordinal=0, checksum=digest,
        ))
        assert api.counter == 1 and transport.index.unresolved_count() == 0
    finally:
        db.close()


def test_independent_bot_accounts_have_distinct_send_intent_fences(tmp_path):
    db = connect_database(str(tmp_path / "multi-account-intents.db"))
    try:
        idx = BotFileIndex(db)
        idx.start_send("bot-a", -10042, "item", 0, "a" * 64, 20)
        idx.start_send("bot-b", -10042, "item", 0, "b" * 64, 20)
        idx.start_send("bot-a", -10043, "item", 0, "c" * 64, 20)
        assert idx.unresolved_count() == 3
        with pytest.raises(BotApiError, match="Unresolved"):
            idx.start_send("bot-a", -10042, "item", 0, "a" * 64, 20)
        assert idx.unresolved_count() == 3
        idx.clear_rejected_send("bot-a", -10042, "item", 0, "a" * 64, 20)
        assert idx.unresolved_count() == 2
    finally:
        db.close()

def test_concurrent_duplicate_upload_attempts_do_not_send_twice(tmp_path):
    db = connect_database(str(tmp_path / "parallel-intents.db"))

    class SlowRemote(FakeBotApi):
        async def call(self, method, fields, source=None):
            if method == "sendDocument":
                await asyncio.sleep(0.05)
            return await super().call(method, fields, source)

    remote = SlowRemote()
    transport = BotApiChunkTransport(
        remote, BotFileIndex(db), chat_id=remote.chat_id,
        min_upload_interval=0,
    )
    source = tmp_path / "concurrent.bin"
    source.write_bytes(os.urandom(8 * 1024 + 1))
    sha = hashlib.sha256(source.read_bytes()).hexdigest()

    async def both():
        return await asyncio.gather(
            transport.upload(source, object_id="dupe-race", ordinal=0, checksum=sha),
            transport.upload(source, object_id="dupe-race", ordinal=0, checksum=sha),
            return_exceptions=True,
        )

    try:
        results = run(both())
        assert sum(isinstance(item, tuple) for item in results) == 1
        assert sum(isinstance(item, BotApiError) for item in results) == 1
        assert remote.counter == 1
        assert transport.index.unresolved_count() == 0
    finally:
        db.close()


def test_unresolved_send_fence_applies_across_independent_sql_sessions(tmp_path):
    path = str(tmp_path / "independent-workers.db")
    original = connect_database(path)
    another = connect_database(path)
    try:
        initial = BotFileIndex(original)
        same_remote = BotFileIndex(another)
        initial.start_send("bot", -1001234, "cross-worker", 0, "a" * 64, 13)
        assert same_remote.unresolved_count() == 1
        with pytest.raises(BotApiError, match="Unresolved"):
            same_remote.start_send(
                "bot", -1001234, "cross-worker", 0, "a" * 64, 13,
            )
        assert initial.unresolved_count() == 1
    finally:
        another.close()
        original.close()


def test_unresolved_intent_cannot_be_cleared_by_wrong_source_metadata(tmp_path):
    db = connect_database(str(tmp_path / "strict-clear.db"))
    try:
        idx = BotFileIndex(db)
        idx.start_send("bot", -1009, "sensitive", 4, "b" * 64, 19)
        idx.clear_rejected_send("bot", -1009, "sensitive", 4, "c" * 64, 19)
        idx.clear_rejected_send("bot", -1009, "sensitive", 4, "b" * 64, 20)
        idx.clear_rejected_send("other", -1009, "sensitive", 4, "b" * 64, 19)
        assert idx.unresolved_count() == 1
        idx.clear_rejected_send("bot", -1009, "sensitive", 4, "b" * 64, 19)
        assert idx.unresolved_count() == 0
    finally:
        db.close()
