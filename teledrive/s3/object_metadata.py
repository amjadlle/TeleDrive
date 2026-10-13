"""Immutable S3 HTTP headers and user metadata per Telegram-backed object.

Only small metadata lives in SQL. Bodies and all chunks remain in Telegram.
Writes can join the caller's head-pointer transaction; multipart initiations
journal metadata separately and copy it when Complete commits the new object.
"""
from __future__ import annotations

import json
import re

from sqlalchemy import Column, ForeignKey, String, Table, Text, insert, select, delete
from teledrive.storage.manifest import metadata as manifest_metadata
from teledrive.storage.catalog import BucketCatalog

object_http_metadata = Table(
    "storage_object_http_metadata", manifest_metadata,
    Column("object_id", String(36), ForeignKey("storage_objects.id"), primary_key=True),
    Column("properties", Text, nullable=False),
)
upload_http_metadata = Table(
    "storage_multipart_http_metadata", manifest_metadata,
    Column("upload_id", String(36), primary_key=True),
    Column("properties", Text, nullable=False),
)

STANDARD_HEADERS = frozenset({
    "content-type", "content-encoding", "content-language",
    "content-disposition", "cache-control", "expires",
})
_METADATA_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,127}\Z")
MAX_METADATA_BYTES = 2048


class ObjectMetadataError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def parse_object_metadata(headers, signed_headers: set[str]) -> dict[str, str]:
    """Only bind supported, signed headers; reject hidden unsigned mutations.

    Starlette's Headers supports multi_items(); an ordinary mapping supports
    items(). Repeated user metadata/header names are ambiguous and rejected.
    Unsupported S3 directives must never silently claim success.
    """
    pairs = headers.multi_items() if hasattr(headers, "multi_items") else headers.items()
    result: dict[str, str] = {}
    for name, value in pairs:
        key = name.lower()
        if key not in STANDARD_HEADERS and not key.startswith("x-amz-meta-"):
            continue
        if key in result:
            raise ObjectMetadataError("InvalidArgument", "Repeated object metadata field")
        if key not in signed_headers:
            raise ObjectMetadataError("AccessDenied", "Object metadata must be signed", 403)
        if key.startswith("x-amz-meta-") and not _METADATA_KEY.fullmatch(key[11:]):
            raise ObjectMetadataError("InvalidArgument", "Invalid user metadata name")
        if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ObjectMetadataError("InvalidArgument", "Invalid object metadata value")
        if len(value.encode("utf-8")) > 1024:
            raise ObjectMetadataError("InvalidArgument", "Object metadata field too large")
        result[key] = value
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_METADATA_BYTES:
        raise ObjectMetadataError("MetadataTooLarge", "Object metadata exceeds 2 KiB")
    return result


def signed_header_names(request) -> set[str]:
    if "X-Amz-Algorithm" in request.query_params:
        return set(request.query_params.get("X-Amz-SignedHeaders", "").split(";"))
    auth = request.headers.get("authorization", "")
    return set(auth.partition("SignedHeaders=")[2].split(",", 1)[0].split(";"))


class ObjectMetadataStore:
    def __init__(self, catalog: BucketCatalog):
        self.db = catalog.db
        manifest_metadata.create_all(
            self.db.engine, tables=[object_http_metadata, upload_http_metadata],
        )

    def get(self, object_id: str) -> dict[str, str]:
        data = self.db.connection.execute(
            select(object_http_metadata.c.properties).where(
                object_http_metadata.c.object_id == object_id
            )
        ).scalar_one_or_none()
        return json.loads(data) if data is not None else {}

    def attach_uncommitted(self, object_id: str, values: dict[str, str]) -> None:
        if values:
            self.db.connection.execute(insert(object_http_metadata).values(
                object_id=object_id,
                properties=json.dumps(values, ensure_ascii=False, sort_keys=True),
            ))

    def copy_uncommitted(self, source_id: str, destination_id: str) -> None:
        self.attach_uncommitted(destination_id, self.get(source_id))

    def stage_upload_uncommitted(self, upload_id: str, values: dict[str, str]) -> None:
        if values:
            self.db.connection.execute(insert(upload_http_metadata).values(
                upload_id=upload_id,
                properties=json.dumps(values, ensure_ascii=False, sort_keys=True),
            ))

    def attach_upload_uncommitted(self, upload_id: str, object_id: str) -> None:
        raw = self.db.connection.execute(select(
            upload_http_metadata.c.properties
        ).where(upload_http_metadata.c.upload_id == upload_id)).scalar_one_or_none()
        if raw:
            self.db.connection.execute(insert(object_http_metadata).values(
                object_id=object_id, properties=raw,
            ))

    def delete_upload_uncommitted(self, upload_id: str) -> None:
        self.db.connection.execute(delete(upload_http_metadata).where(
            upload_http_metadata.c.upload_id == upload_id
        ))
