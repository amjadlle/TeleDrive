"""Telegram-backed path-style S3 gateway (initial core subset).

Run as a separate ASGI process at port 9000. Shares SQL metadata and Telegram
Bot API credentials with REST; never stores object bodies in another S3 service.
Unimplemented S3 operations explicitly return NotImplemented (501).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import tempfile
import time
import zlib
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from teledrive.s3.credentials import S3CredentialStore, load_master_key
from teledrive.s3.sigv4 import SigV4Error, verify_header_signature
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.manifest import ManifestStore
from teledrive.storage.bot_api_adapter import BOT_RECOMMENDED_CHUNK_SIZE
from web.app import db_connect, storage_bot_transport, _secure_mkdir

NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"
app = FastAPI(title="TeleDrive S3 (initial compatibility subset)", docs_url=None,
              redoc_url=None, openapi_url=None)


def _xml(tag: str, pairs: list[tuple[str, str]] | None = None) -> bytes:
    root = ET.Element(tag, xmlns=NAMESPACE)
    for name, value in pairs or []:
        ET.SubElement(root, name).text = str(value)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _error(code: str, message: str, status: int) -> Response:
    return Response(_xml("Error", [("Code", code), ("Message", message)]),
                    media_type="application/xml", status_code=status)


def _headers(item) -> dict[str, str]:
    return {
        "ETag": '"' + item.sha256 + '"',
        "Content-Length": str(item.size),
        "Accept-Ranges": "bytes",
        "Content-Type": "application/octet-stream",
        "x-amz-version-id": item.id,
    }


def _range(value: str | None, size: int) -> tuple[int, int, bool]:
    if value is None:
        return 0, size, False
    import re
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or not (match.group(1) or match.group(2)) or size <= 0:
        raise SigV4Error("InvalidRange", "Unsupported byte range")
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last) + 1, size) if last else size
    else:
        amount = int(last)
        start, end = max(size - amount, 0), size
    if start >= size or start >= end or (not first and int(last) <= 0):
        raise SigV4Error("InvalidRange", "Unsatisfiable byte range")
    return start, end, True


def _list_xml(bucket: str, keys: list, prefix: str, limit: int, after: str,
              more: bool, *, delimiter: str = "") -> bytes:
    root = ET.Element("ListBucketResult", xmlns=NAMESPACE)
    for field, val in (
        ("Name", bucket), ("Prefix", prefix), ("KeyCount", len(keys)),
        ("MaxKeys", limit), ("IsTruncated", "true" if more else "false"),
    ):
        ET.SubElement(root, field).text = str(val)
    if delimiter:
        ET.SubElement(root, "Delimiter").text = delimiter
    if after:
        ET.SubElement(root, "StartAfter").text = after
    for item in keys:
        c = ET.SubElement(root, "Contents")
        for field, value in (
            ("Key", item.item.key),
            ("LastModified", time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(item.item.created_at))),
            ("ETag", '"' + item.item.sha256 + '"'),
            ("Size", item.item.size),
            ("StorageClass", "STANDARD"),
        ):
            ET.SubElement(c, field).text = str(value)
    if more and keys:
        # An opaque continuation key will be supported in the multipart/listing phase.
        # Until then reject paginated clients rather than claim completion.
        ET.SubElement(root, "NextContinuationToken").text = base64.urlsafe_b64encode(
            keys[-1].item.key.encode("utf-8")).decode().rstrip("=")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


async def _handle(request: Request):
    db = None
    retain = False
    try:
        raw_path = request.scope.get("raw_path", b"/").decode("ascii")
        logical = unquote(raw_path.lstrip("/"))
        bucket, separator, key = logical.partition("/")
        if not bucket:
            if request.method != "GET":
                return _error("MethodNotAllowed", "Use GET to list buckets", 405)
            action = "s3:ListAllMyBuckets"
        elif not separator:
            action = {
                "GET": "s3:ListBucket", "HEAD": "s3:ListBucket",
                "PUT": "s3:CreateBucket", "DELETE": "s3:DeleteBucket",
            }.get(request.method)
        else:
            action = {
                "GET": "s3:GetObject", "HEAD": "s3:GetObject",
                "PUT": "s3:PutObject", "DELETE": "s3:DeleteObject",
            }.get(request.method)
        if action is None or "uploads" in request.query_params or "uploadId" in request.query_params:
            return _error("NotImplemented", "This S3 operation is not supported yet", 501)
        # Unknown control-plane query operations must not be misinterpreted as
        # ordinary object reads/writes (e.g. ?tagging, ?versioning, ?acl).
        unsupported = {
            "acl", "versioning", "versions", "tagging", "lifecycle", "policy",
            "notification", "cors", "replication", "uploads", "uploadId",
            "partNumber", "retention", "legal-hold", "logging", "location",
            "encryption", "object-lock", "publicAccessBlock",
        }
        if any(parameter in request.query_params for parameter in unsupported):
            return _error("NotImplemented", "This S3 extension is not implemented", 501)

        master = load_master_key()
        db = db_connect()
        store = S3CredentialStore(db, master)
        verify_header_signature(request, store.resolve, action)
        catalog = BucketCatalog(ManifestStore(db))

        if not bucket:
            root = ET.Element("ListAllMyBucketsResult", xmlns=NAMESPACE)
            ET.SubElement(root, "Owner")
            buckets_node = ET.SubElement(root, "Buckets")
            for name in catalog.list_buckets():
                entry = ET.SubElement(buckets_node, "Bucket")
                ET.SubElement(entry, "Name").text = name
            return Response(ET.tostring(root, encoding="utf-8", xml_declaration=True),
                            media_type="application/xml")

        if not separator:
            if request.method == "PUT":
                catalog.create_bucket(bucket, int(time.time()))
                return Response(status_code=200)
            if request.method == "DELETE":
                catalog.delete_bucket(bucket)
                return Response(status_code=204)
            if bucket not in catalog.list_buckets():
                return _error("NoSuchBucket", "The specified bucket does not exist", 404)
            if request.method == "HEAD":
                return Response(status_code=200)
            if request.query_params.get("list-type") not in (None, "2"):
                return _error("NotImplemented", "Only ListObjectsV2 is supported", 501)
            prefix = request.query_params.get("prefix", "")
            delimiter = request.query_params.get("delimiter", "")
            if delimiter:
                return _error("NotImplemented", "Delimiter listing is not supported yet", 501)
            token = request.query_params.get("continuation-token")
            if token:
                try:
                    padding = "=" * (-len(token) % 4)
                    after = base64.urlsafe_b64decode(token + padding).decode("utf-8")
                except (ValueError, UnicodeError):
                    return _error("InvalidArgument", "Invalid continuation token", 400)
                if not after.startswith(prefix):
                    return _error("InvalidArgument", "Continuation token prefix does not match", 400)
            else:
                after = request.query_params.get("start-after", "")
            try:
                limit = int(request.query_params.get("max-keys", "1000"))
            except ValueError:
                return _error("InvalidArgument", "Invalid max-keys", 400)
            if limit < 1 or limit > 1000:
                return _error("InvalidArgument", "max-keys must be between 1 and 1000", 400)
            items = catalog.list_objects(bucket, prefix=prefix, after=after, limit=limit)
            more = bool(items and catalog.list_objects(bucket, prefix=prefix,
                                                        after=items[-1].item.key, limit=1))
            return Response(_list_xml(bucket, items, prefix, limit, after, more),
                            media_type="application/xml")

        if bucket not in catalog.list_buckets():
            return _error("NoSuchBucket", "The specified bucket does not exist", 404)
        if not key:
            return _error("InvalidObjectName", "Object key is empty", 400)

        if request.method == "DELETE":
            catalog.remove_current(bucket, key)
            return Response(status_code=204)

        if request.method == "PUT":
            root = Path(os.environ.get("TELEDRIVE_STORAGE_SCRATCH_DIR", "/data/tmp"))
            _secure_mkdir(root)
            max_bytes = int(os.environ.get("TELEDRIVE_REST_MAX_UPLOAD_BYTES", str(2 * 1024**3)))
            transport = storage_bot_transport(db)
            await transport.verify_target()
            fd, name = tempfile.mkstemp(prefix="s3-upload-", dir=root)
            stage = Path(name)
            digest = hashlib.sha256()
            crc = 0
            length = 0
            try:
                with os.fdopen(fd, "wb") as dest:
                    async for block in request.stream():
                        length += len(block)
                        if max_bytes < 1 or length > max_bytes:
                            return _error("EntityTooLarge", "Upload exceeds configured maximum", 413)
                        digest.update(block)
                        crc = zlib.crc32(block, crc)
                        dest.write(block)
                    dest.flush()
                    os.fsync(dest.fileno())
                header_digest = request.headers.get("x-amz-content-sha256", "")
                if header_digest != digest.hexdigest():
                    return _error("XAmzContentSHA256Mismatch", "Payload checksum mismatch", 400)
                checksum_crc = request.headers.get("x-amz-checksum-crc32")
                if checksum_crc and checksum_crc != base64.b64encode((crc & 0xffffffff).to_bytes(4, "big")).decode():
                    return _error("BadDigest", "CRC32 checksum mismatch", 400)
                engine = TelegramObjectStorage(
                    catalog.manifests, transport,
                    chunk_size=BOT_RECOMMENDED_CHUNK_SIZE, scratch_directory=root,
                )
                item = await engine.put_file(bucket, key, stage)
                catalog.publish(item.id, now=int(time.time()))
                return Response(status_code=200, headers={"ETag": '"' + item.sha256 + '"'})
            finally:
                stage.unlink(missing_ok=True)

        try:
            item = catalog.get(bucket, key).item
        except FileNotFoundError:
            return _error("NoSuchKey", "The specified key does not exist", 404)
        if request.method == "HEAD":
            return Response(status_code=200, headers=_headers(item))
        start, end, partial = _range(request.headers.get("range"), item.size)
        transport = storage_bot_transport(db)
        scratch = Path(os.environ.get("TELEDRIVE_STORAGE_SCRATCH_DIR", "/data/tmp"))
        _secure_mkdir(scratch)
        engine = TelegramObjectStorage(
            catalog.manifests, transport,
            chunk_size=BOT_RECOMMENDED_CHUNK_SIZE, scratch_directory=scratch,
        )

        async def stream():
            try:
                async for piece in engine.iter_bytes(item.id, start=start, end=end):
                    yield piece
            finally:
                db.close()

        retain = True
        headers = _headers(item)
        headers["Content-Length"] = str(end - start)
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end-1}/{item.size}"
        return StreamingResponse(stream(), status_code=206 if partial else 200, headers=headers)
    except SigV4Error as exc:
        return _error(exc.code, exc.message, {
            "RequestTimeTooSkewed": 403, "NotImplemented": 501,
            "XAmzContentSHA256Mismatch": 400, "InvalidRange": 416,
        }.get(exc.code, 403))
    except FileNotFoundError:
        return _error("NoSuchKey", "The requested resource does not exist", 404)
    except ValueError:
        return _error("InvalidArgument", "Invalid request parameters", 400)
    except RuntimeError:
        return _error("ServiceUnavailable", "S3 storage backend is unavailable", 503)
    finally:
        if db is not None and not retain:
            db.close()


@app.get("/__health", include_in_schema=False)
def health():
    # Container-local liveness only; no credentials or Telegram calls.
    return {"ok": True}


@app.api_route("/", methods=["GET", "PUT", "POST", "DELETE", "HEAD"])
async def root(request: Request):
    return await _handle(request)


@app.api_route("/{path:path}", methods=["GET", "PUT", "POST", "DELETE", "HEAD"])
async def path_route(request: Request, path: str):
    return await _handle(request)
