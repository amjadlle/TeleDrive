"""Atomic server-side S3 object copy without uploading Telegram media again.

The new object owns a distinct SQL manifest and head pointer, but refers to
the immutable original chunk journal. This is deliberately NOT a copy of
Telegram messages: future GC must preserve media while aliases reference it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from teledrive.storage.catalog import BucketCatalog, heads
from teledrive.storage.manifest import object_aliases, objects
from teledrive.s3.multipart import MultipartStore, multipart_etags
from teledrive.s3.tagging import ObjectTagStore
from teledrive.s3.object_metadata import ObjectMetadataStore


class CopyConflict(ValueError):
    """Concurrent destination object creation or replacement."""


@dataclass(frozen=True)
class CopiedObject:
    object_id: str
    etag: str
    size: int
    source_object_id: str


def copy_object(
    catalog: BucketCatalog, source_bucket: str, source_key: str,
    destination_bucket: str, destination_key: str, *, now: int | None = None,
    expected_source_id: str | None = None,
    replacement_tags: list[tuple[str, str]] | None = None,
    replacement_metadata: dict[str, str] | None = None,
    source_version_id: str | None = None,
    expected_destination_generation: int | None = None,
    expected_destination_object_id: str | None = None,
) -> CopiedObject:
    """Publish a snapshot copy with one SQL transaction.

    A copy of a copy points directly to the original content manifest, so
    chains cannot grow indefinitely. It works across buckets using the same
    Telegram storage, and preserves S3 multipart ETags where applicable.
    """
    db = catalog.db
    manifests = catalog.manifests
    now = int(time.time()) if now is None else now
    if source_version_id is not None:
        # The caller may restore only a version previously published to this
        # exact bucket/key, never arbitrary READY objects or staged MPU parts.
        if not catalog.is_visible_version(source_bucket, source_key, source_version_id):
            raise FileNotFoundError("Published object version not found")
        source = manifests.get(source_version_id)
        if source.bucket != source_bucket or source.key != source_key:
            raise ValueError("Version source metadata mismatch")
    else:
        source = catalog.get(source_bucket, source_key).item
    if expected_source_id is not None and source.id != expected_source_id:
        raise CopyConflict("Copy source object changed during request")
    base_id = manifests.resolve_content_id(source.id)
    base = manifests.get(base_id)
    if base.size != source.size or base.sha256 != source.sha256:
        raise ValueError("Copy source manifest integrity mismatch")
    catalog._keyhash(destination_key)
    if destination_bucket not in catalog.list_buckets():
        raise FileNotFoundError("Destination bucket not found")

    multipart = MultipartStore(catalog)
    tagging = ObjectTagStore(catalog)
    original_tag = multipart.object_etag(source.id, source.sha256)
    identifier = str(uuid4())
    key_hash = catalog._keyhash(destination_key)

    try:
        db.connection.execute(insert(objects).values(
            id=identifier, bucket=destination_bucket, object_key=destination_key,
            status="ready", size=source.size, sha256=source.sha256,
            created_at=now, updated_at=now, error=None,
        ))
        db.connection.execute(insert(object_aliases).values(
            object_id=identifier, source_object_id=base_id,
        ))
        # COPY snapshots the source's tag set; REPLACE attaches only the
        # caller's validated tags. Both happen before the head-pointer commit.
        if replacement_tags is None:
            tagging.copy_tags(source.id, identifier)
        else:
            tagging.attach_uncommitted(identifier, replacement_tags)
        http = ObjectMetadataStore(catalog)
        if replacement_metadata is None:
            http.copy_uncommitted(source.id, identifier)
        else:
            http.attach_uncommitted(identifier, replacement_metadata)
        if original_tag != source.sha256:
            db.connection.execute(insert(multipart_etags).values(
                object_id=identifier, etag=original_tag,
            ))
            # Preserve actual selected part boundaries as immutable metadata.
            # Without this, a copied historical multipart ETag could claim a
            # part count while GetObjectAttributes cannot prove its contents.
            multipart.snapshot_copy_parts(source.id, identifier, source.size)

        current = db.connection.execute(select(
            heads.c.object_id, heads.c.generation
        ).where(
            heads.c.bucket == destination_bucket, heads.c.key_hash == key_hash
        )).first()
        if current is None:
            if expected_destination_object_id is not None or (
                expected_destination_generation not in (None, 0)
            ):
                raise CopyConflict("Destination generation changed")
            db.connection.execute(insert(heads).values(
                bucket=destination_bucket, key_hash=key_hash,
                object_id=identifier, generation=1, updated_at=now,
            ))
        else:
            if expected_destination_generation is not None and (
                expected_destination_generation != int(current[1])
                or expected_destination_object_id != current[0]
            ):
                raise CopyConflict("Destination generation changed")
            old = db.connection.execute(select(
                objects.c.bucket, objects.c.object_key, objects.c.status,
            ).where(objects.c.id == current[0])).one_or_none()
            if (old is None or old[0] != destination_bucket or old[1] != destination_key
                    or old[2] not in ("ready", "delete-marker")):
                raise ValueError("Destination hash collision")
            updated = db.connection.execute(update(heads).where(
                heads.c.bucket == destination_bucket,
                heads.c.key_hash == key_hash,
                heads.c.object_id == current[0],
                heads.c.generation == current[1],
            ).values(
                object_id=identifier, generation=current[1] + 1,
                updated_at=now,
            ))
            if updated.rowcount != 1:
                raise CopyConflict("Destination was changed concurrently")
        catalog._record_published_uncommitted(manifests.get(identifier))
        db.commit()
        return CopiedObject(identifier, original_tag, source.size, source.id)
    except IntegrityError as exc:
        db.connection.rollback()
        raise CopyConflict("Concurrent copy or destination mutation") from exc
    except BaseException:
        db.connection.rollback()
        raise
