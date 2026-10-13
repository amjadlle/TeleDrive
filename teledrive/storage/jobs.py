"""Durable, fenced upload jobs for operator-approved local sources.

The job table is metadata-only: payload bytes stay in the permitted local
source until uploaded to Telegram. An interrupted job may retry from the
beginning, leaving any previous immutable Telegram journals for safe GC.
A staged READY manifest can be published without uploading bytes again.
"""
from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy import (
    BigInteger, Column, Integer, MetaData, String, Table, Text,
    and_, or_, insert, select, update,
)
from sqlalchemy.exc import IntegrityError

from .catalog import BucketCatalog
from .engine import TelegramObjectStorage

_meta = MetaData()
jobs = Table(
    "storage_upload_jobs", _meta,
    Column("id", String(36), primary_key=True),
    Column("bucket", String(63), nullable=False),
    Column("object_key", Text, nullable=False),
    Column("source_path", Text, nullable=False),
    Column("source_size", BigInteger, nullable=False),
    Column("source_device", BigInteger, nullable=False),
    Column("source_inode", BigInteger, nullable=False),
    Column("source_mtime_ns", BigInteger, nullable=False),
    Column("expected_generation", BigInteger, nullable=False),
    Column("expected_head_id", String(36)),
    Column("state", String(16), nullable=False),
    Column("object_id", String(36)),
    Column("attempts", Integer, nullable=False),
    Column("lease_token", String(80)),
    Column("lease_until", BigInteger),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    Column("last_error", String(500)),
)


class UploadJobError(ValueError):
    pass


def _source_identity(path: Path, allowed_root: Path) -> tuple[Path, os.stat_result]:
    """Root-bound regular-file identity: never follow an untrusted final symlink."""
    root = allowed_root.resolve(strict=True)
    if not root.is_dir():
        raise UploadJobError("Upload source root must be a directory")
    if path.is_symlink():
        raise UploadJobError("Symlink upload sources are forbidden")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root) or resolved == root:
        raise UploadJobError("Upload source is outside the configured root")
    fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        stat_info = os.fstat(fd)
        if not stat.S_ISREG(stat_info.st_mode):
            raise UploadJobError("Upload source must be a regular file")
        return resolved, stat_info
    finally:
        os.close(fd)


class StorageUploadJobs:
    """One SQL connection per worker. Only lease owner may publish a job."""

    def __init__(self, catalog: BucketCatalog):
        self.catalog = catalog
        self.db = catalog.db
        _meta.create_all(self.db.engine)

    def enqueue(self, bucket: str, key: str, source: Path, *,
                allowed_root: Path, now: int | None = None) -> str:
        now = int(time.time()) if now is None else now
        if bucket not in self.catalog.list_buckets():
            raise FileNotFoundError("Bucket does not exist")
        self.catalog._keyhash(key)
        path, info = _source_identity(Path(source), allowed_root)
        try:
            current = self.catalog.get(bucket, key)
            generation, observed = current.generation, current.item.id
        except FileNotFoundError:
            generation, observed = 0, None
        ident = str(uuid4())
        self.db.connection.execute(insert(jobs).values(
            id=ident, bucket=bucket, object_key=key,
            source_path=str(path), source_size=info.st_size,
            source_device=info.st_dev, source_inode=info.st_ino,
            source_mtime_ns=info.st_mtime_ns,
            expected_generation=generation, expected_head_id=observed,
            state="queued", object_id=None, attempts=0,
            lease_token=None, lease_until=None,
            created_at=now, updated_at=now, last_error=None,
        ))
        self.db.commit()
        return ident

    def list_jobs(self, *, limit: int = 100) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid job list limit")
        rows = self.db.connection.execute(
            select(jobs).order_by(jobs.c.created_at.desc(), jobs.c.id)
            .limit(limit)
        ).mappings().all()
        # Tokens are credentials for in-flight writes; never disclose them.
        return [{key: value for key, value in row.items()
                 if key not in {"lease_token"}} for row in rows]

    def claim_next(self, *, now: int | None = None, ttl: int = 120) -> dict | None:
        if ttl < 5:
            raise ValueError("Job lease TTL must be >= 5")
        now = int(time.time()) if now is None else now
        eligible = or_(
            jobs.c.state == "queued",
            and_(jobs.c.state.in_(("running", "staged", "publishing")),
                 jobs.c.lease_until < now),
        )
        candidates = self.db.connection.execute(
            select(jobs.c.id)
            .where(eligible).order_by(jobs.c.created_at, jobs.c.id).limit(10)
        ).scalars().all()
        for ident in candidates:
            token = uuid4().hex
            result = self.db.connection.execute(
                update(jobs).where(jobs.c.id == ident, eligible)
                .values(state="running", lease_token=token,
                        lease_until=now + ttl, updated_at=now,
                        attempts=jobs.c.attempts + 1)
            )
            self.db.commit()
            if result.rowcount == 1:
                row = self.db.connection.execute(
                    select(jobs).where(jobs.c.id == ident)
                ).mappings().one()
                return dict(row)
        return None

    def _owned(self, ident: str, token: str, now: int):
        return and_(jobs.c.id == ident, jobs.c.lease_token == token,
                    jobs.c.state.in_(("running", "staged", "publishing")),
                    jobs.c.lease_until >= now)

    def renew(self, ident: str, token: str, *, now: int, ttl: int = 120) -> bool:
        result = self.db.connection.execute(
            update(jobs).where(self._owned(ident, token, now))
            .values(lease_until=now + ttl, updated_at=now)
        )
        self.db.commit()
        return result.rowcount == 1

    def mark_staged(self, ident: str, token: str, object_id: str, *, now: int) -> bool:
        obj = self.catalog.manifests.get(object_id)
        job = self.db.connection.execute(
            select(jobs.c.bucket, jobs.c.object_key)
            .where(jobs.c.id == ident)
        ).one_or_none()
        if job is None or job[0] != obj.bucket or job[1] != obj.key:
            raise UploadJobError("Staged object has mismatched bucket/key")
        result = self.db.connection.execute(
            update(jobs).where(self._owned(ident, token, now))
            .values(object_id=object_id, state="staged", updated_at=now)
        )
        self.db.commit()
        return result.rowcount == 1

    def finish(self, ident: str, token: str, *, now: int) -> bool:
        row = self.db.connection.execute(
            select(jobs).where(self._owned(ident, token, now))
        ).mappings().first()
        if not row or not row["object_id"]:
            self.db.connection.rollback()
            return False
        candidate = row["object_id"]
        try:
            current = self.catalog.get(row["bucket"], row["object_key"]).item.id
        except FileNotFoundError:
            current = None
        if current != candidate:
            fenced = self.db.connection.execute(
                update(jobs).where(self._owned(ident, token, now))
                .values(state="publishing", updated_at=now)
            )
            if fenced.rowcount != 1:
                self.db.connection.rollback()
                return False
            try:
                # This commit also persists state=publishing; a restarted worker
                # recognizes the published HEAD without uploading again.
                self.catalog.publish(
                    candidate, now=now,
                    expected_generation=int(row["expected_generation"]),
                    expected_object_id=row["expected_head_id"],
                )
            except BaseException:
                self.db.connection.rollback()
                raise
        finished = self.db.connection.execute(
            update(jobs).where(self._owned(ident, token, now))
            .values(state="done", lease_token=None, lease_until=None,
                    updated_at=now, last_error=None)
        )
        self.db.commit()
        return finished.rowcount == 1

    def fail(self, ident: str, token: str, error: str, *, now: int) -> bool:
        result = self.db.connection.execute(
            update(jobs).where(self._owned(ident, token, now))
            .values(state="failed", lease_token=None, lease_until=None,
                    last_error=error[:500], updated_at=now)
        )
        self.db.commit()
        return result.rowcount == 1

    def cancel(self, ident: str) -> bool:
        """Cancel only inactive jobs; never interrupt a live Telegram writer.

        A staged/expired job must first be claimed and reconciled by the
        regular worker. Cancellation cannot discard its durable manifest.
        No Telegram message is deleted by this operation.
        """
        result = self.db.connection.execute(
            update(jobs).where(
                jobs.c.id == ident,
                jobs.c.state.in_(("queued", "failed")),
            ).values(
                state="cancelled", lease_token=None, lease_until=None,
                updated_at=int(time.time()),
            )
        )
        self.db.commit()
        return result.rowcount == 1

    def retry(self, ident: str) -> bool:
        result = self.db.connection.execute(
            update(jobs).where(jobs.c.id == ident, jobs.c.state == "failed")
            .values(state="queued", last_error=None, lease_token=None,
                    lease_until=None)
        )
        self.db.commit()
        return result.rowcount == 1

    async def _heartbeat(self, ident: str, token: str, ttl: int,
                         stop: asyncio.Event, lost: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(1, ttl // 3))
                return
            except asyncio.TimeoutError:
                pass
            try:
                if not self.renew(ident, token, now=int(time.time()), ttl=ttl):
                    lost.set()
                    return
            except Exception:
                self.db.connection.rollback()
                lost.set()
                return

    async def run_next(self, transport, *, allowed_root: Path,
                       scratch_directory: Path | None = None,
                       chunk_size: int = 16 * 1024 * 1024,
                       lease_seconds: int = 120) -> dict | None:
        """Process one queued or abandoned job; never physically delete media."""
        claim = self.claim_next(ttl=lease_seconds)
        if claim is None:
            return None
        ident, token = claim["id"], claim["lease_token"]
        stop, lost = asyncio.Event(), asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(ident, token, lease_seconds, stop, lost)
        )
        try:
            object_id = claim["object_id"]
            if object_id is None:
                path, stat_info = _source_identity(
                    Path(claim["source_path"]), allowed_root
                )
                expected = (
                    claim["source_device"], claim["source_inode"],
                    claim["source_size"], claim["source_mtime_ns"]
                )
                observed = (
                    stat_info.st_dev, stat_info.st_ino,
                    stat_info.st_size, stat_info.st_mtime_ns
                )
                if observed != expected:
                    raise UploadJobError("Source changed since job was queued")
                verify = getattr(transport, "verify_target", None)
                if callable(verify):
                    await verify()
                engine = TelegramObjectStorage(
                    self.catalog.manifests, transport,
                    chunk_size=chunk_size, scratch_directory=scratch_directory,
                )
                obj = await engine.put_file(claim["bucket"], claim["object_key"], path)
                # Do not publish a fingerprint mismatch even if Telegram has
                # already accepted chunks; they remain for future safe audit.
                _, after = _source_identity(path, allowed_root)
                latest = (
                    after.st_dev, after.st_ino,
                    after.st_size, after.st_mtime_ns
                )
                if latest != expected:
                    raise UploadJobError("Source changed before job publication")
                object_id = obj.id
                if lost.is_set() or not self.mark_staged(
                    ident, token, object_id, now=int(time.time())
                ):
                    raise UploadJobError("Job lease lost before staging")
            stop.set()
            await heartbeat
            if lost.is_set() or not self.finish(
                ident, token, now=int(time.time())
            ):
                raise UploadJobError("Job lease lost before publication")
            return {"id": ident, "state": "done", "object_id": object_id}
        except BaseException as exc:
            self.db.connection.rollback()
            self.fail(ident, token, type(exc).__name__, now=int(time.time()))
            raise
        finally:
            stop.set()
            await heartbeat
