"""Read-only production cutover preflight: local source -> SQL HEAD -> Telegram.

This is an acceptance gate, not an uploader. It cannot switch the live
legacy worker, mutate SQL, or delete Telegram media. A release operator
must separately configure durable cutover after a complete verified report.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.engine import TelegramObjectStorage, _regular_source
from teledrive.storage.jobs import (
    StorageUploadJobs, UploadJobError, _source_identity, jobs,
)

def _revision(info: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(info.st_dev), int(info.st_ino),
        int(info.st_size), int(info.st_mtime_ns),
    )


def _hash_same_source_revision(
    source: Path, root: Path, expected: os.stat_result,
) -> str:
    """Hash the exact regular-file inode without ever following final symlinks.

    A concurrent rename/symlink replacement between path validation and open
    must not cause verification to read arbitrary files outside source root.
    """
    resolved, initial = _source_identity(source, root)
    if resolved != source or _revision(initial) != _revision(expected):
        raise UploadJobError("Legacy source revision changed before checksum")
    with _regular_source(source) as handle:
        if _revision(os.fstat(handle.fileno())) != _revision(expected):
            raise UploadJobError("Legacy source revision changed during open")
        digest = hashlib.sha256()
        while block := handle.read(1024 * 1024):
            digest.update(block)
        if _revision(os.fstat(handle.fileno())) != _revision(expected):
            raise UploadJobError("Legacy source revision changed while hashing")
    resolved, final = _source_identity(source, root)
    if resolved != source or _revision(final) != _revision(expected):
        raise UploadJobError("Legacy source revision changed after checksum")
    return digest.hexdigest()


async def verify_legacy_cutover(
    cfg: Any, queue: StorageUploadJobs, transport: Any, *,
    bucket: str, prefix: str = "", approved_root: Path,
    max_files: int = 1000, max_bytes: int = 64 * 1024 * 1024,
    scratch_directory: Path | None = None,
) -> dict:
    """Only report complete when EVERY eligible local file is remotely read back.

    The caller must pause the legacy uploader and hold the global cutover
    lease externally before collecting a production acceptance report.
    A source revision with no successful queue job never qualifies.
    """
    from teledrive.uploader import iter_source_files, file_allowed

    if (type(max_files) is not int or not 1 <= max_files <= 1000
            or type(max_bytes) is not int or not 1 <= max_bytes <= 1024**3):
        raise ValueError("Invalid cutover verification bounds")
    root = Path(cfg.source_dir).resolve(strict=True)
    permitted = Path(approved_root).resolve(strict=True)
    if (not root.is_dir() or not root.is_relative_to(permitted)
            or bucket not in queue.catalog.list_buckets()
            or prefix.startswith("/") or "\\" in prefix
            or any(part in ("", ".", "..") for part in prefix.split("/") if prefix)):
        raise UploadJobError("Unsafe or unavailable cutover source/bucket/prefix")
    manifests = queue.catalog.manifests
    engine = TelegramObjectStorage(
        manifests, transport, scratch_directory=scratch_directory,
    )
    total = excluded = verified = queued = inconsistent = 0
    truncated = False
    initial_paths = []
    verified_revisions: dict[
        str, tuple[os.stat_result, str, str, str, int, str]
    ] = {}
    # Materialize at most max_files + 1 eligible entries. The original
    # sorted(os.walk(...)) scanned an unbounded number of paths into RAM
    # before noticing its max_files cap.
    for raw_path in iter_source_files(str(root)):
        if not file_allowed(raw_path, cfg):
            excluded += 1
            continue
        initial_paths.append(raw_path)
        if len(initial_paths) > max_files:
            truncated = True
            break
    baseline = set(initial_paths)
    if not truncated:
        for raw_path in sorted(initial_paths):
            if not file_allowed(raw_path, cfg):
                excluded += 1
                continue
            total += 1
            if total > max_files:
                truncated = True
                break
            try:
                source, before = _source_identity(Path(raw_path), root)
                if before.st_size > max_bytes:
                    inconsistent += 1
                    continue
                relative = source.relative_to(root).as_posix()
                key = f"{prefix}/{relative}" if prefix else relative
                job_id = queue.scan_id(bucket, key, source, before)
                task = queue.db.connection.execute(
                    select(jobs).where(jobs.c.id == job_id)
                ).mappings().one_or_none()
                if task is None or task["state"] != "done" or not task["object_id"]:
                    queued += 1
                    continue
                if task["bucket"] != bucket or task["object_key"] != key:
                    inconsistent += 1
                    continue
                live_record = queue.catalog.get(bucket, key)
                live_head = live_record.item
                if live_head.id != task["object_id"] or live_head.size != before.st_size:
                    inconsistent += 1
                    continue
                source_sha = _hash_same_source_revision(source, root, before)
                if live_head.sha256 != source_sha:
                    inconsistent += 1
                    continue
                remote_hash = hashlib.sha256()
                received = 0
                async for chunk in engine.iter_bytes(live_head.id):
                    remote_hash.update(chunk)
                    received += len(chunk)
                final_stat = _source_identity(source, root)[1]
                if (received != before.st_size
                        or remote_hash.hexdigest() != source_sha
                        or _revision(before) != _revision(final_stat)):
                    inconsistent += 1
                    continue
                verified += 1
                verified_revisions[raw_path] = (
                    before, source_sha, key, live_head.id,
                    live_record.generation, job_id,
                )
            except (OSError, ValueError, FileNotFoundError):
                inconsistent += 1
    if not truncated:
        # Files added/removed while Telegram was being read must block the
        # switchover, even if every file seen at the start passed its SHA.
        after_paths = set()
        for raw_path in iter_source_files(str(root)):
            if file_allowed(raw_path, cfg):
                after_paths.add(raw_path)
                if len(after_paths) > max_files:
                    truncated = True
                    break
        if not truncated and after_paths != baseline:
            inconsistent += len(after_paths.symmetric_difference(baseline))
        if not truncated:
            # Paths staying constant is NOT sufficient. An earlier verified
            # source may be modified while a later file is downloaded, then
            # have its original mtime restored. Independently re-hash EVERY
            # verified source at the end, when all Telegram reads are done.
            for raw_path, (
                original_stat, source_sha, key, head_id,
                generation, job_id,
            ) in verified_revisions.items():
                try:
                    source = Path(raw_path)
                    if _hash_same_source_revision(source, root, original_stat) != source_sha:
                        inconsistent += 1
                        continue
                    # Publication may change during another object's
                    # Telegram read. A previously valid source SHA is not
                    # enough if the serving HEAD/generation has moved.
                    final_head = queue.catalog.get(bucket, key)
                    final_job = queue.db.connection.execute(
                        select(jobs.c.state, jobs.c.object_id)
                        .where(jobs.c.id == job_id)
                    ).one_or_none()
                    if (final_head.item.id != head_id
                            or final_head.generation != generation
                            or final_job is None
                            or tuple(final_job) != ("done", head_id)):
                        inconsistent += 1
                except (OSError, ValueError, FileNotFoundError):
                    inconsistent += 1

    return {
        "verified": total > 0 and not truncated and total == verified
                    and queued == 0 and inconsistent == 0,
        "eligible_files": min(total, max_files),
        "verified_files": verified,
        "not_yet_published": queued,
        "inconsistent": inconsistent,
        "excluded": excluded, "truncated": truncated,
        "telegram_uploads": 0, "telegram_deletions": 0,
        "source_modifications": 0,
    }
