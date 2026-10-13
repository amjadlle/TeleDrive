"""Explicit, non-uploading bridge from legacy folder scan to durable jobs.

This NEVER changes the legacy files table or sends Telegram data. Operators
must opt in to a destination bucket and stop legacy background uploaders
before enabling the independent durable object worker.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from teledrive.storage.jobs import StorageUploadJobs, jobs, _source_identity, UploadJobError


@dataclass(frozen=True)
class LegacyQueueReport:
    queued: int
    already_queued: int
    excluded: int
    job_ids: tuple[str, ...]


def enqueue_legacy_folder(
    cfg, queue: StorageUploadJobs, *, bucket: str, prefix: str = "",
    max_files: int = 1000, dry_run: bool = False,
    lease_heartbeat=None,
) -> LegacyQueueReport:
    """Queue eligible legacy source files as immutable S3 object-key jobs.

    Any upload is performed later by the separately configured durable worker.
    Repeated scans of the same device/inode/mtime/size+destination are
    idempotent even when the old file table contains uploaded entries.
    """
    from teledrive.uploader import iter_source_files, file_allowed

    if not 1 <= max_files <= 1000:
        raise ValueError("Legacy bridge max_files must be between 1 and 1000")
    if (prefix.startswith("/") or "\\" in prefix
            or any(segment in ("", ".", "..") for segment in prefix.split("/"))
            and prefix != ""):
        raise ValueError("Legacy object prefix must be a relative safe key path")
    if bucket not in queue.catalog.list_buckets():
        raise FileNotFoundError("Object bucket must already exist")
    root = Path(cfg.source_dir).resolve(strict=True)
    if not root.is_dir():
        raise UploadJobError("Legacy source root must be a directory")
    queued = previous = excluded = 0
    identifiers: list[str] = []
    # Sort for deterministic bounded scans across restarts.
    for candidate in sorted(iter_source_files(str(root))):
        if lease_heartbeat is not None:
            lease_heartbeat()
        if not file_allowed(candidate, cfg):
            excluded += 1
            continue
        try:
            path, stat_info = _source_identity(Path(candidate), root)
            relative = path.relative_to(root).as_posix()
            key = f"{prefix}/{relative}" if prefix else relative
            queue.catalog._keyhash(key)
            ident = queue.scan_id(bucket, key, path, stat_info)
            found = queue.db.connection.execute(
                select(jobs.c.id).where(jobs.c.id == ident)
            ).scalar_one_or_none()
            if found is not None:
                previous += 1
                continue
            if not dry_run:
                queue.enqueue(
                    bucket, key, path, allowed_root=root,
                    deduplicate=True,
                )
            queued += 1
            identifiers.append(ident)
        except (OSError, ValueError, FileNotFoundError):
            # The source may have vanished between scandir and stat. A later
            # scan can retry; do not enqueue an unverified/symlinked target.
            excluded += 1
        if queued >= max_files:
            break
    return LegacyQueueReport(queued, previous, excluded, tuple(identifiers))


async def run_legacy_cutover_cycle(
    cfg, queue: StorageUploadJobs, transport, *, lease_db,
    bucket: str, allowed_root: Path, scratch_directory: Path,
    lease_name: str, lease_ttl: int, chunk_size: int,
    prefix: str = "", max_files: int = 100,
):
    """One fenced legacy scan + one durable job, without a legacy send path.

    The independently connected lease_db owns the legacy uploader's lease
    for the *entire* Telegram operation. A renewer cancels the active send
    if the lease can no longer be held. The durable job retains its journal
    for a later fenced recovery; no remotely accepted bytes are deleted.
    """
    if lease_ttl < 15:
        raise ValueError("Cutover uploader lease TTL must be at least 15 seconds")
    source_root = Path(cfg.source_dir).resolve(strict=True)
    permitted = allowed_root.resolve(strict=True)
    if not source_root.is_dir() or not source_root.is_relative_to(permitted):
        raise UploadJobError("Legacy source must be inside the approved job root")
    if queue.db.url != lease_db.url:
        raise UploadJobError("Legacy importer and job queue must share the same database")

    from uuid import uuid4
    holder = "legacy-cutover-" + uuid4().hex
    if not lease_db.acquire_lease(lease_name, holder, int(time.time()), lease_ttl):
        raise UploadJobError("Legacy uploader currently holds the upload lease")
    worker = None
    monitor = None
    try:
        def heartbeat():
            if not lease_db.renew_lease(
                lease_name, holder, int(time.time()), lease_ttl,
            ):
                raise UploadJobError("Legacy upload lease lost during cutover")

        report = enqueue_legacy_folder(
            cfg, queue, bucket=bucket, prefix=prefix,
            max_files=max_files, lease_heartbeat=heartbeat,
        )
        heartbeat()

        async def keep_lease():
            while True:
                await asyncio.sleep(max(2, lease_ttl // 4))
                try:
                    heartbeat()
                except BaseException:
                    if worker is not None and not worker.done():
                        worker.cancel()
                    raise

        monitor = asyncio.create_task(keep_lease())
        worker = asyncio.create_task(queue.run_next(
            transport, allowed_root=permitted,
            scratch_directory=scratch_directory, chunk_size=chunk_size,
        ))
        try:
            result = await worker
            # Detect an expired/fenced lease before returning success.
            heartbeat()
            if monitor.done():
                monitor.result()
            return report, result
        finally:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass
    finally:
        lease_db.release_lease(lease_name, holder)
