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
        cwd=Path(__file__).resolve().parents[1],
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
