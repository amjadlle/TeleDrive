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

def test_cutover_detects_new_source_file_during_remote_telegram_readback(tmp_path):
    """A newly arrived file after scan start invalidates old HEAD acceptance."""
    db, queue, cfg, src, _, remote = scenario(tmp_path, sizes=(65537,))
    try:
        completed = asyncio.run(queue.run_next(
            remote, allowed_root=src, scratch_directory=src,
            chunk_size=32768,
        ))
        assert completed["state"] == "done"
        uploads_before = remote.uploads
        real_download = remote.download
        inserted = False

        async def mutate_after_scan(ref, destination):
            nonlocal inserted
            if not inserted:
                inserted = True
                (src / "new-arrival.bin").write_bytes(b"arrived during verification")
            await real_download(ref, destination)

        remote.download = mutate_after_scan
        report = asyncio.run(inspect(cfg, queue, remote, src))
        assert inserted
        assert not report["verified"]
        assert report["inconsistent"] >= 1
        assert report["telegram_uploads"] == report["telegram_deletions"] == 0
        assert remote.uploads == uploads_before and remote.deletions == 0
    finally:
        db.close()


def test_legacy_cutover_large_directory_cap_never_reads_telegram(tmp_path):
    db, queue, cfg, src, _, remote = scenario(tmp_path, sizes=(1,))
    try:
        for n in range(120):
            (src / f"late-{n:04d}.bin").write_bytes(bytes([n % 256]))
        result = asyncio.run(inspect(
            cfg, queue, remote, src, max_files=100,
        ))
        assert not result["verified"] and result["truncated"]
        assert result["verified_files"] == 0
        assert result["telegram_uploads"] == result["telegram_deletions"] == 0
        assert remote.uploads == 0 and remote.deletions == 0
    finally:
        db.close()


def test_cutover_detects_late_same_size_edit_even_with_original_mtime(tmp_path):
    """A second file's Telegram read must not mask a late edit to the first."""
    import os

    db, queue, cfg, src, _, remote = scenario(tmp_path, sizes=(65537, 65537))
    try:
        for _ in range(2):
            assert asyncio.run(queue.run_next(
                remote, allowed_root=src, scratch_directory=src,
                chunk_size=32768,
            ))["state"] == "done"
        assert asyncio.run(inspect(cfg, queue, remote, src))["verified"]
        original = src / "document-0.bin"
        stamp = original.stat()
        source_before = original.read_bytes()
        uploads_before = remote.uploads
        real_download = remote.download
        changed = False

        async def edit_earlier_source(ref, destination):
            nonlocal changed
            # The first file has three 32KiB chunks. Only mutate it when
            # the second file starts its independent Telegram readback.
            if not changed and ref.message_id >= 4:
                changed = True
                original.write_bytes(b"Q" * len(source_before))
                os.utime(
                    original, ns=(stamp.st_atime_ns, stamp.st_mtime_ns),
                )
            await real_download(ref, destination)

        remote.download = edit_earlier_source
        report = asyncio.run(inspect(cfg, queue, remote, src))
        assert changed
        assert not report["verified"]
        assert report["inconsistent"] >= 1
        assert original.stat().st_size == stamp.st_size
        assert original.stat().st_mtime_ns == stamp.st_mtime_ns
        assert remote.uploads == uploads_before and remote.deletions == 0
    finally:
        db.close()


def test_cutover_never_follows_last_second_symlink_outside_root(
    tmp_path, monkeypatch,
):
    """A file swapped after identity check must never be opened via symlink."""
    import teledrive.storage.cutover_acceptance as acceptance

    db, queue, cfg, src, _, remote = scenario(tmp_path, sizes=(65537,))
    outside = tmp_path / "outside-secret"
    outside.write_bytes(b"outside root: forbidden")
    try:
        assert asyncio.run(queue.run_next(
            remote, allowed_root=src, scratch_directory=src,
            chunk_size=32768,
        ))["state"] == "done"
        original = acceptance._regular_source
        path = src / "document-0.bin"
        triggered = False

        def swap_before_open(source):
            nonlocal triggered
            if not triggered:
                triggered = True
                path.unlink()
                path.symlink_to(outside)
            return original(source)

        monkeypatch.setattr(acceptance, "_regular_source", swap_before_open)
        before_uploads = remote.uploads
        report = asyncio.run(inspect(cfg, queue, remote, src))
        assert triggered
        assert not report["verified"]
        assert report["inconsistent"] >= 1
        assert remote.uploads == before_uploads
        assert remote.deletions == 0
    finally:
        db.close()
