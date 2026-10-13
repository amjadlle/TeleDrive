"""S3 object tags stored per immutable Telegram object version.

Object body bytes always live in Telegram. Tags are small SQL metadata only.
Writing tags on one version never mutates an older version or a CopyObject
source. An object head is locked before current-version tag mutation.
"""
from __future__ import annotations

import re
import time

from sqlalchemy import (
    Column, ForeignKey, MetaData, String, Table, delete, insert, select, update,
)
from sqlalchemy.exc import IntegrityError

from teledrive.storage.catalog import BucketCatalog, heads

_meta = MetaData()
object_tags = Table(
    "storage_object_tags", _meta,
    Column("object_id", String(36), ForeignKey("storage_objects.id"), primary_key=True),
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
        _meta.create_all(self.db.engine)

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

    def copy_tags(self, source_id: str, destination_id: str) -> None:
        """Copy in the caller's transaction; never issue a separate commit."""
        for tag_key, tag_value in self.get(source_id):
            self.db.connection.execute(insert(object_tags).values(
                object_id=destination_id, tag_key=tag_key, tag_value=tag_value
            ))
