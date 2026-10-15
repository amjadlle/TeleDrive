"""Behavioral recovery tests: real SQLite journal and fake Telegram history.

Verify positive ownership before deletion, orphan discovery, partial cleanup,
restart behavior, concurrent leases, and fail-closed incomplete scans.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import replace
from pathlib import Path

import pytest

from teledrive.db import connect_database
from teledrive.storage.manifest import ManifestStore, ChunkReference
from teledrive.storage.recovery import (
    CleanupReport, Discovery, RecoveryManager, RemoteChunk, UnsafeCleanup,
)
from teledrive.storage.telethon_adapter import TelethonChunkTransport


def run(coro):
    return asyncio.run(coro)


class FakeRecoveryTransport:
    def __init__(self, *, complete=True):
        self.messages: dict[tuple[str, int, int], RemoteChunk] = {}
        self.complete = complete
        self.fail_delete = False
        self.deleted: list[int] = []

    async def discover(self, object_id, *, scan_limit):
        values = tuple(self.messages.values())
        return Discovery(values[:scan_limit], self.complete and len(values) <= scan_limit, len(values))

    async def probe(self, chunk, *, object_id):
        actual = self.messages.get(chunk.identity)
        if actual is None:
            return "missing"
        return "verified" if actual == chunk else "mismatch"

    async def delete_verified(self, chunk, *, object_id):
        if self.fail_delete:
            self.fail_delete = False
            return False
        if await self.probe(chunk, object_id=object_id) != "verified":
            return False
        del self.messages[chunk.identity]
        self.deleted.append(chunk.message_id)
        return True


@pytest.fixture
def db_store(tmp_path):
    connection = connect_database(str(tmp_path / "state.sqlite"))
    try:
        yield ManifestStore(connection)
    finally:
        connection.close()


def failed_object(store, *, message_id=10, with_journal=True):
    # A failed upload older than the required operator safety window.
    older = int(time.time()) - 7200
    ident = store.begin("ci", "backups/archive.bin", 12, older)
    sha = hashlib.sha256(b"abcdef").hexdigest()
    part = ChunkReference(ordinal=0, offset=0, size=6, sha256=sha,
                          account="primary", chat_id=12345, message_id=message_id)
    if with_journal:
        store.append(ident, part, older)
    store.fail(ident, "simulated timeout", older)
    return ident, RemoteChunk("primary", 12345, message_id, 0, sha[:16])


def test_dry_run_detects_recorded_and_unindexed_telegram_messages(db_store):
    ident, saved = failed_object(db_store)
    remote = FakeRecoveryTransport()
    orphan = RemoteChunk("primary", 12345, 11, 1, "a" * 16)
    remote.messages[saved.identity] = saved
    remote.messages[orphan.identity] = orphan
    report = run(RecoveryManager(db_store, remote, min_age_seconds=3600).cleanup(ident))
    assert report.journaled == 1
    assert report.orphaned == 1
    assert report.discovered == 2
    assert report.deleted == 0 and report.dry_run
    assert set(remote.messages) == {saved.identity, orphan.identity}
    assert db_store.interrupted()[0][1] == "failed"


def test_explicit_cleanup_deletes_only_owned_messages_and_is_durable(db_store):
    ident, saved = failed_object(db_store)
    remote = FakeRecoveryTransport()
    orphan = RemoteChunk("primary", 12345, 19, 1, "b" * 16)
    remote.messages = {saved.identity: saved, orphan.identity: orphan}
    report = run(RecoveryManager(db_store, remote, min_age_seconds=3600).cleanup(ident, dry_run=False))
    assert report.cleaned and report.deleted == 2
    assert set(remote.deleted) == {10, 19}
    assert not remote.messages
    with pytest.raises(FileNotFoundError):
        db_store.get(ident)
    with pytest.raises(UnsafeCleanup, match="Only failed"):
        run(RecoveryManager(db_store, remote, min_age_seconds=3600).cleanup(ident, dry_run=False))


def test_truncated_scan_blocks_destructive_cleanup(db_store):
    ident, saved = failed_object(db_store)
    remote = FakeRecoveryTransport(complete=False)
    remote.messages[saved.identity] = saved
    with pytest.raises(UnsafeCleanup, match="truncated"):
        run(RecoveryManager(db_store, remote, min_age_seconds=3600).cleanup(ident, dry_run=False))
    assert remote.deleted == []
    assert db_store.interrupted()[0][1] == "failed"


def test_mismatched_journal_reference_blocks_deletion(db_store):
    ident, saved = failed_object(db_store)
    remote = FakeRecoveryTransport()
    forged = replace(saved, checksum_prefix="0" * 16)
    remote.messages[saved.identity] = forged
    with pytest.raises(UnsafeCleanup, match="does not belong"):
        run(RecoveryManager(db_store, remote, min_age_seconds=3600).cleanup(ident, dry_run=False))
    assert remote.deleted == []


def test_partial_failure_is_retryable_without_double_deleting(db_store):
    ident, saved = failed_object(db_store)
    remote = FakeRecoveryTransport()
    remote.messages[saved.identity] = saved
    remote.fail_delete = True
    manager = RecoveryManager(db_store, remote, min_age_seconds=3600)
    with pytest.raises(UnsafeCleanup, match="not verified"):
        run(manager.cleanup(ident, dry_run=False))
    assert len(remote.messages) == 1
    assert db_store.interrupted()[0][1] == "failed"
    assert run(manager.cleanup(ident, dry_run=False)).cleaned
    assert remote.deleted == [saved.message_id]


def test_known_chunk_previously_removed_is_idempotently_cleanable(db_store):
    ident, _ = failed_object(db_store)
    remote = FakeRecoveryTransport()
    report = run(RecoveryManager(db_store, remote, min_age_seconds=3600).cleanup(ident, dry_run=False))
    assert report.cleaned and report.missing == 1 and report.deleted == 0


def test_pending_and_recent_failures_cannot_be_cleaned(db_store):
    remote = FakeRecoveryTransport()
    fresh = db_store.begin("ci", "pending", 0, int(time.time()))
    with pytest.raises(UnsafeCleanup, match="Only failed"):
        run(RecoveryManager(db_store, remote).cleanup(fresh, dry_run=False))
    db_store.fail(fresh, "recent", int(time.time()))
    with pytest.raises(UnsafeCleanup, match="aged"):
        run(RecoveryManager(db_store, remote).cleanup(fresh, dry_run=False))


def test_existing_cleanup_lease_prevents_second_worker(db_store):
    ident, chunk = failed_object(db_store)
    remote = FakeRecoveryTransport()
    now = int(time.time())
    lease = f"storage-gc:{ident}"
    assert db_store.db.acquire_lease(lease, "other", now, 300)
    try:
        with pytest.raises(UnsafeCleanup, match="Another cleanup worker"):
            run(RecoveryManager(db_store, remote, min_age_seconds=3600).cleanup(ident, dry_run=False))
    finally:
        db_store.db.release_lease(lease, "other")


class FakeMessage:
    def __init__(self, ident, ordinal, digest, *, mid=12, chat_id=12345, outgoing=True):
        self.message = f"teledrive:{ident}:{ordinal}:{digest[:16]}"
        self.id = mid
        self.chat_id = chat_id
        self.out = outgoing


class FakeTelethon:
    def __init__(self, messages):
        self.messages = {m.id: m for m in messages}
        self.deletions: list[int] = []

    async def get_peer_id(self, target):
        return 12345

    async def get_messages(self, target, *, ids):
        return self.messages.get(ids)

    async def iter_messages(self, target, *, limit):
        for msg in list(self.messages.values())[:limit]:
            yield msg

    async def delete_messages(self, target, ids, *, revoke):
        for mid in ids:
            self.deletions.append(mid)
            self.messages.pop(mid, None)


def test_telethon_adapter_rejects_spoofed_markers_and_nonowned_messages(db_store):
    identifier, _ = failed_object(db_store)
    digest = hashlib.sha256(b"abcdef").hexdigest()
    correct = FakeMessage(identifier, 0, digest, mid=40)
    forged = FakeMessage(identifier, 0, digest, mid=41, outgoing=False)
    unrelated = FakeMessage("00000000-0000-0000-0000-000000000000", 0, digest, mid=42)
    client = FakeTelethon([correct, forged, unrelated])
    adapter = TelethonChunkTransport({"primary": (client, "me")})
    remote = RemoteChunk("primary", 12345, 40, 0, digest[:16])
    assert run(adapter.probe(remote, object_id=identifier)) == "verified"
    discovery = run(adapter.discover(identifier, scan_limit=10))
    assert not discovery.complete  # forged channel message makes scan ambiguous
    assert discovery.matches == (remote,)
    assert run(adapter.delete_verified(remote, object_id=identifier))
    assert client.deletions == [40]
    assert 41 in client.messages
    with pytest.raises(UnsafeCleanup):
        run(RecoveryManager(db_store, adapter, min_age_seconds=3600).cleanup(identifier, dry_run=False))


def test_telethon_adapter_scan_limit_cannot_hide_older_orphans(db_store):
    identifier, _ = failed_object(db_store)
    digest = hashlib.sha256(b"abcdef").hexdigest()
    client = FakeTelethon([
        FakeMessage(identifier, i, digest, mid=100+i) for i in range(3)
    ])
    adapter = TelethonChunkTransport({"primary": (client, "me")})
    result = run(adapter.discover(identifier, scan_limit=2))
    assert not result.complete
    assert len(result.matches) == 2


def test_unindexed_telegram_upload_from_db_crash_is_discovered(db_store):
    """send_file succeeded, append() never committed: caption still locates it."""
    ident, _ = failed_object(db_store, with_journal=False)
    remote = FakeRecoveryTransport()
    orphan = RemoteChunk("primary", 12345, 500, 0, "c" * 16)
    remote.messages[orphan.identity] = orphan
    manager = RecoveryManager(db_store, remote, min_age_seconds=3600)
    preview = run(manager.cleanup(ident))
    assert preview.journaled == 0
    assert preview.orphaned == 1
    assert remote.messages
    report = run(manager.cleanup(ident, dry_run=False))
    assert report.cleaned and report.deleted == 1
    assert not remote.messages


def test_ready_object_is_never_a_cleanup_candidate(db_store):
    digest = hashlib.sha256(b"").hexdigest()
    ident = db_store.begin("ci", "finished", 0, int(time.time()) - 100000)
    db_store.finish(ident, digest, int(time.time()) - 100000)
    remote = FakeRecoveryTransport()
    with pytest.raises(UnsafeCleanup, match="Only failed"):
        run(RecoveryManager(db_store, remote).cleanup(ident, dry_run=False))
    assert db_store.get(ident).size == 0


def test_unknown_account_reference_does_not_trigger_deletion(db_store):
    ident, known = failed_object(db_store)
    remote = FakeRecoveryTransport()
    # A different account is not the account recorded for this chunk.
    fake = replace(known, account="unexpected")
    remote.messages[known.identity] = fake
    with pytest.raises(UnsafeCleanup, match="does not belong"):
        run(RecoveryManager(db_store, remote, min_age_seconds=3600).cleanup(ident, dry_run=False))
    assert not remote.deleted


def test_physical_cleanup_rejects_published_version_even_if_object_failed(db_store):
    """A failed but historically indexed object cannot lose its original bytes."""
    from sqlalchemy import insert
    from teledrive.storage.catalog import BucketCatalog, published_versions

    ident, chunk = failed_object(db_store)
    BucketCatalog(db_store)
    db_store.db.connection.execute(insert(published_versions).values(
        object_id=ident, bucket="ci", key_hash="a" * 64, committed_ns=123,
    ))
    db_store.db.commit()
    transport = FakeRecoveryTransport()
    transport.messages[chunk.identity] = chunk
    preview = run(RecoveryManager(db_store, transport, min_age_seconds=3600).cleanup(ident))
    assert preview.dry_run and preview.deleted == 0
    with pytest.raises(UnsafeCleanup, match="another SQL reference"):
        run(RecoveryManager(db_store, transport, min_age_seconds=3600).cleanup(
            ident, dry_run=False
        ))
    assert not transport.deleted and chunk.identity in transport.messages


def test_physical_cleanup_rejects_copy_alias_and_unresolved_mtproto_intent(db_store):
    from sqlalchemy import insert
    from teledrive.storage.manifest import object_aliases
    from teledrive.storage.user_mode import UserSendJournal, mtproto_send_intents

    ident, chunk = failed_object(db_store)
    copy = db_store.begin("ci", "copy-alias", 0, int(time.time()) - 9000)
    db_store.db.connection.execute(insert(object_aliases).values(
        object_id=copy, source_object_id=ident,
    ))
    db_store.db.commit()
    transport = FakeRecoveryTransport()
    transport.messages[chunk.identity] = chunk
    manager = RecoveryManager(db_store, transport, min_age_seconds=3600)
    with pytest.raises(UnsafeCleanup, match="another SQL reference"):
        run(manager.cleanup(ident, dry_run=False))
    db_store.db.connection.execute(object_aliases.delete().where(
        object_aliases.c.object_id == copy
    ))
    db_store.db.commit()
    journal = UserSendJournal(db_store.db)
    journal.start("primary", 12345, ident, 0,
                  hashlib.sha256(b"abcdef").hexdigest(), 6)
    with pytest.raises(UnsafeCleanup, match="another SQL reference"):
        run(manager.cleanup(ident, dry_run=False))
    assert transport.deleted == []


def test_physical_cleanup_refuses_expired_unreconciled_writer_fence(db_store):
    """A lost writer lease does not prove Telegram sends have stopped."""
    identifier, part = failed_object(db_store)
    remote = FakeRecoveryTransport()
    remote.messages[part.identity] = part
    db_store.start_writer(identifier, "not-yet-reconciled",
                          int(time.time()) - 3600, ttl=120)
    with pytest.raises(UnsafeCleanup, match="retained"):
        run(RecoveryManager(db_store, remote, min_age_seconds=3600).cleanup(
            identifier, dry_run=False,
        ))
    assert remote.deleted == []
    assert part.identity in remote.messages
    assert db_store.interrupted()[0][1] == "failed"


def _queued_job(db_store, *, object_id=None, expected_head_id=None,
                bucket="ci", key="backups/archive.bin", index=0):
    from sqlalchemy import insert
    from teledrive.storage.jobs import StorageUploadJobs, jobs
    from teledrive.storage.catalog import BucketCatalog
    StorageUploadJobs(BucketCatalog(db_store))
    now = int(time.time())
    db_store.db.connection.execute(insert(jobs).values(
        id=f"job-{index}-{object_id or expected_head_id or key}",
        bucket=bucket, object_key=key, source_path="/uploads/source.bin",
        source_size=6, source_device=1, source_inode=2, source_mtime_ns=3,
        expected_generation=0, expected_head_id=expected_head_id,
        state="queued", object_id=object_id, attempts=0,
        lease_token=None, lease_until=None, created_at=now,
        updated_at=now, last_error=None,
    ))
    db_store.db.commit()


def test_physical_cleanup_rejects_active_durable_job_reference(db_store):
    """A queued job that can still publish this key must fence physical GC."""
    identifier, part = failed_object(db_store)
    remote = FakeRecoveryTransport()
    remote.messages[part.identity] = part
    manager = RecoveryManager(db_store, remote, min_age_seconds=3600)
    for index, kwargs in enumerate(({"object_id": identifier},
                                    {"expected_head_id": identifier},
                                    {})):
        _queued_job(db_store, index=index, **kwargs)
        with pytest.raises(UnsafeCleanup, match="another SQL reference"):
            run(manager.cleanup(identifier, dry_run=False))
    assert remote.deleted == []
    assert part.identity in remote.messages


def test_physical_cleanup_rejects_mtproto_send_intent_reference(db_store):
    """An unresolved USER-MTProto send intent keeps the failed object protected."""
    from teledrive.storage.user_mode import UserSendJournal, mtproto_send_intents
    from sqlalchemy import insert

    identifier, part = failed_object(db_store)
    remote = FakeRecoveryTransport()
    remote.messages[part.identity] = part
    UserSendJournal(db_store.db)  # create_all for the send-intent table
    db_store.db.connection.execute(insert(mtproto_send_intents).values(
        account="mtproto-user", chat_id=-100987654321, object_id=identifier,
        ordinal=0, sha256=part.checksum_prefix + "0" * 48, size=6,
        message_id=None,
    ))
    db_store.db.commit()
    with pytest.raises(UnsafeCleanup, match="another SQL reference"):
        run(RecoveryManager(db_store, remote, min_age_seconds=3600).cleanup(
            identifier, dry_run=False,
        ))
    assert remote.deleted == []
    assert part.identity in remote.messages
