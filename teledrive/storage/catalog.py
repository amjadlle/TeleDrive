"""Bucket and current-object catalog shared by future REST and S3 gateways.

Telegram is the only payload backend. This module stores names, pointers and
immutable object IDs, never object body bytes. Previous object manifests
remain available after replacement/removal until explicit safe GC.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from sqlalchemy import (
    BigInteger, Column, ForeignKey, MetaData, String, Table,
    and_, delete, insert, select, update,
)
from sqlalchemy.exc import IntegrityError

from .manifest import ManifestStore, StoredObject, objects

catalog_metadata = MetaData()
buckets = Table(
    "storage_buckets", catalog_metadata,
    Column("name", String(63), primary_key=True),
    Column("created_at", BigInteger, nullable=False),
)
heads = Table(
    "storage_object_heads", catalog_metadata,
    Column("bucket", String(63), ForeignKey("storage_buckets.name"), primary_key=True),
    Column("key_hash", String(64), primary_key=True),
    Column("object_id", String(36), nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
)
_BUCKET_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])?\Z")


@dataclass(frozen=True)
class ObjectHead:
    item: StoredObject
    generation: int


class BucketCatalog:
    """Manage bucket names and a single current pointer per logical object key.

    The underlying manifest remains immutable. A newer version simply moves
    the head pointer atomically; old Telegram chunks are not deleted.
    """

    def __init__(self, manifests: ManifestStore):
        self.manifests = manifests
        self.db = manifests.db
        catalog_metadata.create_all(self.db.engine)

    @staticmethod
    def validate_bucket(name: str) -> None:
        if (not isinstance(name, str) or len(name) < 3 or len(name) > 63
                or not _BUCKET_RE.fullmatch(name) or ".." in name
                or ".-" in name or "-." in name
                or all(part.isdigit() for part in name.split("."))):
            raise ValueError("Invalid S3-compatible bucket name")

    @staticmethod
    def _keyhash(key: str) -> str:
        if not isinstance(key, str) or not (1 <= len(key.encode("utf-8")) <= 1024):
            raise ValueError("Object key must contain 1..1024 UTF-8 bytes")
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def create_bucket(self, name: str, now: int) -> None:
        self.validate_bucket(name)
        try:
            with self.db.connection.begin_nested():
                self.db.connection.execute(insert(buckets).values(name=name, created_at=now))
        except IntegrityError as exc:
            raise ValueError("Bucket already exists") from exc
        self.db.commit()

    def list_buckets(self) -> list[str]:
        return list(self.db.connection.execute(select(buckets.c.name).order_by(buckets.c.name)).scalars())

    def get(self, bucket: str, key: str) -> ObjectHead:
        digest = self._keyhash(key)
        row = self.db.connection.execute(
            select(heads.c.object_id, heads.c.generation)
            .where(heads.c.bucket == bucket, heads.c.key_hash == digest)
        ).first()
        if row is None:
            raise FileNotFoundError("Object key does not exist")
        item = self.manifests.get(row[0])
        if item.bucket != bucket or item.key != key:
            raise RuntimeError("Object key digest collision; refusing unsafe resolution")
        return ObjectHead(item, int(row[1]))

    def publish(self, identifier: str, *, now: int,
                expected_generation: int | None = None) -> ObjectHead:
        """Atomically update the current pointer after Telegram upload succeeds.

        expected_generation=0 is create-if-absent, and a positive value is
        optimistic concurrency control. None permits last-writer-wins.
        """
        item = self.manifests.get(identifier)
        digest = self._keyhash(item.key)
        if item.bucket not in self.list_buckets():
            raise ValueError("Bucket does not exist")
        existing = self.db.connection.execute(
            select(heads.c.object_id, heads.c.generation)
            .where(heads.c.bucket == item.bucket, heads.c.key_hash == digest)
        ).first()
        if existing is None:
            if expected_generation not in (None, 0):
                raise ValueError("Object changed; generation precondition failed")
            try:
                with self.db.connection.begin_nested():
                    self.db.connection.execute(insert(heads).values(
                        bucket=item.bucket, key_hash=digest, object_id=item.id,
                        generation=1, updated_at=now,
                    ))
            except IntegrityError as exc:
                raise ValueError("Concurrent object creation; retry with generation") from exc
            self.db.commit()
            return ObjectHead(item, 1)

        current = self.manifests.get(existing[0])
        if current.bucket != item.bucket or current.key != item.key:
            raise RuntimeError("Object hash collision detected")
        generation = int(existing[1])
        if expected_generation is not None and expected_generation != generation:
            raise ValueError("Object changed; generation precondition failed")
        changed = self.db.connection.execute(
            update(heads)
            .where(
                heads.c.bucket == item.bucket, heads.c.key_hash == digest,
                heads.c.generation == generation,
            )
            .values(object_id=item.id, generation=generation + 1, updated_at=now)
        )
        if changed.rowcount != 1:
            self.db.connection.rollback()
            raise ValueError("Concurrent object modification")
        self.db.commit()
        return ObjectHead(item, generation + 1)

    def list_objects(self, bucket: str, *, prefix: str = "",
                     after: str | None = None, limit: int = 100) -> list[ObjectHead]:
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid page limit")
        stmt = (
            select(heads.c.generation, objects)
            .select_from(heads.join(objects, heads.c.object_id == objects.c.id))
            .where(heads.c.bucket == bucket, objects.c.status == "ready")
        )
        if prefix:
            stmt = stmt.where(objects.c.object_key.startswith(prefix, autoescape=True))
        if after is not None:
            stmt = stmt.where(objects.c.object_key > after)
        rows = self.db.connection.execute(
            stmt.order_by(objects.c.object_key, objects.c.id).limit(limit)
        ).mappings().all()
        return [ObjectHead(
            StoredObject(
                id=row["id"], bucket=row["bucket"], key=row["object_key"],
                size=int(row["size"]), sha256=row["sha256"],
                created_at=int(row["created_at"]),
            ), int(row["generation"]),
        ) for row in rows]

    def remove_current(self, bucket: str, key: str, *,
                       expected_generation: int | None = None) -> bool:
        """Unlink a head only; Telegram data stays until separately verified GC."""
        digest = self._keyhash(key)
        try:
            current = self.get(bucket, key)
        except FileNotFoundError:
            return False
        if expected_generation is not None and current.generation != expected_generation:
            raise ValueError("Object changed; generation precondition failed")
        condition = [
            heads.c.bucket == bucket, heads.c.key_hash == digest,
            heads.c.object_id == current.item.id,
            heads.c.generation == current.generation,
        ]
        changed = self.db.connection.execute(delete(heads).where(and_(*condition)))
        self.db.commit()
        return changed.rowcount == 1

    def delete_bucket(self, name: str) -> None:
        """Refuse buckets with any current or historical manifests."""
        existing = self.db.connection.execute(
            select(buckets.c.name).where(buckets.c.name == name)
        ).first()
        if existing is None:
            raise FileNotFoundError("Bucket does not exist")
        history = self.db.connection.execute(
            select(objects.c.id).where(objects.c.bucket == name).limit(1)
        ).first()
        if history is not None:
            raise ValueError("Bucket has object history; safe physical GC required")
        self.db.connection.execute(delete(buckets).where(buckets.c.name == name))
        self.db.commit()
