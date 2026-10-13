"""S3 object tags stored per immutable Telegram object version.

Object body bytes always live in Telegram. Tags are small SQL metadata only.
Writing tags on one version never mutates an older version or a CopyObject
source. An object head is locked before current-version tag mutation.
"""
from __future__ import annotations

import re
import time

from sqlalchemy import (
    Column, ForeignKey, String, Table, delete, insert, select, update,
)
from sqlalchemy.exc import IntegrityError

from teledrive.storage.catalog import (
    BucketCatalog, heads, removed_versions, catalog_metadata,
)
from teledrive.storage.manifest import metadata as _meta, objects
object_tags = Table(
    "storage_object_tags", _meta,
    Column("object_id", String(36), ForeignKey("storage_objects.id"), primary_key=True),
    Column("tag_key", String(512), primary_key=True),
    Column("tag_value", String(1024), nullable=False),
)


bucket_tags = Table(
    "storage_bucket_tags", catalog_metadata,
    Column("bucket", String(63), ForeignKey("storage_buckets.name"), primary_key=True),
    Column("tag_key", String(512), primary_key=True),
    Column("tag_value", String(1024), nullable=False),
)


class TaggingError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def validate_tags(tags: list[tuple[str, str]]) -> list[tuple[str, str]]:
    if not isinstance(tags, list) or len(tags) > 10:
        raise TaggingError("InvalidTag", "Object supports at most 10 tags")
    seen = set()
    result = []
    for key, value in tags:
        if not isinstance(key, str) or not isinstance(value, str):
            raise TaggingError("InvalidTag", "Tag keys and values must be strings")
        # AWS counts tag lengths in UTF-16 code units, not UTF-8 bytes.
        try:
            key_units = len(key.encode("utf-16-le")) // 2
            val_units = len(value.encode("utf-16-le")) // 2
        except UnicodeError as exc:
            raise TaggingError("InvalidTag", "Invalid Unicode in tag") from exc
        if (not 1 <= key_units <= 128 or val_units > 256
                or key.lower().startswith("aws:")):
            raise TaggingError("InvalidTag", "Invalid key/value length or reserved aws: prefix")
        if key in seen:
            raise TaggingError("InvalidTag", "Duplicate tag key")
        seen.add(key)
        result.append((key, value))
    return result


class ObjectTagStore:
    def __init__(self, catalog: BucketCatalog):
        self.catalog = catalog
        self.db = catalog.db
        _meta.create_all(self.db.engine, tables=[object_tags])

    def get(self, object_id: str) -> list[tuple[str, str]]:
        return list(self.db.connection.execute(
            select(object_tags.c.tag_key, object_tags.c.tag_value)
            .where(object_tags.c.object_id == object_id)
            .order_by(object_tags.c.tag_key)
        ).all())

    def replace_current(self, bucket: str, key: str,
                        tags: list[tuple[str, str]]) -> str:
        tags = validate_tags(tags)
        # Lock the current head row while replacing its version-scoped tags.
        # On PostgreSQL/MySQL this UPDATE serializes concurrent head changes.
        digest = self.catalog._keyhash(key)
        try:
            current = self.catalog.get(bucket, key)
            guard = self.db.connection.execute(update(heads).where(
                heads.c.bucket == bucket, heads.c.key_hash == digest,
                heads.c.object_id == current.item.id,
                heads.c.generation == current.generation,
            ).values(updated_at=int(time.time())))
            if guard.rowcount != 1:
                raise TaggingError("OperationAborted", "Object replaced during tagging", 409)
            self.db.connection.execute(delete(object_tags).where(
                object_tags.c.object_id == current.item.id
            ))
            for tag_key, tag_value in tags:
                self.db.connection.execute(insert(object_tags).values(
                    object_id=current.item.id, tag_key=tag_key, tag_value=tag_value
                ))
            self.db.commit()
            return current.item.id
        except BaseException:
            self.db.connection.rollback()
            raise

    def replace_published(self, bucket: str, key: str, identifier: str,
                          tags: list[tuple[str, str]]) -> None:
        """Mutate tags on only a proven published immutable version.

        Unknown/unpublished READY staging is never writable through VersionId.
        SQL row lock fences concurrent physical metadata modifications while
        the actual Telegram payload remains immutable.
        """
        tags = validate_tags(tags)
        try:
            # Acquire the writer lock FIRST, before any consistent SELECT.
            # MySQL REPEATABLE READ would otherwise pin a stale snapshot
            # while waiting on a concurrent VersionId DELETE's object lock.
            locked = self.db.connection.execute(
                update(objects).where(
                    objects.c.id == identifier,
                    objects.c.bucket == bucket,
                    objects.c.object_key == key,
                    objects.c.status == "ready",
                ).values(updated_at=objects.c.updated_at)
            )
            if locked.rowcount != 1:
                raise TaggingError("NoSuchVersion", "Object version unavailable", 404)
            revoked = self.db.connection.execute(
                select(removed_versions.c.object_id).where(
                    removed_versions.c.object_id == identifier
                ).with_for_update()
            ).scalar_one_or_none()
            if revoked is not None or not self.catalog.is_visible_version(
                bucket, key, identifier
            ):
                raise TaggingError("NoSuchVersion", "Object version revoked or unpublished", 404)
            self.db.connection.execute(
                delete(object_tags).where(object_tags.c.object_id == identifier)
            )
            for tag_key, tag_value in tags:
                self.db.connection.execute(insert(object_tags).values(
                    object_id=identifier, tag_key=tag_key, tag_value=tag_value,
                ))
            self.db.commit()
        except BaseException:
            self.db.connection.rollback()
            raise

    def attach_uncommitted(self, object_id: str,
                           tags: list[tuple[str, str]]) -> None:
        """Set initial tags before publishing head; caller commits both atomically."""
        for tag_key, tag_value in validate_tags(tags):
            self.db.connection.execute(insert(object_tags).values(
                object_id=object_id, tag_key=tag_key, tag_value=tag_value
            ))

    def copy_tags(self, source_id: str, destination_id: str) -> None:
        """Copy in the caller's transaction; never issue a separate commit."""
        for tag_key, tag_value in self.get(source_id):
            self.db.connection.execute(insert(object_tags).values(
                object_id=destination_id, tag_key=tag_key, tag_value=tag_value
            ))


class BucketTagStore:
    """Bucket tags are distinct metadata from object version tags."""

    def __init__(self, catalog: BucketCatalog):
        self.catalog = catalog
        self.db = catalog.db
        catalog_metadata.create_all(self.db.engine, tables=[bucket_tags])

    def _ensure_bucket(self, bucket: str) -> None:
        from teledrive.storage.catalog import buckets
        present = self.db.connection.execute(
            select(buckets.c.name).where(buckets.c.name == bucket)
        ).first()
        if present is None:
            raise FileNotFoundError("Bucket not found")

    def get(self, bucket: str) -> list[tuple[str, str]]:
        self._ensure_bucket(bucket)
        return list(self.db.connection.execute(
            select(bucket_tags.c.tag_key, bucket_tags.c.tag_value)
            .where(bucket_tags.c.bucket == bucket)
            .order_by(bucket_tags.c.tag_key)
        ).all())

    def replace(self, bucket: str, tags: list[tuple[str, str]]) -> None:
        from teledrive.storage.catalog import buckets
        tags = validate_tags(tags)
        try:
            # Serialize concurrent bucket deletion/tag mutation.
            guarded = self.db.connection.execute(update(buckets).where(
                buckets.c.name == bucket
            ).values(created_at=buckets.c.created_at))
            if guarded.rowcount != 1:
                raise FileNotFoundError("Bucket not found")
            self.db.connection.execute(delete(bucket_tags).where(
                bucket_tags.c.bucket == bucket
            ))
            for key, value in tags:
                self.db.connection.execute(insert(bucket_tags).values(
                    bucket=bucket, tag_key=key, tag_value=value
                ))
            self.db.commit()
        except BaseException:
            self.db.connection.rollback()
            raise
