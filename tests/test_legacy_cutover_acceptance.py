"""Cutover production rehearsal: every eligible source must match published Telegram."""
from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.cutover_acceptance import verify_legacy_cutover
from teledrive.storage.jobs import StorageUploadJobs
from teledrive.storage.legacy_bridge import enqueue_legacy_folder
from teledrive.storage.manifest import ManifestStore


class DiskTelegram:
    def __init__(self):
        self.messages = {}
        self.uploads = 0
        self.deletions = 0

    async def upload(self, path, *, object_id, ordinal, checksum):
        self.uploads += 1
        self.messages[self.uploads] = path.read_bytes()
        return "legacy-test", -100459, self.uploads

    async def download(self, ref, destination):
        destination.write_bytes(self.messages[ref.message_id])


def scenario(tmp_path, sizes=(0, 65537, 1048619)):
    src = tmp_path / "sources"
    src.mkdir()
    data = {}
    for i, n in enumerate(sizes):
        content = hashlib.shake_256(f"legacy:{i}".encode()).digest(n)
        name = f"document-{i}.bin"
        (src / name).write_bytes(content)
        data[name] = content
    (src / "excluded.txt").write_bytes(b"not selected")
    db = connect_database(str(tmp_path / "cutover.db"))
    catalog = BucketCatalog(ManifestStore(db))
    catalog.create_bucket("cutover-ci", int(time.time()))
    queue = StorageUploadJobs(catalog)
    cfg = SimpleNamespace(source_dir=str(src),
                          allowed_extensions=[".bin"], max_file_size_mb=0)
    report = enqueue_legacy_folder(
        cfg, queue, bucket="cutover-ci", prefix="archived", max_files=100,
    )
    assert report.queued == len(sizes) and report.excluded == 1
    return db, queue, cfg, src, data, DiskTelegram()


async def inspect(cfg, queue, remote, src, **kwargs):
    return await verify_legacy_cutover(
        cfg, queue, remote, bucket="cutover-ci", prefix="archived",
        approved_root=src, scratch_directory=src, **kwargs,
    )


def test_complete_cutover_acceptance_only_after_all_real_journals_verified(tmp_path):
    db, queue, cfg, src, data, remote = scenario(tmp_path)
    try:
        before = asyncio.run(inspect(cfg, queue, remote, src))
        assert not before["verified"] and before["not_yet_published"] == len(data)
        uploads = remote.uploads
        assert uploads == 0
        first = asyncio.run(queue.run_next(
            remote, allowed_root=src, scratch_directory=src, chunk_size=32768,
        ))
        assert first["state"] == "done"
        partial = asyncio.run(inspect(cfg, queue, remote, src))
        assert not partial["verified"]
        assert partial["verified_files"] == 1
        assert partial["not_yet_published"] == len(data) - 1
        for _ in range(len(data) - 1):
            done = asyncio.run(queue.run_next(
                remote, allowed_root=src, scratch_directory=src, chunk_size=32768,
            ))
            assert done["state"] == "done"
        all_done = asyncio.run(inspect(cfg, queue, remote, src))
        assert all_done == {
            "verified": True, "eligible_files": len(data),
            "verified_files": len(data), "not_yet_published": 0,
            "inconsistent": 0, "excluded": 1, "truncated": False,
            "telegram_uploads": 0, "telegram_deletions": 0,
            "source_modifications": 0,
        }
        assert queue.list_jobs(limit=20)
        for name, body in data.items():
            assert (src / name).read_bytes() == body
        uploads_before = remote.uploads
        assert asyncio.run(inspect(cfg, queue, remote, src))["verified"]
        assert remote.uploads == uploads_before and remote.deletions == 0

        # Operators cannot reuse an earlier clean report after a source edit.
        (src / "document-1.bin").write_bytes(b"source-replaced")
        changed = asyncio.run(inspect(cfg, queue, remote, src))
        assert not changed["verified"] and changed["not_yet_published"] >= 1
    finally:
        db.close()


@pytest.mark.parametrize("kind", ["remote-corruption", "max-files",
                                    "max-bytes", "outside-root"])
def test_cutover_rejects_incomplete_or_unsafe_state(tmp_path, kind):
    db, queue, cfg, src, _, remote = scenario(tmp_path, sizes=(1024, 8193))
    try:
        for _ in range(2):
            assert asyncio.run(queue.run_next(
                remote, allowed_root=src, scratch_directory=src, chunk_size=512,
            ))["state"] == "done"
        assert asyncio.run(inspect(cfg, queue, remote, src))["verified"]
        if kind == "remote-corruption":
            remote.messages[1] = b"corrupt-telegram"
            with pytest.raises(RuntimeError):
                asyncio.run(inspect(cfg, queue, remote, src))
        elif kind == "max-files":
            report = asyncio.run(inspect(cfg, queue, remote, src, max_files=1))
            assert not report["verified"] and report["truncated"]
        elif kind == "max-bytes":
            report = asyncio.run(inspect(cfg, queue, remote, src, max_bytes=512))
            assert not report["verified"] and report["inconsistent"] >= 1
        else:
            other = tmp_path / "outside"
            other.mkdir()
            with pytest.raises(ValueError):
                asyncio.run(inspect(cfg, queue, remote, other))
        assert remote.deletions == 0
    finally:
        db.close()
