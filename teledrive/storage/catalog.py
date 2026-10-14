"""Bucket and current-object catalog shared by future REST and S3 gateways.

Telegram is the only payload backend. This module stores names, pointers and
immutable object IDs, never object body bytes. Previous object manifests
remain available after replacement/removal until explicit safe GC.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import (
    BigInteger, Column, ForeignKey, MetaData, String, Table, Index,
    and_, or_, delete, insert, select, update, inspect, text,
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
published_versions = Table(
    "storage_published_versions", catalog_metadata,
    Column("object_id", String(36), primary_key=True),
    Column("bucket", String(63), nullable=False),
    Column("key_hash", String(64), nullable=False),
    Column("committed_ns", BigInteger, nullable=False),
)
# Logical S3 VersionId deletion revokes visibility but NEVER removes Telegram
# media, manifests or aliases. Existing deployments receive an additive table.
removed_versions = Table(
    "storage_removed_versions", catalog_metadata,
    Column("object_id", String(36), primary_key=True),
    Column("removed_ns", BigInteger, nullable=False),
)
Index("ix_storage_published_versions_key",
      published_versions.c.bucket, published_versions.c.key_hash,
      published_versions.c.committed_ns)

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

    def list_bucket_records(self, *, prefix: str = "", after: str = "",
                            limit: int = 10000) -> tuple[list[tuple[str, int]], bool]:
        """SQL-only keyset page; return creation dates and a has-next marker.

        LIKE patterns are escaped to preserve literal S3 prefixes. SQL never
        materializes all matching buckets to serve a single bounded page.
        """
        if not 1 <= limit <= 10000:
            raise ValueError("Invalid bucket page limit")
        # Bucket names are strictly lowercase ASCII. SQLite LIKE can compare
        # case-insensitively, so uppercase prefixes can never match.
        if prefix != prefix.lower():
            return [], False
        statement = select(buckets.c.name, buckets.c.created_at)
        if prefix:
            statement = statement.where(buckets.c.name.startswith(prefix, autoescape=True))
        if after:
            statement = statement.where(buckets.c.name > after)
        rows = self.db.connection.execute(
            statement.order_by(buckets.c.name).limit(limit + 1),
        ).all()
        return [(str(r[0]), int(r[1])) for r in rows[:limit]], len(rows) > limit

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

    def is_visible_version(self, bucket: str, key: str, version_id: str) -> bool:
        """Require exact published key proof AND absence of a removal record."""
        row = self.db.connection.execute(
            select(published_versions.c.object_id)
            .select_from(published_versions.join(
                objects, objects.c.id == published_versions.c.object_id,
            ).outerjoin(
                removed_versions,
                removed_versions.c.object_id == published_versions.c.object_id,
            )).where(
                published_versions.c.object_id == version_id,
                published_versions.c.bucket == bucket,
                published_versions.c.key_hash == self._keyhash(key),
                objects.c.bucket == bucket,
                objects.c.object_key == key,
                objects.c.status.in_(("ready", "delete-marker")),
                removed_versions.c.object_id.is_(None),
            )
        ).scalar_one_or_none()
        return row is not None

    def delete_marker(self, bucket: str, key: str, *,
                      expected_generation: int | None = None,
                      expected_object_id: str | None = None) -> str:
        """Publish a zero-byte logical tombstone with one atomic head CAS.

        The marker is NOT a ready payload manifest; Telegram is never touched.
        A concurrent write must not be overwritten by a stale DELETE.
        """
        if bucket not in self.list_buckets():
            raise FileNotFoundError("Bucket does not exist")
        digest = self._keyhash(key)
        row = self.db.connection.execute(select(
            heads.c.object_id, heads.c.generation,
        ).where(heads.c.bucket == bucket, heads.c.key_hash == digest)).first()
        if expected_object_id is not None and (row is None or row[0] != expected_object_id):
            raise ValueError("Delete precondition object ID changed")
        if expected_generation is not None and (
            (int(row[1]) if row else 0) != expected_generation
        ):
            raise ValueError("Delete precondition generation changed")
        marker = str(uuid4())
        now = int(time.time())
        try:
            self.db.connection.execute(insert(objects).values(
                id=marker, bucket=bucket, object_key=key,
                status="delete-marker", size=0, sha256=None,
                created_at=now, updated_at=now, error=None,
            ))
            if row is None:
                try:
                    with self.db.connection.begin_nested():
                        self.db.connection.execute(insert(heads).values(
                            bucket=bucket, key_hash=digest, object_id=marker,
                            generation=1, updated_at=now,
                        ))
                except IntegrityError as exc:
                    raise ValueError("Concurrent object creation") from exc
            else:
                changed = self.db.connection.execute(update(heads).where(
                    heads.c.bucket == bucket, heads.c.key_hash == digest,
                    heads.c.object_id == row[0],
                    heads.c.generation == row[1],
                ).values(object_id=marker, generation=int(row[1]) + 1, updated_at=now))
                if changed.rowcount != 1:
                    raise ValueError("Concurrent object deletion/write")
            self.db.connection.execute(insert(published_versions).values(
                object_id=marker, bucket=bucket, key_hash=digest,
                committed_ns=time.time_ns(),
            ))
            self.db.commit()
            return marker
        except BaseException:
            self.db.connection.rollback()
            raise

    def delete_version(self, bucket: str, key: str, version_id: str) -> bool:
        """Hide an exact published VersionId, retaining immutable remote data.

        If the selected version owns HEAD, advance to the latest remaining
        version (including a prior marker); this is a compare-and-swap in the
        same SQL transaction as the version revocation.
        """
        digest = self._keyhash(key)
        try:
            # Serialize revocation with version-specific tag edits on the
            # underlying object row, even for historical (non-HEAD) versions.
            # A second DELETE waits for this write lock and then observes the
            # committed logical removal rather than leaking a unique-key error.
            lock = self.db.connection.execute(
                update(objects).where(
                    objects.c.id == version_id,
                    objects.c.status.in_(("ready", "delete-marker")),
                ).values(updated_at=objects.c.updated_at)
            )
            if lock.rowcount != 1:
                self.db.connection.rollback()
                return False
            # A plain SELECT can read an obsolete REPEATABLE READ snapshot on
            # MySQL, established earlier by the S3 credential lookup. This
            # locking current-read observes another session's committed
            # revocation even if our original snapshot predates it.
            revoked = self.db.connection.execute(
                select(removed_versions.c.object_id).where(
                    removed_versions.c.object_id == version_id,
                ).with_for_update()
            ).scalar_one_or_none()
            if revoked is not None or not self.is_visible_version(
                bucket, key, version_id
            ):
                self.db.connection.rollback()
                return False
            head = self.db.connection.execute(select(
                heads.c.object_id, heads.c.generation,
            ).where(heads.c.bucket == bucket, heads.c.key_hash == digest)).first()
            self.db.connection.execute(insert(removed_versions).values(
                object_id=version_id, removed_ns=time.time_ns(),
            ))
            if head is not None and head[0] == version_id:
                previous = self.db.connection.execute(
                    select(published_versions.c.object_id)
                    .select_from(published_versions.join(
                        objects, objects.c.id == published_versions.c.object_id,
                    ).outerjoin(removed_versions,
                        removed_versions.c.object_id == published_versions.c.object_id,
                    )).where(
                        published_versions.c.bucket == bucket,
                        published_versions.c.key_hash == digest,
                        objects.c.object_key == key,
                        objects.c.status.in_(("ready", "delete-marker")),
                        removed_versions.c.object_id.is_(None),
                    ).order_by(
                        published_versions.c.committed_ns.desc(),
                        published_versions.c.object_id.desc(),
                    ).limit(1)
                ).scalar_one_or_none()
                selector = (
                    heads.c.bucket == bucket, heads.c.key_hash == digest,
                    heads.c.object_id == version_id,
                    heads.c.generation == head[1],
                )
                if previous is None:
                    changed = self.db.connection.execute(delete(heads).where(*selector))
                else:
                    changed = self.db.connection.execute(update(heads).where(
                        *selector
                    ).values(object_id=previous, generation=int(head[1]) + 1,
                             updated_at=int(time.time())))
                if changed.rowcount != 1:
                    raise ValueError("Version HEAD changed concurrently")
            self.db.commit()
            return True
        except IntegrityError:
            # An idempotent duplicate DELETE is not a server error. Keep the
            # fail-closed behavior if a different constraint is violated.
            self.db.connection.rollback()
            if not self.is_visible_version(bucket, key, version_id):
                return False
            raise
        except BaseException:
            self.db.connection.rollback()
            raise

    def current_marker(self, bucket: str, key: str) -> str | None:
        digest = self._keyhash(key)
        return self.db.connection.execute(
            select(objects.c.id).select_from(heads.join(
                objects, heads.c.object_id == objects.c.id,
            )).where(
                heads.c.bucket == bucket, heads.c.key_hash == digest,
                objects.c.bucket == bucket, objects.c.object_key == key,
                objects.c.status == "delete-marker",
            )
        ).scalar_one_or_none()

    def _record_published_uncommitted(self, item: StoredObject) -> None:
        """Append a proven head-version record in its publication transaction.

        Records never represent an incomplete upload, overwritten staged part,
        or a failed conditional write. A historical HEAD predating this index
        can be registered explicitly when an operator requests its versions.
        """
        self.db.connection.execute(insert(published_versions).values(
            object_id=item.id, bucket=item.bucket,
            key_hash=self._keyhash(item.key),
            committed_ns=time.time_ns(),
        ))

    def index_legacy_current_heads(self, *, dry_run: bool = True,
                                   limit: int = 100) -> list[str]:
        """Backfill only proven current HEADs; unknown old versions stay hidden.

        An old READY manifest can be a never-published upload and is NOT safe
        to promote. This backfill only trusts pointers already in HEADs.
        """
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid history index limit")
        missing = self.db.connection.execute(
            select(heads.c.object_id, objects.c.bucket, objects.c.object_key)
            .select_from(
                heads.join(objects, heads.c.object_id == objects.c.id)
                     .outerjoin(
                         published_versions,
                         heads.c.object_id == published_versions.c.object_id,
                     )
            )
            .where(
                published_versions.c.object_id.is_(None),
                objects.c.status == "ready",
            )
            .order_by(heads.c.bucket, heads.c.key_hash).limit(limit)
        ).all()
        ids = []
        for identifier, bucket, key in missing:
            if self._keyhash(key) != self.db.connection.execute(
                select(heads.c.key_hash).where(
                    heads.c.bucket == bucket, heads.c.object_id == identifier,
                )
            ).scalar_one_or_none():
                raise RuntimeError("Legacy HEAD references inconsistent key metadata")
            ids.append(identifier)
            if not dry_run:
                self._record_published_uncommitted(
                    self.manifests.get(identifier)
                )
        if not dry_run and ids:
            self.db.commit()
        return ids

    def list_versions(self, bucket: str, key: str, *,
                      limit: int = 100) -> list[dict]:
        """Read versions proven published, not all READY/staged manifests."""
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid version limit")
        digest = self._keyhash(key)
        try:
            current = self.get(bucket, key).item.id
        except FileNotFoundError:
            current = None
        rows = self.db.connection.execute(
            select(published_versions.c.object_id,
                   published_versions.c.committed_ns,
                   objects.c.size, objects.c.sha256)
            .select_from(published_versions.join(
                objects, published_versions.c.object_id == objects.c.id
            ).outerjoin(
                removed_versions,
                removed_versions.c.object_id == published_versions.c.object_id,
            ))
            .where(
                removed_versions.c.object_id.is_(None),
                published_versions.c.bucket == bucket,
                published_versions.c.key_hash == digest,
                objects.c.status == "ready",
                objects.c.bucket == bucket,
                objects.c.object_key == key,
            ).order_by(
                published_versions.c.committed_ns.desc(),
                published_versions.c.object_id.desc(),
            ).limit(limit)
        ).mappings().all()
        return [
            {
                "version_id": row["object_id"],
                "size": int(row["size"]),
                "sha256": row["sha256"],
                "committed_ns": int(row["committed_ns"]),
                "is_current": row["object_id"] == current,
            }
            for row in rows
        ]

    def list_bucket_versions(
        self, bucket: str, *, prefix: str = "", key_marker: str = "",
        version_marker: str = "", limit: int = 1000,
    ) -> tuple[list[dict], bool]:
        """Bounded, deterministic S3 listing of proven published history.

        Never include READY manifests that did not win HEAD publication.
        The pagination marker must correspond to a published version of
        exactly the supplied bucket/key; dangling or forged markers fail.
        Delete markers are modeled as SQL-only published tombstones.
        """
        if not 1 <= limit <= 1000 or len(prefix.encode("utf-8")) > 1024:
            raise ValueError("Invalid version-list pagination")
        if version_marker and not key_marker:
            raise ValueError("Version-id marker requires key marker")
        where = [
            published_versions.c.bucket == bucket,
            objects.c.bucket == bucket,
            objects.c.status.in_(("ready", "delete-marker")),
            removed_versions.c.object_id.is_(None),
        ]
        if prefix:
            where.append(objects.c.object_key.startswith(prefix, autoescape=True))
        if key_marker:
            self._keyhash(key_marker)
            if version_marker:
                selected = self.db.connection.execute(
                    select(published_versions.c.committed_ns)
                    .select_from(published_versions.join(
                        objects, published_versions.c.object_id == objects.c.id
                    ).outerjoin(
                        removed_versions,
                        removed_versions.c.object_id == published_versions.c.object_id,
                    )).where(
                        removed_versions.c.object_id.is_(None),
                        published_versions.c.bucket == bucket,
                        objects.c.object_key == key_marker,
                        published_versions.c.object_id == version_marker,
                        objects.c.status.in_(("ready", "delete-marker")),
                    )
                ).scalar_one_or_none()
                if selected is None:
                    raise ValueError("Unknown version-id marker")
                where.append(or_(
                    objects.c.object_key > key_marker,
                    and_(
                        objects.c.object_key == key_marker,
                        or_(
                            published_versions.c.committed_ns < selected,
                            and_(
                                published_versions.c.committed_ns == selected,
                                published_versions.c.object_id < version_marker,
                            ),
                        ),
                    ),
                ))
            else:
                where.append(objects.c.object_key > key_marker)
        rows = self.db.connection.execute(
            select(
                published_versions.c.object_id,
                published_versions.c.committed_ns,
                objects.c.object_key, objects.c.size, objects.c.sha256,
                objects.c.status.label("object_status"),
                heads.c.object_id.label("current_id"),
            )
            .select_from(
                published_versions.join(
                    objects, published_versions.c.object_id == objects.c.id
                ).outerjoin(
                    removed_versions,
                    removed_versions.c.object_id == published_versions.c.object_id,
                ).outerjoin(
                    heads,
                    and_(
                        heads.c.bucket == published_versions.c.bucket,
                        heads.c.key_hash == published_versions.c.key_hash,
                        heads.c.object_id == published_versions.c.object_id,
                    ),
                )
            )
            .where(*where)
            .order_by(
                objects.c.object_key,
                published_versions.c.committed_ns.desc(),
                published_versions.c.object_id.desc(),
            ).limit(limit + 1)
        ).mappings().all()
        return [
            {
                "key": row["object_key"],
                "version_id": row["object_id"],
                "committed_ns": int(row["committed_ns"]),
                "size": int(row["size"]),
                "sha256": row["sha256"],
                "is_delete_marker": row["object_status"] == "delete-marker",
                "is_current": row["current_id"] is not None,
            }
            for row in rows[:limit]
        ], len(rows) > limit

    def publish(self, identifier: str, *, now: int,
                expected_generation: int | None = None,
                expected_object_id: str | None = None) -> ObjectHead:
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
            if expected_object_id is not None:
                raise ValueError("Object changed; object ID precondition failed")
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
            self._record_published_uncommitted(item)
            self.db.commit()
            return ObjectHead(item, 1)

        old = self.db.connection.execute(select(
            objects.c.bucket, objects.c.object_key, objects.c.status,
        ).where(objects.c.id == existing[0])).one_or_none()
        if old is None or old[0] != item.bucket or old[1] != item.key:
            raise RuntimeError("Object hash collision detected")
        if old[2] not in ("ready", "delete-marker"):
            raise RuntimeError("Current head refers to a non-published object")
        generation = int(existing[1])
        if expected_object_id is not None and expected_object_id != existing[0]:
            raise ValueError("Object changed; object ID precondition failed")
        if expected_generation is not None and expected_generation != generation:
            raise ValueError("Object changed; generation precondition failed")
        changed = self.db.connection.execute(
            update(heads)
            .where(
                heads.c.bucket == item.bucket, heads.c.key_hash == digest,
                heads.c.object_id == existing[0],
                heads.c.generation == generation,
            )
            .values(object_id=item.id, generation=generation + 1, updated_at=now)
        )
        if changed.rowcount != 1:
            self.db.connection.rollback()
            raise ValueError("Concurrent object modification")
        self._record_published_uncommitted(item)
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
                       expected_generation: int | None = None,
                       expected_object_id: str | None = None) -> bool:
        """Unlink a head only; Telegram data stays until separately verified GC."""
        digest = self._keyhash(key)
        try:
            current = self.get(bucket, key)
        except FileNotFoundError:
            return False
        if expected_generation is not None and current.generation != expected_generation:
            raise ValueError("Object changed; generation precondition failed")
        if expected_object_id is not None and current.item.id != expected_object_id:
            raise ValueError("Object changed; object ID precondition failed")
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
        # A bucket with initiated multipart uploads must not disappear, even
        # before the first part is uploaded. Retain all abort/completion
        # history until a verified lifecycle GC can safely remove it.
        if inspect(self.db.engine).has_table("storage_multipart_uploads"):
            mpu_history = self.db.connection.execute(
                text("SELECT id FROM storage_multipart_uploads WHERE bucket = :bucket LIMIT 1"),
                {"bucket": name},
            ).first()
            if mpu_history is not None:
                raise ValueError("Bucket has multipart history; safe GC required")
        if history is not None:
            raise ValueError("Bucket has object history; safe physical GC required")
        # A physically empty bucket can be deleted even if its tag set is
        # nonempty; do not leave dangling metadata across SQL backends.
        if inspect(self.db.engine).has_table("storage_bucket_tags"):
            self.db.connection.execute(
                text("DELETE FROM storage_bucket_tags WHERE bucket = :bucket"),
                {"bucket": name},
            )
        if inspect(self.db.engine).has_table("storage_bucket_lifecycle"):
            self.db.connection.execute(
                text("DELETE FROM storage_bucket_lifecycle WHERE bucket = :bucket"),
                {"bucket": name},
            )
        if inspect(self.db.engine).has_table("storage_bucket_cors"):
            self.db.connection.execute(
                text("DELETE FROM storage_bucket_cors WHERE bucket = :bucket"),
                {"bucket": name},
            )
        self.db.connection.execute(delete(buckets).where(buckets.c.name == name))
        self.db.commit()
