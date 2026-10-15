"""Crash-conscious S3 multipart staging, ownership, and zero-reupload completion.

Part bytes live in Telegram's immutable message storage. SQL holds staged
object IDs; Complete verifies every part and re-parents existing chunk refs
inside one DB transaction. No payload bytes persist in SQL.

Aborted/replaced/unselected parts are retained as *unpublished* objects until
an independently verified garbage collector supports them.
"""
from __future__ import annotations

import asyncio
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
from teledrive.s3.object_metadata import ObjectMetadataStore
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
completion_parts = Table(
    "storage_multipart_completion_parts", _meta,
    Column("upload_id", String(36), ForeignKey("storage_multipart_uploads.id"), primary_key=True),
    Column("position", Integer, primary_key=True),
    Column("number", Integer, nullable=False),
    Column("etag", String(64), nullable=False),
)
completion_leases = Table(
    "storage_multipart_completion_leases", _meta,
    Column("upload_id", String(36), ForeignKey("storage_multipart_uploads.id"), primary_key=True),
    Column("token", String(80), nullable=False),
    Column("lease_until", BigInteger, nullable=False),
    Column("heartbeat_at", BigInteger, nullable=False),
)
multipart_etags = Table(
    "storage_multipart_etags", _meta,
    Column("object_id", String(36), primary_key=True),
    Column("etag", String(128), nullable=False),
)
# Additive immutable snapshot. Completion publishes this in the SAME SQL
# transaction as the final manifest, multipart ETag, and catalog head.
# Never derive logical S3 part numbers from physical Telegram chunks.
multipart_object_parts = Table(
    "storage_multipart_object_parts", _meta,
    Column("object_id", String(36), primary_key=True),
    Column("number", Integer, primary_key=True),
    Column("size", BigInteger, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("etag", String(64), nullable=False),
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

    def initiate(self, bucket: str, key: str, owner: str, now: int | None = None,
                 *, http_metadata: dict[str, str] | None = None) -> str:
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
        if http_metadata:
            ObjectMetadataStore(self.catalog).stage_upload_uncommitted(
                identifier, http_metadata,
            )
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

    def list_uploads(self, bucket: str, owner: str, *,
                     prefix: str = "", after_key: str = "",
                     after_id: str = "", limit: int = 1000):
        """Return a bounded, owner-scoped page and whether another page exists."""
        if not 1 <= limit <= 1000 or len(prefix.encode("utf-8")) > 1024:
            raise MultipartError("InvalidArgument", "Invalid multipart list pagination")
        if after_id and not after_key:
            raise MultipartError("InvalidArgument", "Upload-id marker requires key marker")
        stmt = select(uploads.c.id, uploads.c.object_key, uploads.c.created_at).where(
            uploads.c.bucket == bucket,
            uploads.c.owner_access_key == owner,
            uploads.c.state == "active",
        )
        if prefix:
            stmt = stmt.where(uploads.c.object_key.startswith(prefix, autoescape=True))
        if after_key:
            if after_id:
                stmt = stmt.where(
                    (uploads.c.object_key > after_key)
                    | ((uploads.c.object_key == after_key) & (uploads.c.id > after_id))
                )
            else:
                stmt = stmt.where(uploads.c.object_key > after_key)
        rows = self.db.connection.execute(
            stmt.order_by(uploads.c.object_key, uploads.c.id).limit(limit + 1)
        ).all()
        return rows[:limit], len(rows) > limit

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

    def _claim_completion(self, identifier: str, now: int, ttl: int) -> str:
        """The state change and ownership token commit together."""
        token = uuid4().hex
        try:
            changed = self.db.connection.execute(update(uploads).where(
                uploads.c.id == identifier, uploads.c.state == "active"
            ).values(state="completing"))
            if changed.rowcount != 1:
                self.db.connection.rollback()
                raise MultipartError("OperationAborted", "Upload is already completing", 409)
            self.db.connection.execute(insert(completion_leases).values(
                upload_id=identifier, token=token,
                lease_until=now + ttl, heartbeat_at=now,
            ))
            self.db.commit()
        except BaseException:
            self.db.connection.rollback()
            raise
        return token

    def _renew_completion(self, identifier: str, token: str, now: int, ttl: int,
                          *, commit: bool = True) -> bool:
        changed = self.db.connection.execute(update(completion_leases).where(
            completion_leases.c.upload_id == identifier,
            completion_leases.c.token == token,
            completion_leases.c.lease_until >= now,
        ).values(lease_until=now + ttl, heartbeat_at=now))
        if commit:
            self.db.commit()
        return changed.rowcount == 1

    async def _heartbeat_completion(self, identifier: str, token: str, ttl: int,
                                    stop: asyncio.Event, lost: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(1, ttl // 3))
                return
            except asyncio.TimeoutError:
                pass
            try:
                if not self._renew_completion(identifier, token, int(time.time()), ttl):
                    lost.set()
                    return
            except Exception:
                self.db.connection.rollback()
                lost.set()
                return

    def _reset_owned_completion(self, identifier: str, token: str) -> bool:
        """Never reset a lease that another worker has recovered or taken."""
        now = int(time.time())
        try:
            owned = self.db.connection.execute(update(completion_leases).where(
                completion_leases.c.upload_id == identifier,
                completion_leases.c.token == token,
                completion_leases.c.lease_until >= now,
            ).values(heartbeat_at=now))
            if owned.rowcount != 1:
                self.db.connection.rollback()
                return False
            changed = self.db.connection.execute(update(uploads).where(
                uploads.c.id == identifier, uploads.c.state == "completing"
            ).values(state="active"))
            if changed.rowcount != 1:
                self.db.connection.rollback()
                return False
            self.db.connection.execute(delete(completion_leases).where(
                completion_leases.c.upload_id == identifier,
                completion_leases.c.token == token,
            ))
            self.db.commit()
            return True
        except BaseException:
            self.db.connection.rollback()
            raise

    def stale_completions(self, *, now: int | None = None,
                          min_age_seconds: int = 3600, limit: int = 100) -> list[dict]:
        """Read-only operator inventory; never infer ownership of legacy unfenced rows."""
        if min_age_seconds < 60 or not 1 <= limit <= 1000:
            raise ValueError("Unsafe stale completion limits")
        now = int(time.time()) if now is None else now
        records = self.db.connection.execute(
            select(uploads.c.id, uploads.c.bucket, uploads.c.object_key,
                   completion_leases.c.lease_until)
            .join(completion_leases, uploads.c.id == completion_leases.c.upload_id)
            .where(uploads.c.state == "completing",
                   completion_leases.c.lease_until < now - min_age_seconds)
            .order_by(completion_leases.c.lease_until, uploads.c.id)
            .limit(limit)
        ).mappings().all()
        return [dict(row) for row in records]

    def recover_stale_completion(self, identifier: str, *, dry_run: bool = True,
                                 now: int | None = None,
                                 min_age_seconds: int = 3600) -> bool:
        """Fence an expired writer and put uncommitted multipart data back in ACTIVE.

        An upload with no recorded lease (older pre-fencing installations)
        is deliberately not recovered by this method. No Telegram message
        is removed. Completed writes never become active again.
        """
        if min_age_seconds < 60:
            raise ValueError("Stale completion minimum age must be >= 60 seconds")
        now = int(time.time()) if now is None else now
        try:
            found = self.db.connection.execute(select(
                uploads.c.state, completion_leases.c.token,
                completion_leases.c.lease_until,
            ).select_from(
                uploads.join(completion_leases,
                             uploads.c.id == completion_leases.c.upload_id)
            ).where(uploads.c.id == identifier)).first()
            if (not found or found[0] != "completing"
                    or int(found[2]) >= now - min_age_seconds):
                self.db.connection.rollback()
                return False
            if dry_run:
                self.db.connection.rollback()
                return True
            # Take ownership by atomically invalidating the prior writer.
            # The final SQL publication renews this same row in its transaction.
            fenced = self.db.connection.execute(update(completion_leases).where(
                completion_leases.c.upload_id == identifier,
                completion_leases.c.token == found[1],
                completion_leases.c.lease_until < now - min_age_seconds,
            ).values(token="recovered:" + uuid4().hex,
                     lease_until=now, heartbeat_at=now))
            if fenced.rowcount != 1:
                self.db.connection.rollback()
                return False
            result = self.db.connection.execute(update(uploads).where(
                uploads.c.id == identifier, uploads.c.state == "completing"
            ).values(state="active"))
            if result.rowcount != 1:
                self.db.connection.rollback()
                return False
            self.db.connection.execute(delete(completion_leases).where(
                completion_leases.c.upload_id == identifier,
            ))
            self.db.commit()
            return True
        except BaseException:
            self.db.connection.rollback()
            raise

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
                       engine: TelegramObjectStorage, *,
                       lease_seconds: int = 120,
                       expected_generation: int | None = None,
                       expected_object_id: str | None = None) -> CompletedUpload:
        self._validate_selection(selected)
        row = self.require(identifier, bucket, key, owner, allow_completed=True)
        if row["state"] == "completed":
            # A retry can return the committed result only for exactly the
            # same part selection. Another completion body is not equivalent.
            original = self.db.connection.execute(select(
                completion_parts.c.number, completion_parts.c.etag
            ).where(completion_parts.c.upload_id == identifier)
             .order_by(completion_parts.c.position)).all()
            if [(p.number, p.etag) for p in original] != selected:
                raise MultipartError("InvalidPart", "Completion part list differs from committed object")
            final = self.manifests.get(row["completed_object_id"])
            return CompletedUpload(final.id, row["completed_etag"], final.size)
        if lease_seconds < 5:
            raise ValueError("Completion lease must last at least 5 seconds")
        token = self._claim_completion(identifier, int(time.time()), lease_seconds)
        stop = asyncio.Event()
        lost = asyncio.Event()
        renewal = asyncio.create_task(
            self._heartbeat_completion(identifier, token, lease_seconds, stop, lost)
        )
        try:
            available = {int(p.number): p for p in self.db.connection.execute(
                select(parts).where(parts.c.upload_id == identifier)
            ).all()}
            chosen = []
            chosen_hashes: list[str] = []
            total_size = 0
            full_sha = hashlib.sha256()
            etag_md5 = hashlib.md5(usedforsecurity=False)
            for index, (number, etag) in enumerate(selected):
                part = available.get(number)
                if part is None or part.etag != etag:
                    raise MultipartError("InvalidPart", "Missing part or mismatched ETag")
                if index < len(selected) - 1 and part.size < MIN_PART_SIZE:
                    raise MultipartError("EntityTooSmall", "All parts except the last must be at least 5 MiB")
                # Read real Telegram bytes, checking each immutable part digest
                # as well as the final concatenated plaintext SHA-256.
                source_manifest = self.manifests.get(part.object_id)
                measured = 0
                part_sha = hashlib.sha256()
                async for block in engine.iter_bytes(part.object_id):
                    measured += len(block)
                    part_sha.update(block)
                    full_sha.update(block)
                if (measured != part.size or source_manifest.size != part.size
                        or part_sha.hexdigest() != source_manifest.sha256):
                    raise MultipartError("InvalidPart", "Part size or SHA-256 changed")
                total_size += part.size
                etag_md5.update(bytes.fromhex(etag))
                chosen.append(part)
                chosen_hashes.append(source_manifest.sha256)
            # Stop background DB writes before the publication transaction.
            stop.set()
            await renewal
            if lost.is_set():
                raise MultipartError("OperationAborted", "Completion writer lease lost", 409)
            final_etag = f"{etag_md5.hexdigest()}-{len(selected)}"
            object_id = str(uuid4())
            digest = full_sha.hexdigest()
            now = int(time.time())

            # One SQL transaction: existing Telegram chunk messages move from
            # immutable temporary part manifests to the final object.
            try:
                # SQL fencing point: recovery cannot invalidate this token
                # while the committed object/chunk/head transaction is running.
                if not self._renew_completion(
                    identifier, token, int(time.time()), lease_seconds, commit=False
                ):
                    raise MultipartError("OperationAborted", "Completion lease was lost", 409)
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
                # Publish the selected logical multipart layout atomically.
                # A discarded/replaced upload part is never exposed by this
                # object's attributes or historical VersionId.
                for (number, etag), part, part_sha in zip(
                    selected, chosen, chosen_hashes, strict=True
                ):
                    self.db.connection.execute(insert(multipart_object_parts).values(
                        object_id=object_id, number=number, size=part.size,
                        sha256=part_sha, etag=etag,
                    ))
                # Metadata set at CreateMultipartUpload becomes immutable
                # metadata on the newly published version, not on parts.
                ObjectMetadataStore(self.catalog).attach_upload_uncommitted(
                    identifier, object_id
                )
                for position, (number, etag) in enumerate(selected):
                    self.db.connection.execute(insert(completion_parts).values(
                        upload_id=identifier, position=position,
                        number=number, etag=etag,
                    ))

                keyhash = self.catalog._keyhash(key)
                current = self.db.connection.execute(select(
                    heads.c.object_id, heads.c.generation
                ).where(heads.c.bucket == bucket, heads.c.key_hash == keyhash)).first()
                if current is None:
                    if expected_object_id is not None or expected_generation not in (None, 0):
                        raise MultipartError(
                            "PreconditionFailed", "Multipart destination changed", 412
                        )
                    self.db.connection.execute(insert(heads).values(
                        bucket=bucket, key_hash=keyhash, object_id=object_id,
                        generation=1, updated_at=now,
                    ))
                else:
                    if expected_generation is not None and (
                        expected_generation != int(current[1])
                        or expected_object_id != current[0]
                    ):
                        raise MultipartError(
                            "PreconditionFailed", "Multipart destination changed", 412
                        )
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

                self.catalog._record_published_uncommitted(
                    self.manifests.get(object_id)
                )
                completed = self.db.connection.execute(update(uploads).where(
                    uploads.c.id == identifier, uploads.c.state == "completing"
                ).values(state="completed", completed_object_id=object_id,
                         completed_etag=final_etag))
                if completed.rowcount != 1:
                    raise MultipartError("OperationAborted", "Multipart upload lost ownership", 409)
                self.db.connection.execute(delete(completion_leases).where(
                    completion_leases.c.upload_id == identifier,
                    completion_leases.c.token == token,
                ))
                self.db.commit()
            except BaseException:
                self.db.connection.rollback()
                raise
            return CompletedUpload(object_id, final_etag, total_size)
        except BaseException:
            # An old worker must never reopen an upload once its lease expired,
            # was fenced by recovery, or was committed by another worker.
            self.db.connection.rollback()
            self._reset_owned_completion(identifier, token)
            raise
        finally:
            stop.set()
            await renewal

    def object_parts_for_version(
        self, object_id: str, object_size: int
    ) -> list[tuple[int, int, str, str]] | None:
        """Return the immutable S3 layout, or None for a non-multipart object.

        Old completed uploads are read through their original SQL completion
        journal; no destructive backfill and no Telegram I/O are needed.
        Aliased server-side copies resolve their verified original payload.
        Missing/inconsistent records fail closed rather than emitting a
        plausible but false ObjectParts success response.
        """
        tag = self.db.connection.execute(select(multipart_etags.c.etag).where(
            multipart_etags.c.object_id == object_id
        )).scalar_one_or_none()
        if tag is None:
            return None
        match = re.fullmatch(r"[0-9a-f]{32}-([0-9]+)", tag)
        if match is None:
            raise MultipartError("InvalidObjectState", "Invalid multipart ETag record", 409)
        count = int(match.group(1))
        if count < 1 or count > MAX_PARTS:
            raise MultipartError("InvalidObjectState", "Invalid multipart part count", 409)

        content_id = self.manifests.resolve_content_id(object_id)
        content = self.manifests.get(content_id)
        target = self.manifests.get(object_id)
        if (content.size != target.size or content.sha256 != target.sha256
                or target.size != object_size):
            raise MultipartError("InvalidObjectState", "Copied multipart manifest mismatch", 409)

        def saved(identifier: str) -> list[tuple[int, int, str, str]]:
            rows = self.db.connection.execute(select(
                multipart_object_parts.c.number,
                multipart_object_parts.c.size,
                multipart_object_parts.c.sha256,
                multipart_object_parts.c.etag,
            ).where(
                multipart_object_parts.c.object_id == identifier
            ).order_by(multipart_object_parts.c.number)).all()
            return [(int(row[0]), int(row[1]), str(row[2]), str(row[3]))
                    for row in rows]

        layout = saved(object_id)
        if not layout and content_id != object_id:
            layout = saved(content_id)
        if not layout:
            # Compatibility for multipart versions committed before the
            # additive object-parts snapshot table was deployed. A completed
            # journal binds this source object ID, not the current HEAD.
            rows = self.db.connection.execute(
                select(
                    completion_parts.c.number, parts.c.size,
                    objects.c.sha256, completion_parts.c.etag, parts.c.etag,
                ).select_from(
                    completion_parts.join(
                        uploads, uploads.c.id == completion_parts.c.upload_id,
                    ).join(
                        parts, and_(
                            parts.c.upload_id == completion_parts.c.upload_id,
                            parts.c.number == completion_parts.c.number,
                        ),
                    ).join(objects, objects.c.id == parts.c.object_id)
                ).where(
                    uploads.c.completed_object_id == content_id,
                    uploads.c.state == "completed",
                ).order_by(completion_parts.c.position)
            ).all()
            if any(row[3] != row[4] for row in rows):
                raise MultipartError("InvalidObjectState", "Legacy multipart ETag mismatch", 409)
            layout = [(int(r[0]), int(r[1]), str(r[2]), str(r[3])) for r in rows]

        if len(layout) != count or sum(p[1] for p in layout) != object_size:
            raise MultipartError("InvalidObjectState", "Multipart snapshot is incomplete", 409)
        previous = 0
        for number, size, digest, etag in layout:
            if (number <= previous or number > MAX_PARTS or size < 0
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or not re.fullmatch(r"[0-9a-f]{32}", etag)):
                raise MultipartError("InvalidObjectState", "Invalid immutable multipart part", 409)
            previous = number
        return layout

    def snapshot_copy_parts(
        self, source_id: str, destination_id: str, source_size: int
    ) -> None:
        """Clone logical part metadata, never Telegram messages, in COPY's txn."""
        layout = self.object_parts_for_version(source_id, source_size)
        for number, size, digest, etag in layout or ():
            self.db.connection.execute(insert(multipart_object_parts).values(
                object_id=destination_id, number=number, size=size,
                sha256=digest, etag=etag,
            ))

    def object_etag(self, object_id: str, default_sha256: str) -> str:
        etag = self.db.connection.execute(select(multipart_etags.c.etag).where(
            multipart_etags.c.object_id == object_id
        )).scalar_one_or_none()
        return etag or default_sha256
