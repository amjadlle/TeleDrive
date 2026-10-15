"""Independent OS workers share one SQL queue without double-claiming jobs.

No Telegram credentials are used: a disk-backed fake is process-shared and
every reconstructed payload is verified through the real chunk engine.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.jobs import StorageUploadJobs
from teledrive.storage.manifest import ManifestStore

WORKER = r'''
import asyncio
import secrets
import sys
import time
from pathlib import Path
from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.jobs import StorageUploadJobs
from teledrive.storage.manifest import ManifestStore

class SharedRemote:
    def __init__(self, root):
        self.root = root

    async def upload(self, source, *, object_id, ordinal, checksum):
        mid = secrets.randbits(62) + 1
        (self.root / f"{mid}.bin").write_bytes(source.read_bytes())
        return "process-remote", -100987654321, mid

    async def download(self, chunk, destination):
        destination.write_bytes((self.root / f"{chunk.message_id}.bin").read_bytes())

async def work():
    path, root, remote, ready, go = map(Path, sys.argv[1:])
    db = connect_database(str(path))
    try:
        manager = StorageUploadJobs(BucketCatalog(ManifestStore(db)))
        ready.write_text("ready", encoding="ascii")
        for _ in range(500):
            if go.exists():
                break
            await asyncio.sleep(0.02)
        else:
            raise RuntimeError("Start signal was not received")
        for _ in range(30):
            outcome = await manager.run_next(
                SharedRemote(remote), allowed_root=root,
                scratch_directory=root, chunk_size=37,
            )
            if outcome is None:
                return
        raise RuntimeError("Unexpected excessive worker claims")
    finally:
        db.close()

asyncio.run(work())
'''


class SharedRemote:
    def __init__(self, directory: Path):
        self.directory = directory

    async def download(self, ref, destination):
        destination.write_bytes(
            (self.directory / f"{ref.message_id}.bin").read_bytes()
        )


def test_independent_sqlite_workers_soak_claims_and_restore_all_bytes(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    remote = tmp_path / "remote"
    remote.mkdir()
    db_path = tmp_path / "metadata.sqlite"
    db = connect_database(str(db_path))
    payloads = {}
    try:
        catalog = BucketCatalog(ManifestStore(db))
        catalog.create_bucket("multi-worker", int(time.time()))
        queue = StorageUploadJobs(catalog)
        for n in range(12):
            content = bytes([n]) * (91 + 13 * n)
            file = root / f"file-{n:02d}.bin"
            file.write_bytes(content)
            payloads[f"objects/{n:02d}.bin"] = content
            queue.enqueue(
                "multi-worker", f"objects/{n:02d}.bin", file,
                allowed_root=root,
            )
    finally:
        db.close()

    go = tmp_path / "start-workers"
    processes = []
    try:
        for index in range(3):
            ready = tmp_path / f"worker-{index}.ready"
            proc = subprocess.Popen(
                [sys.executable, "-c", WORKER,
                 str(db_path), str(root), str(remote), str(ready), str(go)],
                cwd=Path(__file__).resolve().parents[2],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            processes.append((proc, ready))
        deadline = time.monotonic() + 22
        while not all(ready.exists() for _, ready in processes):
            assert time.monotonic() < deadline, "Workers did not start"
            for proc, _ in processes:
                assert proc.poll() is None, "Worker exited before start gate"
            time.sleep(0.04)
        go.write_text("go", encoding="ascii")
        for proc, _ in processes:
            out, err = proc.communicate(timeout=50)
            assert proc.returncode == 0, (
                f"Worker exit {proc.returncode}: {err.decode(errors='replace')[-3500:]}"
            )
    finally:
        for proc, _ in processes:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)

    reopened = connect_database(str(db_path))
    try:
        catalog = BucketCatalog(ManifestStore(reopened))
        states = StorageUploadJobs(catalog).list_jobs(limit=1000)
        assert len(states) == 12
        assert all(j["state"] == "done" and j["attempts"] == 1 for j in states)
        assert len({j["object_id"] for j in states}) == 12
        expected_remote_files = 0
        for key, data in payloads.items():
            obj = catalog.get("multi-worker", key).item
            refs = catalog.manifests.parts(obj.id)
            expected_remote_files += len(refs)
            restored = tmp_path / (key.replace("/", "-"))
            asyncio.run(TelegramObjectStorage(
                catalog.manifests, SharedRemote(remote), chunk_size=37
            ).download_to_path(obj.id, restored))
            assert restored.read_bytes() == data
        assert len(list(remote.glob("*.bin"))) == expected_remote_files
    finally:
        reopened.close()
