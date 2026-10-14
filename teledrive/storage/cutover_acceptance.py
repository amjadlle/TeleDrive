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
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.jobs import (
    StorageUploadJobs, UploadJobError, _source_identity, jobs,
)


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
    for raw_path in sorted(iter_source_files(str(root))):
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
            live_head = queue.catalog.get(bucket, key).item
            if live_head.id != task["object_id"] or live_head.size != before.st_size:
                inconsistent += 1
                continue
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
            after = _source_identity(source, root)[1]
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or live_head.sha256 != digest.hexdigest()):
                inconsistent += 1
                continue
            remote_hash = hashlib.sha256()
            received = 0
            async for chunk in engine.iter_bytes(live_head.id):
                remote_hash.update(chunk)
                received += len(chunk)
            final_stat = _source_identity(source, root)[1]
            if (received != before.st_size
                    or remote_hash.hexdigest() != digest.hexdigest()
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (final_stat.st_dev, final_stat.st_ino,
                        final_stat.st_size, final_stat.st_mtime_ns)):
                inconsistent += 1
                continue
            verified += 1
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
