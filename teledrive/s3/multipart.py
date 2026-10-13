"""Crash-conscious S3 multipart staging, ownership, and zero-reupload completion.

Part bytes live in Telegram's immutable message storage. SQL holds staged
object IDs; Complete verifies every part and re-parents existing chunk refs
inside one DB transaction. No payload bytes persist in SQL.

Aborted/replaced/unselected parts are retained as *unpublished* objects until
an independently verified garbage collector supports them.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import (
    BigInteger, Column, ForeignKey, Integer, MetaData, String, Table, Text,
    delete, insert, select, update, and_,
)
from sqlalchemy.exc import IntegrityError

from teledrive.storage.catalog import BucketCatalog, buckets, heads
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.manifest import chunks, objects

MIN_PART_SIZE = 5 * 1024 * 1024
MAX_PARTS = 10000
_meta = MetaData()
uploads = Table(
    "storage_multipart_uploads", _meta,
    Column("id", String(36), primary_key=True),
    Column("bucket", String(63), nullable=False),
    Column("object_key", Text, nullable=False),
    Column("owner_access_key", String(64), nullable=False),
    Column("state", String(16), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("completed_object_id", String(36)),
    Column("completed_etag", String(128)),
)
parts = Table(
    "storage_multipart_parts", _meta,
    Column("upload_id", String(36), ForeignKey("storage_multipart_uploads.id"),
           primary_key=True),
    Column("number", Integer, primary_key=True),
    Column("object_id", String(36), nullable=False),
    Column("etag", String(64), nullable=False),
    Column("size", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
)
multipart_etags = Table(
    "storage_multipart_etags", _meta,
    Column("object_id", String(36), primary_key=True),
    Column("etag", String(128), nullable=False),
)


class MultipartError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class CompletedUpload:
    object_id: str
    etag: str
    size: int


class MultipartStore:
    def __init__(self, catalog: BucketCatalog):
        self.catalog = catalog
        self.db = catalog.db
        self.manifests = catalog.manifests
        _meta.create_all(self.db.engine)

    def initiate(self, bucket: str, key: str, owner: str, now: int | None = None) -> str:
        if bucket not in self.catalog.list_buckets():
            raise FileNotFoundError("Bucket does not exist")
        self.catalog._keyhash(key)
        if not owner:
            raise ValueError("Missing multipart owner")
        identifier = str(uuid4())
        self.db.connection.execute(insert(uploads).values(
            id=identifier, bucket=bucket, object_key=key, owner_access_key=owner,
            state="active", created_at=now or int(time.time()),
            completed_object_id=None, completed_etag=None,
        ))
        self.db.commit()
        return identifier

    def require(self, identifier: str, bucket: str, key: str, owner: str,
                *, allow_completed: bool = False):
        row = self.db.connection.execute(
            select(uploads).where(
                uploads.c.id == identifier, uploads.c.bucket == bucket,
                uploads.c.object_key == key, uploads.c.owner_access_key == owner,
            )
        ).mappings().first()
        if not row or row["state"] not in (("active", "completed") if allow_completed else ("active",)):
            raise MultipartError("NoSuchUpload", "Multipart upload not found or inactive", 404)
        return row

    def list_parts(self, identifier: str, bucket: str, key: str, owner: str):
        self.require(identifier, bucket, key, owner)
        return self.db.connection.execute(
            select(parts.c.number, parts.c.size, parts.c.etag).where(
                parts.c.upload_id == identifier
            ).order_by(parts.c.number)
        ).all()

    def record_part(self, identifier: str, bucket: str, key: str, owner: str,
                    *, number: int, object_id: str, etag: str) -> None:
        if number < 1 or number > MAX_PARTS or not re.fullmatch(r"[0-9a-f]{32}", etag):
            raise MultipartError("InvalidArgument", "Invalid part number or MD5 digest")
        obj = self.manifests.get(object_id)
        if obj.bucket != bucket or obj.key != key:
            raise MultipartError("InvalidPart", "Part object ownership mismatch")
        # Conditional state check blocks publishing a part after Abort/Complete.
        self.require(identifier, bucket, key, owner)
        old = self.db.connection.execute(select(parts.c.object_id).where(
            parts.c.upload_id == identifier, parts.c.number == number
        )).scalar_one_or_none()
        now = int(time.time())
        if old is None:
            try:
                with self.db.connection.begin_nested():
                    self.db.connection.execute(insert(parts).values(
                        upload_id=identifier, number=number, object_id=object_id,
                        etag=etag, size=obj.size, updated_at=now,
                    ))
            except IntegrityError as exc:
                raise MultipartError("OperationAborted", "Concurrent part update", 409) from exc
        else:
            self.db.connection.execute(update(parts).where(
                parts.c.upload_id == identifier, parts.c.number == number,
                parts.c.object_id == old,
            ).values(object_id=object_id, etag=etag, size=obj.size, updated_at=now))
        # Compare-and-update the upload row, rejecting races with completion.
        live = self.db.connection.execute(update(uploads).where(
            uploads.c.id == identifier, uploads.c.state == "active"
        ).values(state="active"))
        if live.rowcount != 1:
            self.db.connection.rollback()
            raise MultipartError("NoSuchUpload", "Multipart upload changed state", 404)
        self.db.commit()

    def abort(self, identifier: str, bucket: str, key: str, owner: str) -> None:
        self.require(identifier, bucket, key, owner)
        changed = self.db.connection.execute(update(uploads).where(
            uploads.c.id == identifier, uploads.c.state == "active"
        ).values(state="aborted"))
        self.db.commit()
        if changed.rowcount != 1:
            raise MultipartError("OperationAborted", "Concurrent multipart mutation", 409)

    @staticmethod
    def _validate_selection(selected: list[tuple[int, str]]) -> None:
        if not selected or len(selected) > MAX_PARTS:
            raise MultipartError("InvalidPart", "A multipart completion must select 1..10000 parts")
        previous = 0
        for number, etag in selected:
            if number <= previous:
                raise MultipartError("InvalidPartOrder", "Parts must be strictly increasing")
            if number < 1 or number > MAX_PARTS or not re.fullmatch(r"[0-9a-f]{32}", etag):
                raise MultipartError("InvalidPart", "Invalid part number or ETag")
            previous = number

    async def complete(self, identifier: str, bucket: str, key: str, owner: str,
                       selected: list[tuple[int, str]],
                       engine: TelegramObjectStorage) -> CompletedUpload:
        self._validate_selection(selected)
        row = self.require(identifier, bucket, key, owner, allow_completed=True)
        if row["state"] == "completed":
            final = self.manifests.get(row["completed_object_id"])
            return CompletedUpload(final.id, row["completed_etag"], final.size)
        # A DB conditional update fences concurrent completion, abort and part writes.
        changed = self.db.connection.execute(update(uploads).where(
            uploads.c.id == identifier, uploads.c.state == "active"
        ).values(state="completing"))
        self.db.commit()
        if changed.rowcount != 1:
            raise MultipartError("OperationAborted", "Multipart upload is being completed", 409)

        try:
            available = {int(p.number): p for p in self.db.connection.execute(
                select(parts).where(parts.c.upload_id == identifier)
            ).all()}
            chosen = []
            total_size = 0
            full_sha = hashlib.sha256()
            etag_md5 = hashlib.md5(usedforsecurity=False)
            for index, (number, etag) in enumerate(selected):
                part = available.get(number)
                if part is None or part.etag != etag:
                    raise MultipartError("InvalidPart", "Missing part or mismatched ETag")
                if index < len(selected) - 1 and part.size < MIN_PART_SIZE:
                    raise MultipartError("EntityTooSmall", "All parts except the last must be at least 5 MiB")
                # Re-read Telegram rather than trusting unverified SQL metadata.
                measured = 0
                async for block in engine.iter_bytes(part.object_id):
                    measured += len(block)
                    full_sha.update(block)
                if measured != part.size:
                    raise MultipartError("InvalidPart", "Part size changed")
                total_size += part.size
                etag_md5.update(bytes.fromhex(etag))
                chosen.append(part)
            final_etag = f"{etag_md5.hexdigest()}-{len(selected)}"
            object_id = str(uuid4())
            digest = full_sha.hexdigest()
            now = int(time.time())

            # One SQL transaction: existing Telegram chunk messages move from
            # immutable temporary part manifests to the final object.
            try:
                self.db.connection.execute(insert(objects).values(
                    id=object_id, bucket=bucket, object_key=key,
                    status="pending", size=total_size,
                    sha256=None, created_at=now, updated_at=now, error=None,
                ))
                offset, ordinal = 0, 0
                for part in chosen:
                    refs = self.manifests.parts(part.object_id)
                    part_bytes = 0
                    for ref in refs:
                        moved = self.db.connection.execute(
                            update(chunks).where(
                                chunks.c.object_id == part.object_id,
                                chunks.c.ordinal == ref.ordinal,
                            ).values(
                                object_id=object_id, ordinal=ordinal,
                                offset=offset + ref.offset,
                            )
                        )
                        if moved.rowcount != 1:
                            raise MultipartError("InvalidPart", "Telegram chunk ownership changed")
                        ordinal += 1
                        part_bytes += ref.size
                    if part_bytes != part.size:
                        raise MultipartError("InvalidPart", "Part manifest is incomplete")
                    offset += part.size
                    self.db.connection.execute(update(objects).where(
                        objects.c.id == part.object_id, objects.c.status == "ready"
                    ).values(status="consumed", updated_at=now))
                if offset != total_size:
                    raise MultipartError("InvalidPart", "Incomplete multipart assembly")
                self.db.connection.execute(update(objects).where(
                    objects.c.id == object_id, objects.c.status == "pending"
                ).values(status="ready", sha256=digest))
                self.db.connection.execute(insert(multipart_etags).values(
                    object_id=object_id, etag=final_etag,
                ))

                keyhash = self.catalog._keyhash(key)
                current = self.db.connection.execute(select(
                    heads.c.object_id, heads.c.generation
                ).where(heads.c.bucket == bucket, heads.c.key_hash == keyhash)).first()
                if current is None:
                    self.db.connection.execute(insert(heads).values(
                        bucket=bucket, key_hash=keyhash, object_id=object_id,
                        generation=1, updated_at=now,
                    ))
                else:
                    live = self.manifests.get(current[0])
                    if live.bucket != bucket or live.key != key:
                        raise MultipartError("InvalidPart", "Object key collision")
                    moved = self.db.connection.execute(update(heads).where(
                        heads.c.bucket == bucket, heads.c.key_hash == keyhash,
                        heads.c.object_id == current[0], heads.c.generation == current[1]
                    ).values(
                        object_id=object_id, generation=current[1] + 1,
                        updated_at=now,
                    ))
                    if moved.rowcount != 1:
                        raise MultipartError("OperationAborted", "Concurrent object replacement", 409)

                completed = self.db.connection.execute(update(uploads).where(
                    uploads.c.id == identifier, uploads.c.state == "completing"
                ).values(state="completed", completed_object_id=object_id,
                         completed_etag=final_etag))
                if completed.rowcount != 1:
                    raise MultipartError("OperationAborted", "Multipart upload lost ownership", 409)
                self.db.commit()
            except BaseException:
                self.db.connection.rollback()
                raise
            return CompletedUpload(object_id, final_etag, total_size)
        except BaseException:
            # Reset a failed completion for a safe explicit retry. All object
            # re-parenting above is rolled back if any DB write fails.
            self.db.connection.rollback()
            self.db.connection.execute(update(uploads).where(
                uploads.c.id == identifier, uploads.c.state == "completing"
            ).values(state="active"))
            self.db.commit()
            raise

    def object_etag(self, object_id: str, default_sha256: str) -> str:
        etag = self.db.connection.execute(select(multipart_etags.c.etag).where(
            multipart_etags.c.object_id == object_id
        )).scalar_one_or_none()
        return etag or default_sha256
