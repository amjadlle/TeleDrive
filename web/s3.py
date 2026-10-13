"""Telegram-backed path-style S3 gateway (initial core subset).

Run as a separate ASGI process at port 9000. Shares SQL metadata and Telegram
Bot API credentials with REST; never stores object bodies in another S3 service.
Unimplemented S3 operations explicitly return NotImplemented (501).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import tempfile
import time
import zlib
from pathlib import Path
from urllib.parse import unquote, parse_qsl
from xml.etree import ElementTree as ET
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from teledrive.s3.credentials import S3CredentialStore, load_master_key
from teledrive.s3.sigv4 import SigV4Error, verify_header_signature
from teledrive.s3.multipart import MultipartStore, MultipartError, MAX_PARTS
from teledrive.s3.batch_delete import BatchDeleteError, MAX_XML_BYTES, parse_batch, verify_body
from teledrive.s3.tagging import ObjectTagStore, BucketTagStore, TaggingError, validate_tags
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.copy import copy_object, CopyConflict
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


def _headers(item, etag: str | None = None) -> dict[str, str]:
    return {
        "ETag": '"' + (etag or item.sha256) + '"',
        "Content-Length": str(item.size),
        "Accept-Ranges": "bytes",
        "Content-Type": "application/octet-stream",
        "x-amz-version-id": item.id,
    }


def _verify_upload_sha256(request: Request, digest: bytes) -> None:
    """Verify actual bytes against checksum committed to the signed request.

    Presigned PUTs cannot use an unsigned mutable payload: the required
    SHA-256 checksum must be included among the signed request headers.
    """
    hexdigest = digest.hex()
    signed_content = request.headers.get("x-amz-content-sha256", "")
    signed_checksum = request.headers.get("x-amz-checksum-sha256", "")
    presigned = "X-Amz-Algorithm" in request.query_params
    if signed_content == hexdigest:
        return
    if presigned and signed_checksum and hmac.compare_digest(
        signed_checksum,
        base64.b64encode(digest).decode("ascii"),
    ):
        return
    raise SigV4Error("XAmzContentSHA256Mismatch", "Signed SHA-256 body checksum mismatch")


def _initial_tags(request: Request, owner: str, credentials: S3CredentialStore):
    """Parse only SigV4-bound x-amz-tagging; never let an unsigned header mutate metadata."""
    raw = request.headers.get("x-amz-tagging")
    if raw is None:
        return None
    if len(raw.encode("utf-8")) > 8192:
        raise TaggingError("InvalidTag", "Tag header is too large")
    if "X-Amz-Algorithm" in request.query_params:
        signed = request.query_params.get("X-Amz-SignedHeaders", "").split(";")
    else:
        auth = request.headers.get("authorization", "")
        signed = auth.partition("SignedHeaders=")[2].split(",", 1)[0].split(";")
    if "x-amz-tagging" not in signed:
        raise SigV4Error("AccessDenied", "x-amz-tagging must be signed")
    if credentials.resolve(owner, "s3:PutObjectTagging") is None:
        raise SigV4Error("AccessDenied", "PutObjectTagging permission required")
    import re
    if re.search(r"%(?![0-9A-Fa-f]{2})", raw):
        raise TaggingError("InvalidTag", "Malformed tag URL encoding")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=10)
    except (UnicodeError, ValueError) as exc:
        raise TaggingError("InvalidTag", "Malformed tagging header") from exc
    return validate_tags(pairs)


def _tag_xml(tags: list[tuple[str, str]]) -> bytes:
    root = ET.Element("Tagging", xmlns=NAMESPACE)
    tagset = ET.SubElement(root, "TagSet")
    for key, value in tags:
        tag = ET.SubElement(tagset, "Tag")
        ET.SubElement(tag, "Key").text = key
        ET.SubElement(tag, "Value").text = value
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _parse_tag_xml(body: bytes) -> list[tuple[str, str]]:
    if len(body) > 16 * 1024:
        raise TaggingError("MalformedXML", "Tagging XML exceeds maximum size")
    try:
        root = SafeET.fromstring(body)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise TaggingError("MalformedXML", "Tagging XML is malformed") from exc
    def tag_name(node):
        return node.tag.rsplit("}", 1)[-1]
    if (tag_name(root) != "Tagging" or root.attrib
            or len(root) != 1 or tag_name(root[0]) != "TagSet"
            or root[0].attrib):
        raise TaggingError("MalformedXML", "Unexpected tagging XML structure")
    entries = []
    for tag in root[0]:
        if tag_name(tag) != "Tag" or tag.attrib or len(tag) != 2:
            raise TaggingError("MalformedXML", "Invalid Tag element")
        children = {tag_name(child): child for child in tag}
        if (set(children) != {"Key", "Value"} or
                any(child.attrib or len(child) for child in tag)):
            raise TaggingError("MalformedXML", "Invalid Tag key/value fields")
        entries.append((children["Key"].text or "", children["Value"].text or ""))
    return validate_tags(entries)


async def _tag_request_tags(request: Request) -> list[tuple[str, str]]:
    body = bytearray()
    async for block in request.stream():
        body.extend(block)
        if len(body) > 16 * 1024:
            raise TaggingError("MalformedXML", "Tagging XML exceeds maximum size")
    _verify_upload_sha256(request, hashlib.sha256(body).digest())
    md5_header = request.headers.get("content-md5")
    if md5_header and not hmac.compare_digest(
        md5_header,
        base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode("ascii")
    ):
        raise TaggingError("BadDigest", "Content-MD5 checksum mismatch")
    return _parse_tag_xml(bytes(body))


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


def _cursor_encode(master: bytes, bucket: str, prefix: str, delimiter: str,
                   after: str, skip_prefix: str) -> str:
    payload = json.dumps(
        {"bucket": bucket, "prefix": prefix, "delimiter": delimiter,
         "after": after, "skip_prefix": skip_prefix},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(master, encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _cursor_decode(token: str, master: bytes, bucket: str,
                   prefix: str, delimiter: str) -> tuple[str, str]:
    try:
        encoded, signature = token.split(".", 1)
        if len(token) > 4096 or not hmac.compare_digest(
            signature, hmac.new(master, encoded.encode(), hashlib.sha256).hexdigest()
        ):
            raise ValueError("Signature mismatch")
        parsed = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if (not isinstance(parsed, dict) or parsed.get("bucket") != bucket
                or parsed.get("prefix") != prefix or parsed.get("delimiter") != delimiter
                or not isinstance(parsed.get("after"), str)
                or not isinstance(parsed.get("skip_prefix"), str)):
            raise ValueError("Cursor does not match request")
        return parsed["after"], parsed["skip_prefix"]
    except (ValueError, UnicodeError, TypeError, KeyError, binascii.Error) as exc:
        raise SigV4Error("InvalidArgument", "Invalid continuation token") from exc


def _page(catalog: BucketCatalog, bucket: str, prefix: str,
          delimiter: str, after: str, limit: int,
          skip_prefix: str) -> tuple[list, list[str], str, str, bool]:
    """Page over raw keys without splitting a common-prefix across pages.

    The last emitted prefix is carried in the signed continuation cursor
    so a folder containing >1000 objects is not shown repeatedly.
    """
    keys = []
    common = []
    seen = {skip_prefix} if skip_prefix else set()
    position = after
    last_consumed = after
    while True:
        batch = catalog.list_objects(bucket, prefix=prefix, after=position, limit=1000)
        if not batch:
            return keys, common, last_consumed, skip_prefix, False
        for item in batch:
            key = item.item.key
            tail = key[len(prefix):]
            common_name = ""
            if delimiter and delimiter in tail:
                common_name = prefix + tail.split(delimiter, 1)[0] + delimiter
            if common_name and common_name in seen:
                position = key
                last_consumed = key
                continue
            if len(keys) + len(common) >= limit:
                return keys, common, last_consumed, skip_prefix, True
            if common_name:
                common.append(common_name)
                seen.add(common_name)
                skip_prefix = common_name
            else:
                keys.append(item)
                skip_prefix = ""
            position = key
            last_consumed = key
        if len(batch) < 1000:
            return keys, common, last_consumed, skip_prefix, False


def _list_xml(bucket: str, keys: list, common: list[str], prefix: str,
              limit: int, after: str, delimiter: str,
              more: bool, next_token: str | None, etag_for=None) -> bytes:
    root = ET.Element("ListBucketResult", xmlns=NAMESPACE)
    for field, val in (
        ("Name", bucket), ("Prefix", prefix), ("KeyCount", len(keys) + len(common)),
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
            ("ETag", '"' + (etag_for(item.item) if etag_for else item.item.sha256) + '"'),
            ("Size", item.item.size),
            ("StorageClass", "STANDARD"),
        ):
            ET.SubElement(c, field).text = str(value)
    for name in common:
        ET.SubElement(ET.SubElement(root, "CommonPrefixes"), "Prefix").text = name
    if next_token:
        ET.SubElement(root, "NextContinuationToken").text = next_token
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
        multi_delete = bool(bucket and not separator and request.method == "POST"
                            and "delete" in request.query_params)
        if "delete" in request.query_params and not multi_delete:
            return _error("NotImplemented", "Unsupported DeleteObjects route", 501)
        mpu_bucket_list = bool(bucket and not separator and "uploads" in request.query_params
                               and request.method == "GET")
        mpu_start = bool(separator and "uploads" in request.query_params and request.method == "POST")
        mpu_id = request.query_params.get("uploadId") if separator else None
        mpu_part = bool(mpu_id and request.method == "PUT" and "partNumber" in request.query_params)
        mpu_complete = bool(mpu_id and request.method == "POST")
        mpu_abort = bool(mpu_id and request.method == "DELETE")
        mpu_list = bool(mpu_id and request.method == "GET")
        is_object_tagging = bool(separator and "tagging" in request.query_params)
        is_bucket_tagging = bool(bucket and not separator and "tagging" in request.query_params)
        if (is_object_tagging or is_bucket_tagging) and any(
            k in request.query_params for k in ("uploads", "uploadId", "versionId")
        ):
            return _error("NotImplemented", "Tagging with multipart/version selectors unsupported", 501)
        if multi_delete:
            if any(p in request.query_params for p in ("tagging", "uploads", "uploadId", "versionId")):
                return _error("NotImplemented", "Conflicting DeleteObjects controls", 501)
            action = "s3:DeleteObject"
        elif is_object_tagging:
            action = {
                "GET": "s3:GetObjectTagging",
                "PUT": "s3:PutObjectTagging",
                "DELETE": "s3:DeleteObjectTagging",
            }.get(request.method)
        elif is_bucket_tagging:
            action = {
                "GET": "s3:GetBucketTagging",
                "PUT": "s3:PutBucketTagging",
                "DELETE": "s3:DeleteBucketTagging",
            }.get(request.method)
        elif mpu_start or mpu_part or mpu_complete:
            action = "s3:PutObject"
        elif mpu_bucket_list:
            action = "s3:ListBucket"
        elif mpu_abort:
            action = "s3:DeleteObject"
        elif mpu_list:
            action = "s3:GetObject"
        elif "uploads" in request.query_params or "uploadId" in request.query_params:
            return _error("NotImplemented", "This multipart operation is unsupported", 501)
        if action is None or (separator and "partNumber" in request.query_params and not mpu_part):
            return _error("NotImplemented", "This S3 operation is not supported yet", 501)
        # Unknown control-plane query operations must not be misinterpreted as
        # ordinary object reads/writes (e.g. ?tagging, ?versioning, ?acl).
        unsupported = {
            "acl", "versioning", "versions", "lifecycle", "policy",
            "notification", "cors", "replication",
            "retention", "legal-hold", "logging", "location",
            "encryption", "object-lock", "publicAccessBlock", "versionId",
        }
        if any(parameter in request.query_params for parameter in unsupported):
            return _error("NotImplemented", "This S3 extension is not implemented", 501)

        master = load_master_key()
        db = db_connect()
        store = S3CredentialStore(db, master)
        owner = verify_header_signature(request, store.resolve, action)
        inline_tags = _initial_tags(request, owner, store) if (
            separator and request.method == "PUT" and not is_object_tagging
        ) else None
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
            if multi_delete:
                if bucket not in catalog.list_buckets():
                    return _error("NoSuchBucket", "The specified bucket does not exist", 404)
                if request.headers.get("x-amz-mfa") or request.headers.get(
                    "x-amz-bypass-governance-retention"
                ):
                    return _error("NotImplemented", "Retention/MFA Delete unsupported", 501)
                body = bytearray()
                async for block in request.stream():
                    body.extend(block)
                    if len(body) > MAX_XML_BYTES:
                        raise BatchDeleteError("MalformedXML", "DeleteObjects XML exceeds 1 MiB")
                payload = bytes(body)
                verify_body(payload, request.headers)
                batch = parse_batch(payload)
                result = ET.Element("DeleteResult", xmlns=NAMESPACE)
                for key_to_delete in batch.keys:
                    try:
                        catalog.remove_current(bucket, key_to_delete)
                    except ValueError:
                        problem = ET.SubElement(result, "Error")
                        ET.SubElement(problem, "Key").text = key_to_delete
                        ET.SubElement(problem, "Code").text = "InvalidObjectName"
                        ET.SubElement(problem, "Message").text = "Invalid object name"
                        continue
                    if not batch.quiet:
                        deleted = ET.SubElement(result, "Deleted")
                        ET.SubElement(deleted, "Key").text = key_to_delete
                return Response(ET.tostring(result, encoding="utf-8", xml_declaration=True),
                                media_type="application/xml", status_code=200)
            if is_bucket_tagging:
                if bucket not in catalog.list_buckets():
                    return _error("NoSuchBucket", "Bucket does not exist", 404)
                tagstore = BucketTagStore(catalog)
                if request.method == "GET":
                    values = tagstore.get(bucket)
                    if not values:
                        return _error("NoSuchTagSet", "No bucket tag set exists", 404)
                    return Response(_tag_xml(values), media_type="application/xml")
                if request.method == "PUT":
                    tagstore.replace(bucket, await _tag_request_tags(request))
                    return Response(status_code=204)
                if request.method == "DELETE":
                    async for block in request.stream():
                        if block:
                            raise TaggingError("InvalidRequest", "DeleteBucketTagging must have no body")
                    tagstore.replace(bucket, [])
                    return Response(status_code=204)
                return _error("NotImplemented", "Unsupported bucket tagging method", 501)
            if request.method == "PUT":
                catalog.create_bucket(bucket, int(time.time()))
                return Response(status_code=200)
            if request.method == "DELETE":
                try:
                    catalog.delete_bucket(bucket)
                except ValueError as exc:
                    raise MultipartError("BucketNotEmpty", str(exc), 409) from exc
                return Response(status_code=204)
            if bucket not in catalog.list_buckets():
                return _error("NoSuchBucket", "The specified bucket does not exist", 404)
            if request.method == "HEAD":
                return Response(status_code=200)
            if mpu_bucket_list:
                records, more = MultipartStore(catalog).list_uploads(
                    bucket, owner,
                    prefix=request.query_params.get("prefix", ""),
                    after_key=request.query_params.get("key-marker", ""),
                    after_id=request.query_params.get("upload-id-marker", ""),
                    limit=int(request.query_params.get("max-uploads", "1000")),
                )
                root = ET.Element("ListMultipartUploadsResult", xmlns=NAMESPACE)
                for label, val in (
                    ("Bucket", bucket),
                    ("KeyMarker", request.query_params.get("key-marker", "")),
                    ("UploadIdMarker", request.query_params.get("upload-id-marker", "")),
                    ("MaxUploads", request.query_params.get("max-uploads", "1000")),
                    ("IsTruncated", str(more).lower()),
                ):
                    ET.SubElement(root, label).text = str(val)
                if more and records:
                    ET.SubElement(root, "NextKeyMarker").text = records[-1].object_key
                    ET.SubElement(root, "NextUploadIdMarker").text = records[-1].id
                for record in records:
                    item = ET.SubElement(root, "Upload")
                    for label, value in (
                        ("Key", record.object_key), ("UploadId", record.id),
                        ("Initiated", time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                                    time.gmtime(record.created_at))),
                    ):
                        ET.SubElement(item, label).text = str(value)
                return Response(ET.tostring(root, encoding="utf-8", xml_declaration=True),
                                media_type="application/xml")
            if request.query_params.get("list-type") not in (None, "2"):
                return _error("NotImplemented", "Only ListObjectsV2 is supported", 501)
            prefix = request.query_params.get("prefix", "")
            delimiter = request.query_params.get("delimiter", "")
            if len(prefix.encode("utf-8")) > 1024 or len(delimiter.encode("utf-8")) > 16:
                return _error("InvalidArgument", "Prefix or delimiter is too large", 400)
            token = request.query_params.get("continuation-token")
            if token:
                after, skip_prefix = _cursor_decode(token, master, bucket, prefix, delimiter)
            else:
                after = request.query_params.get("start-after", "")
                skip_prefix = ""
            try:
                limit = int(request.query_params.get("max-keys", "1000"))
            except ValueError:
                return _error("InvalidArgument", "Invalid max-keys", 400)
            if limit < 1 or limit > 1000:
                return _error("InvalidArgument", "max-keys must be between 1 and 1000", 400)
            keys, common, last, skipped, more = _page(
                catalog, bucket, prefix, delimiter, after, limit, skip_prefix
            )
            next_token = _cursor_encode(master, bucket, prefix, delimiter, last, skipped) if more else None
            tags = MultipartStore(catalog)
            return Response(_list_xml(bucket, keys, common, prefix, limit,
                                      after if not token else "", delimiter, more,
                                      next_token, lambda obj: tags.object_etag(obj.id, obj.sha256)),
                            media_type="application/xml")

        if bucket not in catalog.list_buckets():
            return _error("NoSuchBucket", "The specified bucket does not exist", 404)
        if not key:
            return _error("InvalidObjectName", "Object key is empty", 400)

        if is_object_tagging:
            try:
                target = catalog.get(bucket, key)
            except FileNotFoundError:
                return _error("NoSuchKey", "Object does not exist", 404)
            tags = ObjectTagStore(catalog)
            if request.method == "GET":
                return Response(_tag_xml(tags.get(target.item.id)),
                                media_type="application/xml", status_code=200)
            if request.method == "PUT":
                tags.replace_current(bucket, key, await _tag_request_tags(request))
                return Response(status_code=200)
            if request.method == "DELETE":
                async for block in request.stream():
                    if block:
                        raise TaggingError("InvalidRequest", "DeleteObjectTagging must have no body")
                tags.replace_current(bucket, key, [])
                return Response(status_code=204)
            return _error("NotImplemented", "Unsupported object tagging method", 501)

        if mpu_start or mpu_part or mpu_complete or mpu_abort or mpu_list:
            mpu = MultipartStore(catalog)
            if mpu_start:
                upload_id = mpu.initiate(bucket, key, owner)
                return Response(_xml("InitiateMultipartUploadResult", [
                    ("Bucket", bucket), ("Key", key), ("UploadId", upload_id),
                ]), media_type="application/xml", status_code=200)
            upload_id = mpu_id
            if not upload_id:
                raise MultipartError("NoSuchUpload", "Missing UploadId", 404)
            if mpu_abort:
                mpu.abort(upload_id, bucket, key, owner)
                return Response(status_code=204)
            if mpu_list:
                rows = mpu.list_parts(upload_id, bucket, key, owner)
                try:
                    marker = int(request.query_params.get("part-number-marker", "0"))
                    limit = int(request.query_params.get("max-parts", "1000"))
                except ValueError:
                    raise MultipartError("InvalidArgument", "Invalid ListParts pagination")
                if marker < 0 or not (1 <= limit <= 1000):
                    raise MultipartError("InvalidArgument", "Invalid ListParts pagination")
                visible = [row for row in rows if row.number > marker]
                selected_parts = visible[:limit]
                result = ET.Element("ListPartsResult", xmlns=NAMESPACE)
                for title, val in (
                    ("Bucket", bucket), ("Key", key), ("UploadId", upload_id),
                    ("PartNumberMarker", marker), ("MaxParts", limit),
                    ("IsTruncated", str(len(visible) > len(selected_parts)).lower()),
                ):
                    ET.SubElement(result, title).text = str(val)
                if len(visible) > len(selected_parts):
                    ET.SubElement(result, "NextPartNumberMarker").text = str(selected_parts[-1].number)
                for part in selected_parts:
                    row = ET.SubElement(result, "Part")
                    ET.SubElement(row, "PartNumber").text = str(part.number)
                    ET.SubElement(row, "ETag").text = '"' + part.etag + '"'
                    ET.SubElement(row, "Size").text = str(part.size)
                return Response(ET.tostring(result, encoding="utf-8", xml_declaration=True),
                                media_type="application/xml")
            if mpu_complete:
                mpu.require(upload_id, bucket, key, owner, allow_completed=True)
                content = bytearray()
                async for block in request.stream():
                    content.extend(block)
                    if len(content) > 1024 * 1024:
                        raise MultipartError("MalformedXML", "Complete XML exceeds 1 MiB")
                if hashlib.sha256(content).hexdigest() != request.headers.get("x-amz-content-sha256"):
                    raise MultipartError("XAmzContentSHA256Mismatch", "Completion XML checksum mismatch")
                if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
                    raise MultipartError("MalformedXML", "XML entities are not accepted")
                try:
                    document = SafeET.fromstring(content)
                    if document.tag.rsplit("}", 1)[-1] != "CompleteMultipartUpload":
                        raise ValueError("Wrong XML root")
                    selected = []
                    for element in document:
                        if element.tag.rsplit("}", 1)[-1] != "Part":
                            raise ValueError("Unexpected XML element")
                        number = element.findtext("{*}PartNumber")
                        etag = element.findtext("{*}ETag")
                        if not number or not number.isdigit() or not etag:
                            raise ValueError("Missing part number or ETag")
                        selected.append((int(number), etag.strip('"').lower()))
                except (ValueError, ET.ParseError) as exc:
                    raise MultipartError("MalformedXML", "Invalid CompleteMultipartUpload XML") from exc
                transport = storage_bot_transport(db)
                scratch = Path(os.environ.get("TELEDRIVE_STORAGE_SCRATCH_DIR", "/data/tmp"))
                _secure_mkdir(scratch)
                engine = TelegramObjectStorage(
                    catalog.manifests, transport,
                    chunk_size=BOT_RECOMMENDED_CHUNK_SIZE, scratch_directory=scratch,
                )
                finished = await mpu.complete(upload_id, bucket, key, owner, selected, engine)
                return Response(_xml("CompleteMultipartUploadResult", [
                    ("Bucket", bucket), ("Key", key), ("ETag", '"' + finished.etag + '"'),
                ]), status_code=200, media_type="application/xml")
            # UploadPart: checksum is verified before any Telegram send.
            mpu.require(upload_id, bucket, key, owner)
            try:
                number = int(request.query_params.get("partNumber", ""))
            except ValueError:
                raise MultipartError("InvalidArgument", "Invalid part number")
            if not 1 <= number <= MAX_PARTS:
                raise MultipartError("InvalidArgument", "Invalid part number")
            scratch = Path(os.environ.get("TELEDRIVE_STORAGE_SCRATCH_DIR", "/data/tmp"))
            _secure_mkdir(scratch)
            max_bytes = int(os.environ.get("TELEDRIVE_REST_MAX_UPLOAD_BYTES", str(2 * 1024**3)))
            fd, name = tempfile.mkstemp(prefix="s3-part-", dir=scratch)
            stage = Path(name)
            try:
                sha = hashlib.sha256()
                md5 = hashlib.md5(usedforsecurity=False)
                crc = 0
                size = 0
                with os.fdopen(fd, "wb") as output:
                    async for block in request.stream():
                        size += len(block)
                        if max_bytes < 1 or size > max_bytes:
                            raise MultipartError("EntityTooLarge", "Part exceeds configured upload maximum", 413)
                        sha.update(block)
                        md5.update(block)
                        crc = zlib.crc32(block, crc)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                _verify_upload_sha256(request, sha.digest())
                checksum_crc = request.headers.get("x-amz-checksum-crc32")
                if checksum_crc and checksum_crc != base64.b64encode(
                    (crc & 0xffffffff).to_bytes(4, "big")
                ).decode():
                    raise MultipartError("BadDigest", "CRC32 checksum mismatch")
                transport = storage_bot_transport(db)
                await transport.verify_target()
                engine = TelegramObjectStorage(
                    catalog.manifests, transport,
                    chunk_size=BOT_RECOMMENDED_CHUNK_SIZE, scratch_directory=scratch,
                )
                staged = await engine.put_file(bucket, key, stage)
                tag = md5.hexdigest()
                mpu.record_part(upload_id, bucket, key, owner,
                                number=number, object_id=staged.id, etag=tag)
                return Response(status_code=200, headers={"ETag": '"' + tag + '"'})
            finally:
                stage.unlink(missing_ok=True)

        if request.method == "PUT" and request.headers.get("x-amz-copy-source") is not None:
            # The source path must be signed. Otherwise a valid PUT signature
            # could be replayed to copy a different private source object.
            if "X-Amz-Algorithm" in request.query_params:
                raise SigV4Error("NotImplemented", "Presigned CopyObject unsupported")
            authorization = request.headers.get("authorization", "")
            signed_headers = authorization.partition("SignedHeaders=")[2].split(",", 1)[0].split(";")
            source_header = request.headers["x-amz-copy-source"]
            if ("x-amz-copy-source" not in signed_headers
                    or store.resolve(owner, "s3:GetObject") is None):
                raise SigV4Error("AccessDenied", "Signed source and GetObject scope required")
            qualifiers = (
                "x-amz-copy-source-if-match", "x-amz-copy-source-if-none-match",
            )
            for condition in qualifiers:
                if request.headers.get(condition) is not None and condition not in signed_headers:
                    raise SigV4Error("AccessDenied", "Copy precondition must be signed")
            unsupported_headers = (
                "x-amz-copy-source-if-modified-since",
                "x-amz-copy-source-if-unmodified-since",
                "x-amz-copy-source-range",
                "x-amz-server-side-encryption", "x-amz-storage-class",
            )
            if any(request.headers.get(name) is not None for name in unsupported_headers):
                return _error("NotImplemented", "CopyObject option is unsupported", 501)
            if request.headers.get("x-amz-metadata-directive", "COPY") != "COPY":
                return _error("NotImplemented", "Metadata replacement is unsupported", 501)
            tag_directive = request.headers.get("x-amz-tagging-directive", "COPY")
            if tag_directive not in {"COPY", "REPLACE"}:
                raise TaggingError("InvalidArgument", "Invalid CopyObject tagging directive")
            if tag_directive == "COPY" and inline_tags is not None:
                raise TaggingError("InvalidRequest", "x-amz-tagging requires TaggingDirective=REPLACE")
            if (request.headers.get("x-amz-tagging-directive") is not None
                    and "x-amz-tagging-directive" not in signed_headers):
                raise SigV4Error("AccessDenied", "Copy tagging directive must be signed")
            # Copying nonempty source tags is a separate protected read/write
            # operation; GetObject/PutObject alone must not bypass tag scopes.
            if tag_directive == "REPLACE":
                if store.resolve(owner, "s3:PutObjectTagging") is None:
                    raise SigV4Error("AccessDenied", "Copy tag replacement needs PutObjectTagging")

            if (request.headers.get("x-amz-metadata-directive") is not None
                    and "x-amz-metadata-directive" not in signed_headers):
                raise SigV4Error("AccessDenied", "Metadata directive must be signed")
            # Both `bucket/key` (boto3) and `/bucket/key` (AWS CLI)
            # are valid CopySource header shapes. Never accept remote URLs
            # or source version selectors as though they were object keys.
            if ("?" in source_header or source_header.startswith("//")
                    or "://" in source_header):
                return _error("InvalidArgument", "Copy source must be a local bucket/object path", 400)
            original_path = unquote(
                source_header[1:] if source_header.startswith("/") else source_header
            )
            src_bucket, slash, src_key = original_path.partition("/")
            if not slash or not src_bucket or not src_key:
                return _error("InvalidArgument", "Invalid CopySource bucket/object key", 400)
            if src_bucket not in catalog.list_buckets():
                return _error("NoSuchBucket", "CopySource bucket does not exist", 404)
            try:
                source_item = catalog.get(src_bucket, src_key).item
            except FileNotFoundError:
                return _error("NoSuchKey", "CopySource object does not exist", 404)
            if tag_directive == "COPY" and ObjectTagStore(catalog).get(source_item.id):
                if (store.resolve(owner, "s3:GetObjectTagging") is None
                        or store.resolve(owner, "s3:PutObjectTagging") is None):
                    raise SigV4Error("AccessDenied", "Copying source tags needs tag read/write scopes")
            existing_tag = MultipartStore(catalog).object_etag(source_item.id, source_item.sha256)
            if_match = request.headers.get("x-amz-copy-source-if-match")
            if_none_match = request.headers.get("x-amz-copy-source-if-none-match")
            quoted_tag = '"' + existing_tag + '"'
            if (if_match is not None and if_match not in (quoted_tag, "*")
                    or if_none_match is not None and if_none_match in (quoted_tag, "*")):
                return _error("PreconditionFailed", "CopySource ETag precondition failed", 412)
            # CopyObject is a metadata-only operation and has no upload body.
            # Consume streaming requests safely rather than trusting Content-Length.
            async for block in request.stream():
                if block:
                    return _error("InvalidRequest", "CopyObject request must have no body", 400)
            try:
                result = copy_object(
                    catalog, src_bucket, src_key, bucket, key,
                    expected_source_id=source_item.id,
                    replacement_tags=(inline_tags or []) if tag_directive == "REPLACE" else None,
                )
            except CopyConflict:
                return _error("OperationAborted", "Concurrent source/destination mutation", 409)
            copied = ET.Element("CopyObjectResult", xmlns=NAMESPACE)
            ET.SubElement(copied, "LastModified").text = time.strftime(
                "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()
            )
            ET.SubElement(copied, "ETag").text = '"' + result.etag + '"'
            return Response(ET.tostring(copied, encoding="utf-8", xml_declaration=True),
                            media_type="application/xml", status_code=200)

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
                _verify_upload_sha256(request, digest.digest())
                checksum_crc = request.headers.get("x-amz-checksum-crc32")
                if checksum_crc and checksum_crc != base64.b64encode((crc & 0xffffffff).to_bytes(4, "big")).decode():
                    return _error("BadDigest", "CRC32 checksum mismatch", 400)
                engine = TelegramObjectStorage(
                    catalog.manifests, transport,
                    chunk_size=BOT_RECOMMENDED_CHUNK_SIZE, scratch_directory=root,
                )
                # PUT's tagging metadata and head pointer share one SQL
                # transaction. Failed tag writes never publish an untagged key.
                tag_store = ObjectTagStore(catalog) if inline_tags is not None else None
                item = await engine.put_file(bucket, key, stage)
                if tag_store is not None:
                    tag_store.attach_uncommitted(item.id, inline_tags)
                catalog.publish(item.id, now=int(time.time()))
                return Response(status_code=200, headers={"ETag": '"' + item.sha256 + '"'})
            finally:
                stage.unlink(missing_ok=True)

        try:
            item = catalog.get(bucket, key).item
        except FileNotFoundError:
            return _error("NoSuchKey", "The specified key does not exist", 404)
        item_etag = MultipartStore(catalog).object_etag(item.id, item.sha256)
        if request.method == "HEAD":
            return Response(status_code=200, headers=_headers(item, item_etag))
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
        headers = _headers(item, item_etag)
        headers["Content-Length"] = str(end - start)
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end-1}/{item.size}"
        return StreamingResponse(stream(), status_code=206 if partial else 200, headers=headers)
    except BatchDeleteError as exc:
        return _error(exc.code, str(exc), exc.status)
    except TaggingError as exc:
        return _error(exc.code, str(exc), exc.status)
    except MultipartError as exc:
        return _error(exc.code, str(exc), exc.status)
    except SigV4Error as exc:
        return _error(exc.code, exc.message, {
            "RequestTimeTooSkewed": 403, "NotImplemented": 501,
            "InvalidArgument": 400,
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
