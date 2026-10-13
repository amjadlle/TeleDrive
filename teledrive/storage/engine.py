"""Telegram-backed immutable object write/read engine.

Each successful write stores all bytes in Telegram and atomically publishes a
SQL manifest. Staging is temporary; it is NEVER a durable S3 primary backend.
A failed/interrupted upload remains invisible until explicitly reconciled.

This module intentionally does not implement S3's HTTP semantics yet.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import AsyncIterator, Protocol

from .manifest import ChunkReference, ManifestStore, StoredObject, ManifestError


class IntegrityError(RuntimeError):
    """Source or Telegram chunk is missing, truncated or checksum-mismatched."""


class TelegramTransport(Protocol):
    async def upload(self, path: Path, *, object_id: str, ordinal: int,
                     checksum: str) -> tuple[str, int, int]:
        """Upload bytes and return (account_name, Telegram chat_id, message_id)."""
        ...

    async def download(self, reference: ChunkReference, destination: Path) -> None:
        """Download exactly one Telegram message body to a local regular file."""
        ...


def _regular_source(path: Path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    handle = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        handle.close()
        raise ValueError("Source must be a regular file")
    return handle


def _digest_file(path: Path) -> tuple[int, str]:
    sha = hashlib.sha256()
    total = 0
    with _regular_source(path) as f:
        while block := f.read(1024 * 1024):
            sha.update(block)
            total += len(block)
    return total, sha.hexdigest()


class TelegramObjectStorage:
    def __init__(self, manifests: ManifestStore, transport: TelegramTransport,
                 *, chunk_size: int = 64 * 1024 * 1024,
                 scratch_directory: Path | None = None,
                 writer_ttl_seconds: int = 120):
        if chunk_size <= 0 or chunk_size > 1024 * 1024 * 1024:
            raise ValueError("chunk_size must be between 1 byte and 1 GiB")
        self.manifests = manifests
        self.transport = transport
        self.chunk_size = chunk_size
        self.scratch_directory = scratch_directory
        if writer_ttl_seconds < 5:
            raise ValueError("writer_ttl_seconds must be at least 5")
        self.writer_ttl_seconds = writer_ttl_seconds

    async def _keep_writer_alive(self, identifier: str, token: str,
                                 stop: asyncio.Event, lost: asyncio.Event) -> None:
        interval = max(1, self.writer_ttl_seconds // 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                if not self.manifests.heartbeat_writer(
                    identifier, token, int(time.time()), self.writer_ttl_seconds
                ):
                    lost.set()
                    return
            except Exception:
                lost.set()
                return

    async def put_file(self, bucket: str, key: str, source: Path) -> StoredObject:
        source = Path(source)
        with _regular_source(source) as handle:
            before = os.fstat(handle.fileno())
            identifier = self.manifests.begin(bucket, key, before.st_size, int(time.time()))
            token = secrets.token_hex(24)
            self.manifests.start_writer(identifier, token, int(time.time()), self.writer_ttl_seconds)
            stop = asyncio.Event()
            lost = asyncio.Event()
            renewal = asyncio.create_task(self._keep_writer_alive(identifier, token, stop, lost))
            full_sha = hashlib.sha256()
            offset = 0
            ordinal = 0
            try:
                with tempfile.TemporaryDirectory(
                    prefix="teledrive-chunks-", dir=self.scratch_directory
                ) as scratch:
                    root = Path(scratch)
                    while data := handle.read(self.chunk_size):
                        full_sha.update(data)
                        digest = hashlib.sha256(data).hexdigest()
                        part = root / f"part-{ordinal:08d}"
                        with part.open("xb") as f:
                            f.write(data)
                        part.chmod(0o600)
                        account, chat_id, message_id = await self.transport.upload(
                            part, object_id=identifier, ordinal=ordinal, checksum=digest,
                        )
                        # The remote message may already exist if our SQL lease
                        # expired: reject publication and leave the marker for GC.
                        if lost.is_set() or not self.manifests.heartbeat_writer(
                            identifier, token, int(time.time()), self.writer_ttl_seconds
                        ):
                            raise ManifestError("Writer ownership lost before chunk journal")
                        self.manifests.append(identifier, ChunkReference(
                            ordinal=ordinal, offset=offset, size=len(data), sha256=digest,
                            account=account, chat_id=int(chat_id), message_id=int(message_id),
                        ), int(time.time()))
                        part.unlink()
                        offset += len(data)
                        ordinal += 1
                after = os.fstat(handle.fileno())
                if (offset != before.st_size or before.st_dev != after.st_dev
                    or before.st_ino != after.st_ino
                    or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_size != after.st_size):
                    raise IntegrityError("Source changed during upload")
                if lost.is_set() or not self.manifests.heartbeat_writer(
                    identifier, token, int(time.time()), self.writer_ttl_seconds
                ):
                    raise ManifestError("Writer ownership lost before publication")
                return self.manifests.finish(
                    identifier, full_sha.hexdigest(), int(time.time()), writer_token=token
                )
            except BaseException as exc:
                # Do not delete recorded Telegram chunks here. They form a
                # recovery journal and may be needed by a later reconciler.
                try:
                    self.manifests.fail(identifier, type(exc).__name__, int(time.time()),
                                        writer_token=token)
                except Exception:
                    pass
                raise
            finally:
                stop.set()
                await renewal

    async def iter_bytes(self, identifier: str, *,
                         start: int = 0, end: int | None = None,
                         block_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
        """Stream a half-open [start,end) range, verifying each downloaded chunk."""
        obj = self.manifests.get(identifier)
        if end is None:
            end = obj.size
        if block_size <= 0 or start < 0 or end < start or end > obj.size:
            raise ValueError("Invalid range or block size")
        if start == end:
            return
        parts = self.manifests.parts(identifier)
        expected = 0
        with tempfile.TemporaryDirectory(
            prefix="teledrive-read-", dir=self.scratch_directory
        ) as scratch:
            root = Path(scratch)
            for part in parts:
                if part.offset != expected:
                    raise IntegrityError("Non-contiguous chunk manifest")
                expected += part.size
                if part.offset >= end:
                    break
                if part.offset + part.size <= start:
                    continue
                target = root / f"read-{part.ordinal:08d}"
                await self.transport.download(part, target)
                if not target.is_file() or target.is_symlink():
                    raise IntegrityError("Telegram chunk missing or unsafe")
                size, sha = _digest_file(target)
                if size != part.size or sha != part.sha256:
                    raise IntegrityError("Telegram chunk checksum mismatch")
                local_start = max(0, start - part.offset)
                local_end = min(part.size, end - part.offset)
                with _regular_source(target) as file:
                    file.seek(local_start)
                    remaining = local_end - local_start
                    while remaining:
                        block = file.read(min(remaining, block_size))
                        if not block:
                            raise IntegrityError("Telegram chunk truncated while streaming")
                        remaining -= len(block)
                        yield block
                target.unlink()
            # Reaching end is only valid if manifests cover all requested bytes.
            if expected < end:
                raise IntegrityError("Missing part of requested range")

    async def download_to_path(self, identifier: str, destination: Path) -> StoredObject:
        """Atomically publish a verified full object; never leave a partial file."""
        obj = self.manifests.get(identifier)
        destination = Path(destination)
        if destination.is_symlink():
            raise ValueError("Destination must not be a symlink")
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".teledrive-", dir=destination.parent)
        temp = Path(name)
        total = 0
        sha = hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as output:
                async for block in self.iter_bytes(identifier):
                    output.write(block)
                    sha.update(block)
                    total += len(block)
                output.flush()
                os.fsync(output.fileno())
            if total != obj.size or sha.hexdigest() != obj.sha256:
                raise IntegrityError("Object checksum mismatch")
            os.chmod(temp, 0o600)
            os.replace(temp, destination)
            return obj
        finally:
            temp.unlink(missing_ok=True)
