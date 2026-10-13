"""Portable SQLAlchemy object/chunk manifests.

An object is readable only after status becomes READY. Upload progress is
journaled per chunk to support cleanup/recovery after interrupted uploads.
No file payload bytes are persisted in the SQL database.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    BigInteger, CheckConstraint, Column, ForeignKey, Index, Integer, MetaData,
    String, Table, Text, UniqueConstraint, and_, func, select, update, insert, delete,
)

from teledrive.db import Database

metadata = MetaData()
objects = Table(
    "storage_objects", metadata,
    Column("id", String(36), primary_key=True),
    Column("bucket", String(63), nullable=False),
    Column("object_key", Text, nullable=False),
    Column("status", String(16), nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("sha256", String(64)),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
    Column("error", Text),
    CheckConstraint("size >= 0", name="ck_storage_objects_size"),
)
chunks = Table(
    "storage_chunks", metadata,
    Column("object_id", String(36), ForeignKey("storage_objects.id"), primary_key=True),
    Column("ordinal", Integer, primary_key=True),
    Column("offset", BigInteger, nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("account", String(255), nullable=False),
    Column("chat_id", BigInteger, nullable=False),
    Column("message_id", BigInteger, nullable=False),
    UniqueConstraint("account", "chat_id", "message_id", name="uq_storage_chunk_message"),
    CheckConstraint("size > 0", name="ck_storage_chunk_size"),
)
# Immutable server-side copies reference the original Telegram chunk journal.
# No duplicate (account, chat_id, message_id) rows are created. Physical GC
# MUST account for these aliases before deleting any original Telegram media.
object_aliases = Table(
    "storage_object_aliases", metadata,
    Column("object_id", String(36), ForeignKey("storage_objects.id"), primary_key=True),
    Column("source_object_id", String(36), ForeignKey("storage_objects.id"),
           nullable=False),
)

writer_fences = Table(
    "storage_writer_fences", metadata,
    Column("object_id", String(36), ForeignKey("storage_objects.id"), primary_key=True),
    Column("token", String(80), nullable=False),
    Column("lease_until", BigInteger, nullable=False),
    Column("heartbeat_at", BigInteger, nullable=False),
)

Index("ix_storage_objects_bucket", objects.c.bucket)
Index("ix_storage_objects_status", objects.c.status)


@dataclass(frozen=True)
class ChunkReference:
    ordinal: int
    offset: int
    size: int
    sha256: str
    account: str
    chat_id: int
    message_id: int


@dataclass(frozen=True)
class StoredObject:
    id: str
    bucket: str
    key: str
    size: int
    sha256: str
    created_at: int


class ManifestError(RuntimeError):
    pass


class ManifestStore:
    """One SQLAlchemy connection per worker; not safe to share across threads.

    The caller owns the Database instance and its lifetime. Chunk writes are
    committed separately, before completion, forming a recovery journal.
    """

    def __init__(self, database: Database):
        self.db = database
        metadata.create_all(database.engine)

    def begin(self, bucket: str, key: str, size: int, now: int) -> str:
        if not bucket or len(bucket) > 63 or not key or size < 0:
            raise ValueError("Invalid bucket/key/size")
        identifier = str(uuid4())
        self.db.connection.execute(insert(objects).values(
            id=identifier, bucket=bucket, object_key=key, status="pending",
            size=size, sha256=None, created_at=now, updated_at=now,
        ))
        self.db.commit()
        return identifier

    def start_writer(self, identifier: str, token: str, now: int, ttl: int = 120) -> None:
        """Bind an unguessable owner to a pending object before Telegram I/O."""
        if not token or len(token) > 80 or ttl < 5:
            raise ValueError("Invalid writer token or TTL")
        self.db.connection.execute(insert(writer_fences).values(
            object_id=identifier, token=token, lease_until=now + ttl,
            heartbeat_at=now,
        ))
        self.db.commit()

    def heartbeat_writer(self, identifier: str, token: str, now: int, ttl: int = 120) -> bool:
        """Conditional SQL update is the fencing point across worker processes."""
        if ttl < 5:
            raise ValueError("Invalid TTL")
        changed = self.db.connection.execute(
            update(writer_fences).where(
                writer_fences.c.object_id == identifier,
                writer_fences.c.token == token,
                writer_fences.c.lease_until >= now,
            ).values(lease_until=now + ttl, heartbeat_at=now)
        )
        self.db.commit()
        return changed.rowcount == 1

    def _assert_writer(self, identifier: str, token: str | None, now: int) -> None:
        row = self.db.connection.execute(
            select(writer_fences.c.token, writer_fences.c.lease_until)
            .where(writer_fences.c.object_id == identifier)
        ).first()
        if row is None:
            if token is not None:
                raise ManifestError("Writer lease disappeared")
            return
        if token != row[0] or int(row[1]) < now:
            raise ManifestError("Writer lease lost or expired")

    def abandon_stale_pending(self, *, now: int, min_age_seconds: int = 3600,
                              limit: int = 100) -> list[str]:
        """Fence expired writers and mark aged pending objects failed.

        Never touch unleased/legacy pending rows or a live writer. The expired
        fence is consumed under the same SQL transaction as the status update.
        The failed object's Telegram journal is kept for subsequent dry-run GC.
        """
        if min_age_seconds < 60 or not (1 <= limit <= 1000):
            raise ValueError("Invalid abandonment policy")
        rows = self.db.connection.execute(
            select(objects.c.id)
            .join(writer_fences, objects.c.id == writer_fences.c.object_id)
            .where(
                objects.c.status == "pending",
                objects.c.updated_at <= now - min_age_seconds,
                writer_fences.c.lease_until < now,
            ).order_by(objects.c.created_at, objects.c.id).limit(limit)
        ).all()
        abandoned = []
        for row in rows:
            identifier = row[0]
            # Atomically fence the old owner first. A concurrent heartbeat
            # cannot succeed after this conditional compare-and-update.
            affected = self.db.connection.execute(
                update(writer_fences).where(
                    writer_fences.c.object_id == identifier,
                    writer_fences.c.lease_until < now,
                ).values(token="abandoned:" + identifier, lease_until=now)
            )
            if affected.rowcount != 1:
                continue
            changed = self.db.connection.execute(
                update(objects).where(
                    objects.c.id == identifier, objects.c.status == "pending",
                    objects.c.updated_at <= now - min_age_seconds,
                ).values(status="failed", error="Writer lease expired; needs reconciliation",
                         updated_at=now)
            )
            if changed.rowcount == 1:
                abandoned.append(identifier)
        self.db.commit()
        return abandoned

    def append(self, identifier: str, part: ChunkReference, now: int) -> None:
        row = self.db.connection.execute(
            select(objects.c.status, objects.c.size).where(objects.c.id == identifier)
        ).first()
        if row is None or row[0] != "pending":
            raise ManifestError("Object is not pending")
        if part.ordinal < 0 or part.offset < 0 or part.size <= 0:
            raise ManifestError("Invalid chunk coordinates")
        if len(part.sha256) != 64 or not all(c in "0123456789abcdef" for c in part.sha256):
            raise ManifestError("Invalid SHA-256 digest")
        if not part.account or part.message_id <= 0 or part.chat_id == 0:
            raise ManifestError("Invalid Telegram chunk reference")
        count, position = self.db.connection.execute(
            select(func.count(), func.coalesce(func.sum(chunks.c.size), 0))
            .where(chunks.c.object_id == identifier)
        ).one()
        if part.ordinal != count or part.offset != position or part.offset + part.size > row[1]:
            raise ManifestError("Chunk order, offset, or size is inconsistent")
        self.db.connection.execute(insert(chunks).values(
            object_id=identifier, ordinal=part.ordinal, offset=part.offset,
            size=part.size, sha256=part.sha256, account=part.account,
            chat_id=part.chat_id, message_id=part.message_id,
        ))
        self.db.connection.execute(
            update(objects).where(objects.c.id == identifier).values(updated_at=now)
        )
        self.db.commit()

    def finish(self, identifier: str, sha256: str, now: int, *, writer_token: str | None = None) -> StoredObject:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ManifestError("Invalid object digest")
        row = self.db.connection.execute(
            select(objects).where(objects.c.id == identifier)
        ).mappings().first()
        if row is None or row["status"] != "pending":
            raise ManifestError("Object is not pending")
        self._assert_writer(identifier, writer_token, now)
        size, count = self.db.connection.execute(
            select(func.coalesce(func.sum(chunks.c.size), 0), func.count())
            .where(chunks.c.object_id == identifier)
        ).one()
        if size != row["size"] or (count == 0 and size != 0):
            raise ManifestError("Cannot finalize incomplete object")
        self.db.connection.execute(update(objects).where(
            objects.c.id == identifier, objects.c.status == "pending"
        ).values(status="ready", sha256=sha256, updated_at=now, error=None))
        if writer_token is not None:
            self.db.connection.execute(delete(writer_fences).where(
                writer_fences.c.object_id == identifier,
                writer_fences.c.token == writer_token,
            ))
        self.db.commit()
        return self.get(identifier)

    def fail(self, identifier: str, error: str, now: int, *, writer_token: str | None = None) -> None:
        self._assert_writer(identifier, writer_token, now)
        self.db.connection.execute(
            update(objects).where(objects.c.id == identifier, objects.c.status == "pending")
            .values(status="failed", error=error[:500], updated_at=now)
        )
        if writer_token is not None:
            self.db.connection.execute(delete(writer_fences).where(
                writer_fences.c.object_id == identifier,
                writer_fences.c.token == writer_token,
            ))
        self.db.commit()

    def get(self, identifier: str) -> StoredObject:
        row = self.db.connection.execute(select(objects).where(
            objects.c.id == identifier, objects.c.status == "ready"
        )).mappings().first()
        if row is None:
            raise FileNotFoundError("Object not found or not committed")
        return StoredObject(id=row["id"], bucket=row["bucket"], key=row["object_key"],
                            size=int(row["size"]), sha256=row["sha256"],
                            created_at=int(row["created_at"]))

    def resolve_content_id(self, identifier: str) -> str:
        """Locate immutable Telegram bytes for a ready object or server-side copy.

        Depth is bounded and cycles fail closed if metadata is corrupt.
        This read-only lookup also validates source readiness at each hop.
        """
        visited: set[str] = set()
        current = identifier
        for _ in range(64):
            if current in visited:
                raise ManifestError("Cyclic copy alias metadata")
            visited.add(current)
            self.get(current)
            source = self.db.connection.execute(select(
                object_aliases.c.source_object_id
            ).where(object_aliases.c.object_id == current)).scalar_one_or_none()
            if source is None:
                return current
            current = source
        raise ManifestError("Copy alias chain is too deep")

    def parts(self, identifier: str) -> list[ChunkReference]:
        current = self.resolve_content_id(identifier)
        rows = self.db.connection.execute(select(chunks).where(
            chunks.c.object_id == current
        ).order_by(chunks.c.ordinal)).mappings().all()
        return [ChunkReference(ordinal=int(r["ordinal"]), offset=int(r["offset"]),
                               size=int(r["size"]), sha256=r["sha256"], account=r["account"],
                               chat_id=int(r["chat_id"]), message_id=int(r["message_id"]))
                for r in rows]

    def interrupted(self) -> list[tuple[str, str]]:
        """List non-ready objects requiring reconciliation; never show to clients."""
        rows = self.db.connection.execute(
            select(objects.c.id, objects.c.status).where(objects.c.status.in_(["pending", "failed"]))
        ).all()
        return [(str(row[0]), str(row[1])) for row in rows]

    def recovery_parts(self, identifier: str) -> list[ChunkReference]:
        """Administrator-only journal access; pending/failed objects are not readable."""
        rows = self.db.connection.execute(select(chunks).where(
            chunks.c.object_id == identifier
        ).order_by(chunks.c.ordinal)).mappings().all()
        return [ChunkReference(ordinal=int(r["ordinal"]), offset=int(r["offset"]),
                               size=int(r["size"]), sha256=r["sha256"], account=r["account"],
                               chat_id=int(r["chat_id"]), message_id=int(r["message_id"]))
                for r in rows]
