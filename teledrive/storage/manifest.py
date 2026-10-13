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
    String, Table, Text, UniqueConstraint, func, select, update, insert,
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

    def finish(self, identifier: str, sha256: str, now: int) -> StoredObject:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ManifestError("Invalid object digest")
        row = self.db.connection.execute(
            select(objects).where(objects.c.id == identifier)
        ).mappings().first()
        if row is None or row["status"] != "pending":
            raise ManifestError("Object is not pending")
        size, count = self.db.connection.execute(
            select(func.coalesce(func.sum(chunks.c.size), 0), func.count())
            .where(chunks.c.object_id == identifier)
        ).one()
        if size != row["size"] or (count == 0 and size != 0):
            raise ManifestError("Cannot finalize incomplete object")
        self.db.connection.execute(update(objects).where(
            objects.c.id == identifier, objects.c.status == "pending"
        ).values(status="ready", sha256=sha256, updated_at=now, error=None))
        self.db.commit()
        return self.get(identifier)

    def fail(self, identifier: str, error: str, now: int) -> None:
        self.db.connection.execute(
            update(objects).where(objects.c.id == identifier, objects.c.status == "pending")
            .values(status="failed", error=error[:500], updated_at=now)
        )
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

    def parts(self, identifier: str) -> list[ChunkReference]:
        self.get(identifier)
        rows = self.db.connection.execute(select(chunks).where(
            chunks.c.object_id == identifier
        ).order_by(chunks.c.ordinal)).mappings().all()
        return [ChunkReference(ordinal=int(r["ordinal"]), offset=int(r["offset"]),
                               size=int(r["size"]), sha256=r["sha256"], account=r["account"],
                               chat_id=int(r["chat_id"]), message_id=int(r["message_id"]))
                for r in rows]

    def interrupted(self) -> list[tuple[str, str]]:
        """List non-ready objects requiring reconciliation; never show to clients."""
        rows = self.db.connection.execute(
            select(objects.c.id, objects.c.status).where(objects.c.status != "ready")
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
