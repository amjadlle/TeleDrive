"""Storage-engine behavior tests: real SQL transactions, fake Telegram I/O.

These test the actual byte transfer algorithm and manifest persistence, not
merely HTTP response status or mocked function calls.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import pytest
from sqlalchemy import select

from teledrive.db import connect_database
from teledrive.storage.engine import IntegrityError, TelegramObjectStorage
from teledrive.storage.manifest import (
    ChunkReference, ManifestError, ManifestStore, objects, chunks,
)


class MemoryTelegram:
    """Deterministic stand-in for Telegram message storage."""

    def __init__(self):
        self.data: dict[tuple[str, int, int], bytes] = {}
        self.uploads = 0
        self.downloads = 0
        self.fail_on_upload: int | None = None
        self.on_upload = None
        self.account = "primary"

    async def upload(self, path: Path, *, object_id: str, ordinal: int,
                     checksum: str) -> tuple[str, int, int]:
        self.uploads += 1
        if self.fail_on_upload == self.uploads:
            raise TimeoutError("simulated Telegram disconnect")
        message = self.uploads
        body = path.read_bytes()
        assert hashlib.sha256(body).hexdigest() == checksum
        self.data[(self.account, 12345, message)] = body
        if self.on_upload:
            self.on_upload()
        return self.account, 12345, message

    async def download(self, reference: ChunkReference, destination: Path) -> None:
        self.downloads += 1
        key = (reference.account, reference.chat_id, reference.message_id)
        if key not in self.data:
            raise FileNotFoundError("Telegram media missing")
        destination.write_bytes(self.data[key])


@pytest.fixture
def store(tmp_path):
    conn = connect_database(str(tmp_path / "state.db"))
    manifests = ManifestStore(conn)
    try:
        yield manifests
    finally:
        conn.close()


def execute(coroutine):
    return asyncio.run(coroutine)


@pytest.mark.parametrize("payload,chunk_size", [
    (b"", 5), (b"a", 5), (b"abcd", 5),
    (b"abcde", 5), (b"abcdef", 5), (bytes(range(53)), 5),
    (os.urandom(100_003), 4096),
])
def test_full_telegram_round_trip_and_persisted_manifest(store, tmp_path, payload, chunk_size):
    backend = MemoryTelegram()
    engine = TelegramObjectStorage(store, backend, chunk_size=chunk_size)
    source = tmp_path / "original"
    source.write_bytes(payload)
    item = execute(engine.put_file("backup", "folder/backup.bin", source))
    assert item.size == len(payload)
    assert item.sha256 == hashlib.sha256(payload).hexdigest()
    assert len(store.parts(item.id)) == (len(payload) + chunk_size - 1) // chunk_size
    output = tmp_path / "restored"
    # No source file or persistent local content remains for the read path.
    source.unlink()
    execute(engine.download_to_path(item.id, output))
    assert output.read_bytes() == payload
    assert store.get(item.id).key == "folder/backup.bin"
    assert len(list(tmp_path.glob("teledrive-*"))) == 0


def test_cross_chunk_range_reads_only_relevant_parts(store, tmp_path):
    backend = MemoryTelegram()
    engine = TelegramObjectStorage(store, backend, chunk_size=4)
    source = tmp_path / "file"
    source.write_bytes(b"0123456789ABCDEF")
    item = execute(engine.put_file("b", "k", source))
    async def read_range():
        result = bytearray()
        async for part in engine.iter_bytes(item.id, start=3, end=11, block_size=2):
            assert len(part) <= 2
            result.extend(part)
        return bytes(result)
    assert execute(read_range()) == b"3456789A"
    assert backend.downloads == 3


def test_missing_telegram_chunk_never_publishes_partial_destination(store, tmp_path):
    backend = MemoryTelegram()
    engine = TelegramObjectStorage(store, backend, chunk_size=3)
    source = tmp_path / "source"
    source.write_bytes(b"abcdefghi")
    item = execute(engine.put_file("b", "k", source))
    part = store.parts(item.id)[1]
    del backend.data[(part.account, part.chat_id, part.message_id)]
    output = tmp_path / "output"
    output.write_bytes(b"previous-good-value")
    with pytest.raises(FileNotFoundError):
        execute(engine.download_to_path(item.id, output))
    assert output.read_bytes() == b"previous-good-value"
    assert not list(tmp_path.glob(".teledrive-*"))


@pytest.mark.parametrize("replacement", [b"xxxx", b"ab", b""])
def test_tampered_or_truncated_chunk_fails_closed(store, tmp_path, replacement):
    backend = MemoryTelegram()
    engine = TelegramObjectStorage(store, backend, chunk_size=4)
    source = tmp_path / "src"
    source.write_bytes(b"abcdefgh")
    item = execute(engine.put_file("b", "k", source))
    first = store.parts(item.id)[0]
    backend.data[(first.account, first.chat_id, first.message_id)] = replacement
    with pytest.raises(IntegrityError, match="checksum"):
        execute(engine.download_to_path(item.id, tmp_path / "out"))
    assert not (tmp_path / "out").exists()


def test_failed_upload_is_hidden_but_journaled_for_reconciliation(store, tmp_path):
    backend = MemoryTelegram()
    backend.fail_on_upload = 2
    engine = TelegramObjectStorage(store, backend, chunk_size=3)
    source = tmp_path / "src"
    source.write_bytes(b"abcdefghi")
    with pytest.raises(TimeoutError):
        execute(engine.put_file("b", "k", source))
    rows = store.interrupted()
    assert len(rows) == 1 and rows[0][1] == "failed"
    identifier = rows[0][0]
    with pytest.raises(FileNotFoundError):
        store.get(identifier)
    journal = store.recovery_parts(identifier)
    assert len(journal) == 1
    assert journal[0].size == 3


def test_changed_source_does_not_publish_object(store, tmp_path):
    backend = MemoryTelegram()
    source = tmp_path / "src"
    source.write_bytes(b"abcdefgh")
    async def edited():
        pass
    backend.on_upload = lambda: source.write_bytes(b"ABCDEFGH")
    engine = TelegramObjectStorage(store, backend, chunk_size=4)
    with pytest.raises(IntegrityError, match="Source changed"):
        execute(engine.put_file("b", "k", source))
    assert store.interrupted()[0][1] == "failed"


def test_manifest_refuses_incomplete_and_noncontiguous_parts(store):
    now = 123
    identifier = store.begin("my-bucket", "my/key", 9, now)
    reference = ChunkReference(0, 0, 4, hashlib.sha256(b"abcd").hexdigest(),
                               "primary", 12345, 99)
    store.append(identifier, reference, now)
    with pytest.raises(ManifestError, match="incomplete"):
        store.finish(identifier, hashlib.sha256(b"abcdefghi").hexdigest(), now)
    with pytest.raises(ManifestError, match="order"):
        store.append(identifier, ChunkReference(2, 5, 5, reference.sha256,
                                                "primary", 12345, 100), now)
    assert store.recovery_parts(identifier) == [reference]


def test_object_metadata_is_durable_across_database_restart(tmp_path):
    url = str(tmp_path / "metadata.sqlite")
    db = connect_database(url)
    try:
        first = ManifestStore(db)
        identifier = first.begin("bucket", "item", 0, 1)
        first.finish(identifier, hashlib.sha256(b"").hexdigest(), 2)
    finally:
        db.close()
    db2 = connect_database(url)
    try:
        second = ManifestStore(db2)
        assert second.get(identifier).size == 0
        assert second.parts(identifier) == []
    finally:
        db2.close()


def test_invalid_range_rejected_without_download(store, tmp_path):
    fake = MemoryTelegram()
    engine = TelegramObjectStorage(store, fake, chunk_size=2)
    path = tmp_path / "x"
    path.write_bytes(b"abcd")
    obj = execute(engine.put_file("b", "k", path))
    async def consume():
        return [chunk async for chunk in engine.iter_bytes(obj.id, start=3, end=5)]
    with pytest.raises(ValueError):
        execute(consume())
    assert fake.downloads == 0


def test_symlink_source_rejected_and_never_queued(store, tmp_path):
    source = tmp_path / "real"
    source.write_bytes(b"secret")
    link = tmp_path / "link"
    link.symlink_to(source)
    engine = TelegramObjectStorage(store, MemoryTelegram(), chunk_size=2)
    with pytest.raises(OSError):
        execute(engine.put_file("b", "k", link))
    assert store.interrupted() == []


def test_active_writer_lease_cannot_be_abandoned(store):
    ident = store.begin("backup", "active", 0, 100)
    store.start_writer(ident, "worker-a", 100, ttl=500)
    assert store.abandon_stale_pending(now=200, min_age_seconds=60) == []
    assert store.heartbeat_writer(ident, "worker-a", 200, ttl=500)
    assert store.abandon_stale_pending(now=500, min_age_seconds=60) == []
    ready = store.finish(ident, hashlib.sha256(b"").hexdigest(), 600, writer_token="worker-a")
    assert ready.id == ident
    assert store.abandon_stale_pending(now=900, min_age_seconds=60) == []


def test_expired_writer_fenced_before_pending_abandonment(store):
    ident = store.begin("backup", "interrupted", 0, 100)
    store.start_writer(ident, "crashed-worker", 100, ttl=120)
    assert store.abandon_stale_pending(now=161, min_age_seconds=60) == []
    assert store.abandon_stale_pending(now=221, min_age_seconds=60) == [ident]
    assert store.interrupted() == [(ident, "failed")]
    assert not store.heartbeat_writer(ident, "crashed-worker", 222, ttl=120)
    with pytest.raises(ManifestError, match="not pending"):
        store.finish(ident, hashlib.sha256(b"").hexdigest(), 222, writer_token="crashed-worker")
    with pytest.raises(ManifestError, match="lease lost"):
        store.fail(ident, "late worker", 222, writer_token="crashed-worker")


def test_stale_legacy_pending_without_writer_fence_is_not_deleted(store):
    ident = store.begin("legacy", "old", 0, 100)
    assert store.abandon_stale_pending(now=999_999, min_age_seconds=60) == []
    assert store.interrupted() == [(ident, "pending")]


def test_writer_tokens_cannot_be_impersonated(store):
    ident = store.begin("backup", "owned", 0, 100)
    store.start_writer(ident, "secret-a", 100, ttl=200)
    assert not store.heartbeat_writer(ident, "secret-b", 110)
    with pytest.raises(ManifestError, match="lease lost"):
        store.finish(ident, hashlib.sha256(b"").hexdigest(), 110, writer_token="secret-b")
    with pytest.raises(ManifestError, match="lease lost"):
        store.finish(ident, hashlib.sha256(b"").hexdigest(), 110)
    assert store.heartbeat_writer(ident, "secret-a", 150)
    assert store.finish(ident, hashlib.sha256(b"").hexdigest(), 151, writer_token="secret-a").id == ident


def test_process_killed_after_telegram_send_cannot_publish_later(store, tmp_path):
    backend = MemoryTelegram()
    source = tmp_path / "source"
    source.write_bytes(b"file-payload")
    def forced_crash_fence():
        # Simulates an unavailable worker long enough for another process to
        # fence its lease, after Telegram accepted the message.
        store.abandon_stale_pending(now=int(__import__("time").time()) + 10000,
                                    min_age_seconds=60)
    backend.on_upload = forced_crash_fence
    engine = TelegramObjectStorage(store, backend, chunk_size=4)
    with pytest.raises(ManifestError, match="ownership lost"):
        execute(engine.put_file("bucket", "item", source))
    assert len(backend.data) == 1
    assert store.interrupted()[0][1] == "failed"
    with pytest.raises(FileNotFoundError):
        store.get(store.interrupted()[0][0])
