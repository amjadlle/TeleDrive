"""Actual SIGKILL/crash of an upload worker with SQLite and Telegram disk stub.

This runs the real durable-job and storage-engine implementations in another
OS process; after SIGKILL only committed SQL and fake remote media survive.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import update

from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.jobs import StorageUploadJobs, jobs
from teledrive.storage.manifest import ManifestStore, writer_fences

CHILD = r'''
import asyncio
import os
import signal
import sys
from pathlib import Path
from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.jobs import StorageUploadJobs
from teledrive.storage.manifest import ManifestStore

class DiskTelegram:
    async def upload(self, path, *, object_id, ordinal, checksum):
        if ordinal == 1:
            os.kill(os.getpid(), signal.SIGKILL)
        remote = Path(sys.argv[3]) / f"{ordinal + 1}.bin"
        remote.write_bytes(path.read_bytes())
        return "disk", -100111, ordinal + 1

    async def download(self, ref, destination):
        destination.write_bytes((Path(sys.argv[3]) / f"{ref.message_id}.bin").read_bytes())

async def run():
    db = connect_database(sys.argv[1])
    try:
        await StorageUploadJobs(BucketCatalog(ManifestStore(db))).run_next(
            DiskTelegram(), allowed_root=Path(sys.argv[2]),
            scratch_directory=Path(sys.argv[2]), chunk_size=5,
        )
    finally:
        db.close()

asyncio.run(run())
'''


class DiskTelegram:
    def __init__(self, directory):
        self.directory = directory
        self.uploads = 0
        self.downloads = 0

    async def upload(self, path, *, object_id, ordinal, checksum):
        self.uploads += 1
        destination = self.directory / f"{ordinal + 1}.bin"
        if destination.exists():
            raise AssertionError("Previously journaled Telegram chunk retransmitted")
        destination.write_bytes(path.read_bytes())
        return "disk", -100111, ordinal + 1

    async def download(self, ref, destination):
        self.downloads += 1
        destination.write_bytes(
            (self.directory / f"{ref.message_id}.bin").read_bytes()
        )


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL is required")
def test_real_sigkill_recovers_only_missing_remote_chunks(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    remote = tmp_path / "remote"
    remote.mkdir()
    body = bytes(range(19))
    source = root / "source.bin"
    source.write_bytes(body)
    db_path = tmp_path / "state.db"
    conn = connect_database(str(db_path))
    try:
        catalog = BucketCatalog(ManifestStore(conn))
        catalog.create_bucket("sigkill-ci", int(time.time()))
        job_id = StorageUploadJobs(catalog).enqueue(
            "sigkill-ci", "crash.bin", source, allowed_root=root,
        )
    finally:
        conn.close()

    killed = subprocess.run(
        [sys.executable, "-c", CHILD, str(db_path), str(root), str(remote)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, timeout=25, check=False,
    )
    assert killed.returncode == -signal.SIGKILL, killed.stderr.decode(errors="replace")
    assert (remote / "1.bin").read_bytes() == body[:5]
    assert not (remote / "2.bin").exists()
    reopened = connect_database(str(db_path))
    try:
        catalog = BucketCatalog(ManifestStore(reopened))
        queue = StorageUploadJobs(catalog)
        recorded = queue.list_jobs()[0]
        assert recorded["id"] == job_id
        assert recorded["state"] == "running"
        identifier = recorded["object_id"]
        assert identifier
        refs = catalog.manifests.recovery_parts(identifier)
        assert len(refs) == 1 and refs[0].size == 5
        with pytest.raises(FileNotFoundError):
            catalog.get("sigkill-ci", "crash.bin")
        # The OS process has genuinely died; advance expired lease time
        # without waiting 120 seconds in CI.
        reopened.connection.execute(
            update(writer_fences).where(writer_fences.c.object_id == identifier)
            .values(lease_until=0)
        )
        reopened.connection.execute(
            update(jobs).where(jobs.c.id == job_id).values(lease_until=0)
        )
        reopened.commit()
        transport = DiskTelegram(remote)
        result = asyncio.run(queue.run_next(
            transport, allowed_root=root, scratch_directory=root, chunk_size=5,
        ))
        assert result == {"id": job_id, "state": "done", "object_id": identifier}
        assert transport.uploads == 3
        assert transport.downloads == 1
        restored = root / "restored.bin"
        asyncio.run(TelegramObjectStorage(
            catalog.manifests, transport,
        ).download_to_path(identifier, restored))
        assert restored.read_bytes() == body
        assert (remote / "1.bin").read_bytes() == body[:5]
    finally:
        reopened.close()

MT_ACK_CRASH_CHILD = r'''
import asyncio
import hashlib
import os
import signal
import sys
from pathlib import Path

from teledrive.db import connect_database
from teledrive.storage.user_mode import UnifiedUserTransport, UserSendJournal
from teledrive.storage.telethon_adapter import TelethonChunkTransport

class AcceptedThenKilled:
    async def send_file(self, target, path, caption=None, force_document=False):
        # Simulate a remote Telegram post that really accepted the bytes.
        Path(sys.argv[3]).write_bytes(Path(path).read_bytes())
        os.kill(os.getpid(), signal.SIGKILL)
        raise AssertionError("SIGKILL should prevent an ACK from returning")

    async def get_peer_id(self, target):
        return -1001234

    async def get_messages(self, peer, ids):
        raise AssertionError("readback is not part of this crash window")


class FakeRuntime:
    account = "user"
    chat_id = -1001234

    def require(self):
        return TelethonChunkTransport({"user": (AcceptedThenKilled(), None)})


async def main():
    db = connect_database(sys.argv[1])
    try:
        path = Path(sys.argv[2])
        transport = UnifiedUserTransport(FakeRuntime(), UserSendJournal(db),
                                         min_upload_interval=0)
        await transport.upload(
            path, object_id="sigkill-user-ack", ordinal=0,
            checksum=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    finally:
        db.close()

asyncio.run(main())
'''


@pytest.mark.skipif(os.name != "posix", reason="POSIX SIGKILL required")
def test_actual_sigkill_after_remote_user_acceptance_never_blindly_retries(tmp_path):
    """A real child dies after remote acceptance and before its MTProto receipt.

    Recovery sees the previously committed send intent on a new SQL
    connection and cannot retransmit that exact object chunk. An operator
    may later record an independently proven ACK; no unproven message is
    automatically inserted into the manifest or deleted.
    """
    import hashlib
    from teledrive.storage.user_mode import (
        UnifiedUserTransport, UserSendJournal, mtproto_send_intents,
    )
    from teledrive.storage.telethon_adapter import TelethonChunkTransport
    from sqlalchemy import select

    path = tmp_path / "mtproto-intents.db"
    chunk = tmp_path / "signed-binary.bin"
    accepted = tmp_path / "telegram-accepted.bin"
    data = bytes(range(256)) * 400 + bytes((0, 255)) * 170
    chunk.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    seed = connect_database(str(path))
    try:
        UserSendJournal(seed)
    finally:
        seed.close()

    result = subprocess.run(
        [sys.executable, "-c", MT_ACK_CRASH_CHILD,
         str(path), str(chunk), str(accepted)],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == -signal.SIGKILL, result.stderr.decode(errors="replace")
    assert accepted.read_bytes() == data  # remote post definitely occurred

    reopened = connect_database(str(path))
    try:
        journal = UserSendJournal(reopened)
        assert journal.unresolved_count() == 1
        with pytest.raises(RuntimeError, match="Unresolved MTProto upload intent"):
            journal.start("user", -1001234, "sigkill-user-ack", 0, sha, len(data))

        class UnexpectedResend:
            calls = 0

            async def send_file(self, target, path, caption=None,
                                force_document=False):
                self.calls += 1
                raise AssertionError("Unknown-outcome chunk was resent")

        fake = UnexpectedResend()

        class ResendRuntime:
            account = "user"
            chat_id = -1001234

            def require(self):
                return TelethonChunkTransport({"user": (fake, None)})

        transport = UnifiedUserTransport(ResendRuntime(), journal,
                                         min_upload_interval=0)
        with pytest.raises(RuntimeError, match="Unresolved MTProto upload intent"):
            asyncio.run(transport.upload(
                chunk, object_id="sigkill-user-ack", ordinal=0, checksum=sha,
            ))
        assert fake.calls == 0
        # A separate owner-authorized proof of original message identity
        # and byte-for-byte match is required before recovery.
        assert hashlib.sha256(accepted.read_bytes()).hexdigest() == sha
        journal.acknowledged("user", -1001234, "sigkill-user-ack", 0, 99)
        assert journal.unresolved_count() == 0
        recorded = reopened.connection.execute(
            select(mtproto_send_intents.c.message_id).where(
                mtproto_send_intents.c.object_id == "sigkill-user-ack",
            )
        ).scalar_one()
        assert recorded == 99
    finally:
        reopened.close()
