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
import re
import tempfile
import time
import zlib
from pathlib import Path
from urllib.parse import unquote, parse_qsl, quote, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree as ET
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select

from teledrive.s3.credentials import S3CredentialStore, load_master_key
from teledrive.s3.route_guard import validate_s3_query, S3QueryError
from teledrive.s3.bucket_listing import list_buckets_xml, BucketListingError
from teledrive.s3.checksums import crc32c, crc64nvme
from teledrive.s3.sigv4 import SigV4Error, verify_header_signature
from teledrive.s3.post_policy import parse_form, verify_post_policy, FORM_LIMIT
from teledrive.s3.multipart import MultipartStore, MultipartError, MAX_PARTS
from teledrive.s3.batch_delete import BatchDeleteError, MAX_XML_BYTES, parse_batch, verify_body
from teledrive.s3.conditions import http_last_modified, read_precondition
from teledrive.s3.write_conditions import check_write_precondition, WritePreconditionError
from teledrive.s3.tagging import ObjectTagStore, BucketTagStore, TaggingError, validate_tags
from teledrive.s3.cors import (BucketCORSStore, CORSError, MAX_CORS_XML_BYTES,
    parse_cors_xml, cors_xml, matching_rule, response_cors_headers)
from teledrive.s3.versioning import (VersioningError, MAX_VERSIONING_XML_BYTES,
    parse_versioning_xml, versioning_xml)
from teledrive.s3.public_access import (PublicAccessError, MAX_PUBLIC_ACCESS_XML_BYTES,
    parse_public_access_xml, public_access_xml)
from teledrive.s3.lifecycle import (LifecycleError, MAX_XML as MAX_LIFECYCLE_XML,
    BucketLifecycleStore, parse_lifecycle_xml, lifecycle_xml)
from teledrive.s3.object_metadata import (
    ObjectMetadataStore, ObjectMetadataError, parse_object_metadata, signed_header_names,
    STANDARD_HEADERS,
)
from teledrive.storage.catalog import BucketCatalog, published_versions
from teledrive.storage.copy import copy_object, CopyConflict
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.manifest import ManifestStore, objects
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
    # S3 error XML is *not* in the successful-response namespace.
    # Boto3/other AWS SDK error parsers otherwise yield Error.Code == "".
    root = ET.Element("Error")
    ET.SubElement(root, "Code").text = code
    ET.SubElement(root, "Message").text = message
    return Response(ET.tostring(root, encoding="utf-8", xml_declaration=True),
                    media_type="application/xml", status_code=status)


def _headers(item, etag: str | None = None,
             metadata: dict[str, str] | None = None,
             *, checksum_mode: bool = False) -> dict[str, str]:
    headers = {
        "ETag": '"' + (etag or item.sha256) + '"',
        "Content-Length": str(item.size),
        "Accept-Ranges": "bytes",
        "Content-Type": "application/octet-stream",
        "Last-Modified": http_last_modified(item.created_at),
        "x-amz-version-id": item.id,
    }
    if checksum_mode:
        # The immutable manifest stores the digest of the FULL plaintext
        # object, independently of ETag/part layout. This is a real SHA-256,
        # not an invented checksum or AWS multipart checksum composition.
        headers["x-amz-checksum-sha256"] = base64.b64encode(
            bytes.fromhex(item.sha256)
        ).decode("ascii")
        headers["x-amz-checksum-type"] = "FULL_OBJECT"
    for name, value in (metadata or {}).items():
        # HTTP header names are case-insensitive. Do not emit both
        # Content-Type (the default) and content-type (persisted metadata):
        # SDK clients otherwise interpret them as a comma-joined MIME type.
        if name.lower() == "content-type":
            headers["Content-Type"] = value
        else:
            headers[name] = value
    return headers


def _reject_unimplemented_write_options(request: Request) -> None:
    """Fail closed for dangerous S3 semantics we cannot yet guarantee.

    In particular, acknowledging encryption, retention, ACL or a storage
    class without implementing its semantics would mislead backup clients.
    Do not stage bytes or contact Telegram when any option is present.
    """
    rejected = {
        "x-amz-grant-read", "x-amz-grant-write",
        "x-amz-grant-full-control", "x-amz-grant-read-acp",
        "x-amz-grant-write-acp",
        "x-amz-server-side-encryption",
        "x-amz-server-side-encryption-aws-kms-key-id",
        "x-amz-server-side-encryption-context",
        "x-amz-server-side-encryption-customer-algorithm",
        "x-amz-server-side-encryption-customer-key",
        "x-amz-server-side-encryption-customer-key-md5",
        "x-amz-bucket-key-enabled",
        "x-amz-website-redirect-location", "x-amz-object-lock-mode",
        "x-amz-object-lock-retain-until-date", "x-amz-object-lock-legal-hold",
        "x-amz-request-payer", "x-amz-trailer",
        "x-amz-bypass-governance-retention", "x-amz-mfa",
        "x-amz-expected-bucket-owner",
        "x-amz-decoded-content-length",
    }
    # The S3 gateway is permanently private and has exactly one storage
    # tier, so these explicit default values do not change actual behavior.
    # Reject every other canned ACL / storage-class rather than pretending
    # to provide public-read, Glacier, Standard-IA or replicated durability.
    defaults = {
        "x-amz-acl": "private",
        "x-amz-storage-class": "STANDARD",
    }
    signed = signed_header_names(request)
    for name, expected in defaults.items():
        actual = request.headers.get(name)
        if actual is None:
            continue
        if name not in signed:
            raise SigV4Error("AccessDenied", "S3 access mode must be signed")
        if actual != expected:
            raise SigV4Error("NotImplemented", "Unsupported S3 ACL or storage tier")
    if (any(name in request.headers for name in rejected)
            or request.headers.get("content-encoding", "").lower() == "aws-chunked"):
        raise SigV4Error(
            "NotImplemented",
            "S3 encryption, retention, ACL, storage class or aws-chunked semantics unsupported",
        )


def _verify_upload_sha256(
    request: Request, digest: bytes, *, md5_digest: bytes | None = None,
    sha1_digest: bytes | None = None,
    crc64nvme_value: int | None = None,
) -> None:
    """Verify *every* supplied checksum, not only the first matching digest.

    Signature verification and upload checksum validation are independent.
    A valid SHA-256 content digest must never permit a contradictory
    Content-MD5, SHA-1 or x-amz-checksum-sha256 to go unreported.
    """
    actual = base64.b64encode(digest).decode("ascii")
    sha256_header = request.headers.get("x-amz-checksum-sha256")
    if sha256_header is not None and not hmac.compare_digest(sha256_header, actual):
        raise SigV4Error("BadDigest", "SHA-256 checksum mismatch")
    md5_header = request.headers.get("content-md5")
    if md5_header is not None:
        if md5_digest is None or not hmac.compare_digest(
            md5_header, base64.b64encode(md5_digest).decode("ascii")
        ):
            raise SigV4Error("BadDigest", "Content-MD5 checksum mismatch")
    crc64_header = request.headers.get("x-amz-checksum-crc64nvme")
    if crc64_header is not None:
        if crc64nvme_value is None:
            # Never acknowledge an unverified CRC64NVME declaration.
            raise SigV4Error("NotImplemented", "CRC64NVME not verified on this operation")
        actual_crc64 = base64.b64encode(
            crc64nvme_value.to_bytes(8, "big")
        ).decode("ascii")
        if not hmac.compare_digest(crc64_header, actual_crc64):
            raise SigV4Error("BadDigest", "CRC64NVME checksum mismatch")
    sha1_header = request.headers.get("x-amz-checksum-sha1")
    if sha1_header is not None:
        if sha1_digest is None or not hmac.compare_digest(
            sha1_header, base64.b64encode(sha1_digest).decode("ascii")
        ):
            raise SigV4Error("BadDigest", "SHA-1 checksum mismatch")
    declared = request.headers.get("x-amz-sdk-checksum-algorithm")
    if declared is not None:
        allowed = {"SHA1": "x-amz-checksum-sha1", "SHA256": "x-amz-checksum-sha256",
                   "CRC32": "x-amz-checksum-crc32",
                   "CRC32C": "x-amz-checksum-crc32c",
                   "CRC64NVME": "x-amz-checksum-crc64nvme"}
        if declared not in allowed:
            raise SigV4Error("NotImplemented", "Unsupported SDK checksum algorithm")
        if request.headers.get(allowed[declared]) is None:
            raise SigV4Error("BadDigest", "Declared SDK checksum is missing")

    hexdigest = digest.hex()
    signed_content = request.headers.get("x-amz-content-sha256", "")
    presigned = "X-Amz-Algorithm" in request.query_params
    if signed_content == hexdigest:
        return
    if sha256_header is not None and hmac.compare_digest(sha256_header, actual):
        return
    if (not presigned and signed_content == "UNSIGNED-PAYLOAD"
            and md5_header is not None and md5_digest is not None):
        # Header-mode SigV4 has already authenticated the signed MD5 header.
        if "content-md5" in signed_header_names(request):
            return
    raise SigV4Error("XAmzContentSHA256Mismatch", "Signed upload checksum mismatch")


def _verify_bucket_control_payload(request: Request, body: bytes) -> None:
    """Exact signed checksum policy for small S3 bucket-control XML payloads."""
    import zlib
    for header, actual in (
        ("x-amz-checksum-crc32", zlib.crc32(body) & 0xffffffff),
        ("x-amz-checksum-crc32c", crc32c(body)),
    ):
        declared = request.headers.get(header)
        if declared is not None and not hmac.compare_digest(
            declared, base64.b64encode(actual.to_bytes(4, "big")).decode(),
        ):
            raise SigV4Error("BadDigest", "Bucket XML checksum mismatch")
    _verify_upload_sha256(
        request, hashlib.sha256(body).digest(),
        md5_digest=hashlib.md5(body, usedforsecurity=False).digest(),
        sha1_digest=hashlib.sha1(body, usedforsecurity=False).digest(),
        crc64nvme_value=crc64nvme(body),
    )


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
    return _parse_initial_tags(raw)


def _parse_initial_tags(raw: str) -> list[tuple[str, str]]:
    """Strictly decode and validate either signed header or POST form tags."""
    import re
    if len(raw.encode("utf-8")) > 8192:
        raise TaggingError("InvalidTag", "Tag payload is too large")
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


def _parse_tag_xml(body: bytes, *, bucket: bool = False) -> list[tuple[str, str]]:
    if len(body) > (256 * 1024 if bucket else 16 * 1024):
        raise TaggingError("MalformedXML", "Tagging XML exceeds maximum size")
    try:
        root = SafeET.fromstring(body)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise TaggingError("MalformedXML", "Tagging XML is malformed") from exc
    def tag_name(node):
        # Both bare AWS S3 XML and canonical 2006-03-01 namespace are
        # accepted; arbitrary XML namespaces must not bypass validation.
        name = node.tag.rsplit("}", 1)[-1]
        if node.tag not in {name, f"{{{NAMESPACE}}}{name}"}:
            raise TaggingError("MalformedXML", "Unexpected tagging XML namespace")
        return name
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
    return validate_tags(entries, max_tags=50 if bucket else 10)


async def _tag_request_tags(request: Request, *, bucket: bool = False) -> list[tuple[str, str]]:
    body = bytearray()
    async for block in request.stream():
        body.extend(block)
        if len(body) > (256 * 1024 if bucket else 16 * 1024):
            raise TaggingError("MalformedXML", "Tagging XML exceeds maximum size")
    if bucket:
        _verify_bucket_control_payload(request, bytes(body))
    else:
        _verify_upload_sha256(
            request, hashlib.sha256(body).digest(), crc64nvme_value=crc64nvme(body),
        )
        md5_header = request.headers.get("content-md5")
        if md5_header and not hmac.compare_digest(
            md5_header,
            base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode("ascii")
        ):
            raise TaggingError("BadDigest", "Content-MD5 checksum mismatch")
    return _parse_tag_xml(bytes(body), bucket=bucket)


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
              more: bool, next_token: str | None, etag_for=None, *,
              version_one: bool = False, next_marker: str | None = None,
              encoding_type: str | None = None) -> bytes:
    root = ET.Element("ListBucketResult", xmlns=NAMESPACE)
    # S3 EncodingType=url percent-encodes UTF-8 keys *inside XML* so names
    # containing XML control characters remain safely representable.
    encoded = lambda value: quote(value, safe="") if encoding_type == "url" else value
    fields = [("Name", bucket), ("Prefix", encoded(prefix)),
              ("MaxKeys", limit), ("IsTruncated", "true" if more else "false")]
    if not version_one:
        fields.append(("KeyCount", len(keys) + len(common)))
    for field, val in fields:
        ET.SubElement(root, field).text = str(val)
    if delimiter:
        ET.SubElement(root, "Delimiter").text = encoded(delimiter)
    if encoding_type:
        ET.SubElement(root, "EncodingType").text = encoding_type
    if version_one:
        ET.SubElement(root, "Marker").text = encoded(after)
    elif after:
        ET.SubElement(root, "StartAfter").text = encoded(after)
    for item in keys:
        c = ET.SubElement(root, "Contents")
        for field, value in (
            ("Key", encoded(item.item.key)),
            ("LastModified", time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(item.item.created_at))),
            ("ETag", '"' + (etag_for(item.item) if etag_for else item.item.sha256) + '"'),
            ("Size", item.item.size),
            ("StorageClass", "STANDARD"),
        ):
            ET.SubElement(c, field).text = str(value)
    for name in common:
        ET.SubElement(ET.SubElement(root, "CommonPrefixes"), "Prefix").text = encoded(name)
    if next_token:
        ET.SubElement(root, "NextContinuationToken").text = next_token
    if version_one and next_marker:
        ET.SubElement(root, "NextMarker").text = encoded(next_marker)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _virtual_host_bucket(request: Request) -> str | None:
    """Use Host only when a public DNS suffix was explicitly configured.

    SigV4 must authenticate the exact Host afterwards; never trust proxy
    forwarding headers for bucket selection. The default remains path-style.
    """
    suffix = os.environ.get("TELEDRIVE_S3_VHOST_SUFFIX", "").strip().lower().strip(".")
    if not suffix:
        return None
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if len(suffix) > 253 or re.fullmatch(label + r"(?:\." + label + r")+", suffix) is None:
        raise SigV4Error("InvalidArgument", "Invalid virtual-host DNS suffix")
    authority = request.headers.get("host", "").lower()
    if authority.count(":") == 1 and authority.rsplit(":", 1)[1].isdigit():
        hostname = authority.rsplit(":", 1)[0]
    else:
        hostname = authority
    ending = "." + suffix
    if not hostname.endswith(ending):
        return None
    bucket = hostname[:-len(ending)]
    try:
        BucketCatalog.validate_bucket(bucket)
    except ValueError as exc:
        raise SigV4Error("InvalidArgument", "Invalid virtual-host bucket") from exc
    return bucket


async def _handle(request: Request):
    db = None
    retain = False
    try:
        raw_path = request.scope.get("raw_path", b"/").decode("ascii")
        logical = unquote(raw_path.lstrip("/"))
        virtual_bucket = _virtual_host_bucket(request)
        if virtual_bucket is not None:
            bucket, separator, key = virtual_bucket, "/" if logical else "", logical
        else:
            bucket, separator, key = logical.partition("/")
        validate_s3_query(request.method, bucket, bool(separator), request.query_params)
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
        form_upload = bool(bucket and not separator and request.method == "POST"
                           and not request.query_params)
        multi_delete = bool(bucket and not separator and request.method == "POST"
                            and "delete" in request.query_params)
        if "delete" in request.query_params and not multi_delete:
            return _error("NotImplemented", "Unsupported DeleteObjects route", 501)
        if request.method in ("PUT", "DELETE") or (
            request.method == "POST" and separator and request.query_params.get("uploadId")
        ):
            if any(name in request.headers for name in ("if-modified-since", "if-unmodified-since")):
                return _error("NotImplemented", "Conditional write dates are unsupported", 501)
            has_write_cond = any(name in request.headers for name in ("if-match", "if-none-match"))
            # SigV4 presigned URLs carry authentication query fields, not
            # operation selectors. Permit these to use the same signed CAS.
            auth_only_query = all(name.startswith("X-Amz-")
                                  for name in request.query_params)
            simple_object = bool(request.method in ("PUT", "DELETE")
                                 and separator and auth_only_query
                                 and not request.headers.get("x-amz-copy-source"))
            multipart_completion = bool(
                request.method == "POST" and separator
                and request.query_params.get("uploadId")
                and all(k == "uploadId" or k.startswith("X-Amz-")
                        for k in request.query_params)
            )
            if has_write_cond and not (simple_object or multipart_completion):
                return _error("NotImplemented", "Unsupported conditional S3 operation", 501)
        is_version_read = bool(
            separator and request.method in ("GET", "HEAD")
            and "versionId" in request.query_params
            and all(
                key_name == "versionId"
                or (key_name == "attributes" and "attributes" in request.query_params
                    and request.method == "GET")
                or key_name.startswith("X-Amz-")
                for key_name in request.query_params
            )
        )
        if is_version_read and len(request.query_params.getlist("versionId")) != 1:
            return _error("InvalidArgument", "Ambiguous version ID", 400)
        is_version_delete = bool(
            separator and request.method == "DELETE"
            and "versionId" in request.query_params
            and "tagging" not in request.query_params
        )
        if is_version_delete and (
            len(request.query_params.getlist("versionId")) != 1
            or any(k != "versionId" and not k.startswith("X-Amz-")
                   for k in request.query_params)
        ):
            return _error("InvalidArgument", "Ambiguous VersionId deletion selector", 400)
        is_version_listing = bool(
            bucket and not separator and request.method == "GET"
            and "versions" in request.query_params
        )
        if is_version_listing and any(
            item not in {"versions", "prefix", "max-keys", "key-marker",
                         "version-id-marker"} and not item.startswith("X-Amz-")
            for item in request.query_params
        ):
            return _error("NotImplemented", "Unsupported ListObjectVersions parameter", 501)
        mpu_bucket_list = bool(bucket and not separator and "uploads" in request.query_params
                               and request.method == "GET")
        mpu_start = bool(separator and "uploads" in request.query_params and request.method == "POST")
        mpu_id = request.query_params.get("uploadId") if separator else None
        mpu_part = bool(mpu_id and request.method == "PUT" and "partNumber" in request.query_params)
        mpu_complete = bool(mpu_id and request.method == "POST")
        mpu_abort = bool(mpu_id and request.method == "DELETE")
        mpu_list = bool(mpu_id and request.method == "GET")
        is_object_attributes = bool(separator and "attributes" in request.query_params)
        is_object_tagging = bool(separator and "tagging" in request.query_params)
        is_version_tagging = bool(is_object_tagging and "versionId" in request.query_params)
        if is_version_tagging and (
            len(request.query_params.getlist("versionId")) != 1
            or any(name not in {"tagging", "versionId"} and not name.startswith("X-Amz-")
                   for name in request.query_params)
        ):
            return _error("InvalidArgument", "Ambiguous versioned tagging selector", 400)
        is_bucket_tagging = bool(bucket and not separator and "tagging" in request.query_params)
        is_bucket_cors = bool(bucket and not separator and "cors" in request.query_params)
        is_bucket_versioning = bool(bucket and not separator and "versioning" in request.query_params)
        is_public_access_block = bool(bucket and not separator and "publicAccessBlock" in request.query_params)
        is_bucket_lifecycle = bool(bucket and not separator and "lifecycle" in request.query_params)
        is_bucket_policy_status = bool(bucket and not separator and "policyStatus" in request.query_params)
        if is_bucket_lifecycle and (
            len(request.query_params.getlist("lifecycle")) != 1
            or request.query_params.get("lifecycle") != ""
            or any(k != "lifecycle" and not k.startswith("X-Amz-")
                   for k in request.query_params)
        ):
            return _error("InvalidArgument", "Ambiguous lifecycle selector", 400)
        if is_public_access_block and (
            len(request.query_params.getlist("publicAccessBlock")) != 1
            or request.query_params.get("publicAccessBlock") != ""
            or any(name != "publicAccessBlock" and not name.startswith("X-Amz-")
                   for name in request.query_params)
        ):
            return _error("InvalidArgument", "Invalid public access control selector", 400)
        if is_bucket_versioning and (
            len(request.query_params.getlist("versioning")) != 1
            or request.query_params.get("versioning") != ""
            or any(name != "versioning" and not name.startswith("X-Amz-")
                   for name in request.query_params)
        ):
            return _error("InvalidArgument", "Invalid bucket versioning selector", 400)
        if (is_object_tagging or is_bucket_tagging) and any(
            k in request.query_params for k in ("uploads", "uploadId")
        ):
            return _error("NotImplemented", "Tagging with multipart selectors unsupported", 501)
        if is_bucket_tagging and "versionId" in request.query_params:
            return _error("NotImplemented", "Bucket tags cannot select object versions", 501)
        if is_object_attributes:
            if request.method != "GET" or any(
                p in request.query_params for p in ("uploadId", "uploads", "tagging")
            ):
                return _error("NotImplemented", "Unsupported GetObjectAttributes variant", 501)
            action = "s3:GetObjectAttributes"
        elif form_upload:
            action = "s3:PutObject"
        elif multi_delete:
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
        elif is_bucket_cors:
            action = {
                "GET": "s3:GetBucketCORS",
                "PUT": "s3:PutBucketCORS",
                "DELETE": "s3:PutBucketCORS",
            }.get(request.method)
        elif is_bucket_versioning:
            action = {
                "GET": "s3:GetBucketVersioning",
                "PUT": "s3:PutBucketVersioning",
            }.get(request.method)
        elif is_public_access_block:
            action = {
                "GET": "s3:GetBucketPublicAccessBlock",
                "PUT": "s3:PutBucketPublicAccessBlock",
            }.get(request.method)
        elif is_bucket_lifecycle:
            action = {
                "GET": "s3:GetLifecycleConfiguration",
                "PUT": "s3:PutLifecycleConfiguration",
                "DELETE": "s3:PutLifecycleConfiguration",
            }.get(request.method)
        elif is_bucket_policy_status:
            action = "s3:GetBucketPolicyStatus" if request.method == "GET" else None
        elif bucket and not separator and "location" in request.query_params:
            action = "s3:GetBucketLocation" if request.method == "GET" else None
        elif mpu_start or mpu_part or mpu_complete:
            action = "s3:PutObject"
        elif is_version_listing:
            action = "s3:ListBucketVersions"
        elif mpu_bucket_list:
            action = "s3:ListBucket"
        elif mpu_abort:
            action = "s3:DeleteObject"
        elif mpu_list:
            action = "s3:GetObject"
        elif "uploads" in request.query_params or "uploadId" in request.query_params:
            return _error("NotImplemented", "This multipart operation is unsupported", 501)
        # A selector on an object path must never silently degrade to a
        # regular object operation (particularly an unexpected DELETE).
        if "lifecycle" in request.query_params and not is_bucket_lifecycle:
            return _error("NotImplemented", "Lifecycle controls require bucket route", 501)
        if "publicAccessBlock" in request.query_params and not is_public_access_block:
            return _error("NotImplemented", "Public access control is bucket-only", 501)
        if "versioning" in request.query_params and not is_bucket_versioning:
            return _error("NotImplemented", "Bucket versioning is a bucket-only operation", 501)
        if action is None or (separator and "partNumber" in request.query_params and not mpu_part):
            return _error("NotImplemented", "This S3 operation is not supported yet", 501)
        # Unknown control-plane query operations must not be misinterpreted as
        # ordinary object reads/writes (e.g. ?tagging, ?versioning, ?acl).
        unsupported = {
            "acl", "versions", "policy",
            "notification", "replication",
            "retention", "legal-hold", "logging",
            "encryption", "object-lock", "versionId",
        }
        if any(parameter in request.query_params and (
            (parameter != "versionId" or not (is_version_read or is_version_tagging or is_version_delete))
            and (parameter != "versions" or not is_version_listing)
        ) for parameter in unsupported):
            return _error("NotImplemented", "This S3 extension is not implemented", 501)

        master = load_master_key()
        db = db_connect()
        store = S3CredentialStore(db, master)
        if form_upload:
            if request.headers.get("authorization"):
                raise SigV4Error("AccessDenied", "Ambiguous form and header authentication")
            _reject_unimplemented_write_options(request)
            max_upload = int(os.environ.get("TELEDRIVE_REST_MAX_UPLOAD_BYTES", str(2 * 1024**3)))
            form_limit = min(FORM_LIMIT, max_upload)
            if form_limit < 1:
                return _error("EntityTooLarge", "Form upload size is disabled", 413)
            form_bytes = bytearray()
            async for block in request.stream():
                form_bytes.extend(block)
                if len(form_bytes) > form_limit:
                    return _error("EntityTooLarge", "Browser upload form too large", 413)
            post_fields, payload = parse_form(
                request.headers.get("content-type", ""), bytes(form_bytes),
            )
            post_key = verify_post_policy(
                post_fields, payload, bucket, store.resolve, max_bytes=form_limit,
            )
            # Signed form fields are validated using the same bounded
            # metadata contract as ordinary S3 PUT. Browser-controlled MIME,
            # cache rules and x-amz-meta-* values must not be silently
            # accepted without durable storage.
            post_headers = {
                name.lower(): value for name, value in post_fields.items()
                if name.lower() in STANDARD_HEADERS
                or name.startswith("x-amz-meta-")
            }
            post_metadata = parse_object_metadata(
                post_headers, set(post_headers),
            )
            # Tagging is a separate S3 capability, not included in generic
            # PutObject credentials. Enforce BOTH privileges before sending
            # even a single byte, then publish tags in the HEAD transaction.
            form_tags = None
            if "x-amz-tagging" in post_fields:
                credential_access = post_fields["x-amz-credential"].split("/", 1)[0]
                if store.resolve(credential_access, "s3:PutObjectTagging") is None:
                    raise SigV4Error("AccessDenied", "PutObjectTagging permission required")
                form_tags = _parse_initial_tags(post_fields["x-amz-tagging"])
            post_catalog = BucketCatalog(ManifestStore(db))
            if bucket not in post_catalog.list_buckets():
                return _error("NoSuchBucket", "Upload bucket does not exist", 404)
            directory = Path(os.environ.get("TELEDRIVE_STORAGE_SCRATCH_DIR", "/data/tmp"))
            _secure_mkdir(directory)
            fd, temp_name = tempfile.mkstemp(prefix="s3-form-", dir=directory)
            staged = Path(temp_name)
            try:
                with os.fdopen(fd, "wb") as dest:
                    dest.write(payload)
                    dest.flush()
                    os.fsync(dest.fileno())
                transport = storage_bot_transport(db)
                await transport.verify_target()
                obj = await TelegramObjectStorage(
                    post_catalog.manifests, transport,
                    chunk_size=BOT_RECOMMENDED_CHUNK_SIZE,
                    scratch_directory=directory,
                ).put_file(bucket, post_key, staged)
                ObjectMetadataStore(post_catalog).attach_uncommitted(
                    obj.id, post_metadata,
                )
                if form_tags is not None:
                    ObjectTagStore(post_catalog).attach_uncommitted(
                        obj.id, form_tags,
                    )
                post_catalog.publish(obj.id, now=int(time.time()))
                etag = '"' + obj.sha256 + '"'
                response_headers = {"ETag": etag, "x-amz-version-id": obj.id}
                # A redirect is an explicit signed browser instruction. The
                # gateway never contacts the target and does not disclose
                # credentials or original payload in the callback parameters.
                redirect = post_fields.get("success_action_redirect")
                if redirect is not None:
                    parsed_redirect = urlsplit(redirect)  # verified HTTPS URL
                    parameters = urlencode({
                        "bucket": bucket, "key": post_key, "etag": etag,
                    })
                    redirect_query = (parsed_redirect.query + "&" + parameters
                                      if parsed_redirect.query else parameters)
                    location = urlunsplit((
                        parsed_redirect.scheme, parsed_redirect.netloc,
                        parsed_redirect.path, redirect_query, "",
                    ))
                    return Response(status_code=303, headers={
                        **response_headers, "Location": location,
                    })
                # The requested response status is itself an exact signed
                # policy condition; no unsigned status override is accepted.
                status = int(post_fields.get("success_action_status", "204"))
                if status == 201:
                    # Quote untrusted key text before placing it in Location.
                    # The XML serializer escapes the same key in the body.
                    location = (str(request.base_url).rstrip("/")
                                + "/" + quote(bucket, safe="")
                                + "/" + quote(post_key, safe="/"))
                    response_headers["Location"] = location
                    return Response(_xml("PostResponse", [
                        ("Location", location), ("Bucket", bucket),
                        ("Key", post_key), ("ETag", etag),
                    ]), media_type="application/xml", status_code=201,
                        headers=response_headers)
                return Response(status_code=status, headers=response_headers)
            finally:
                staged.unlink(missing_ok=True)
        owner = verify_header_signature(request, store.resolve, action)
        # Every signed S3 method must reject unsupported retention, SSE,
        # requester-payer and governance semantics, including GET/HEAD/DELETE.
        # Ignoring those headers can turn a supposedly protected delete into
        # an ordinary tombstone or pretend to verify a requested checksum.
        _reject_unimplemented_write_options(request)
        if not is_object_attributes and any(
            name in request.headers for name in
            ("x-amz-max-parts", "x-amz-part-number-marker")
        ):
            raise SigV4Error(
                "NotImplemented", "Multipart attributes headers require GetObjectAttributes"
            )
        checksum_mode = request.headers.get("x-amz-checksum-mode")
        if checksum_mode is not None:
            # AWS CLI/botocore send ENABLED by default for standard downloads.
            # Only object GET/HEAD can use the manifest's full-object digest.
            if (request.method not in {"GET", "HEAD"} or not separator
                    or is_object_attributes or is_object_tagging
                    or mpu_list or mpu_abort or mpu_part or mpu_complete):
                raise SigV4Error(
                    "NotImplemented", "ChecksumMode unsupported on this S3 operation",
                )
            if checksum_mode != "ENABLED":
                raise SigV4Error("InvalidArgument", "Unknown checksum mode")
        if request.method == "DELETE" and any(
            name in request.headers for name in ("x-amz-copy-source", "x-amz-tagging")
        ):
            raise SigV4Error("NotImplemented", "Unsupported S3 delete headers")
        inline_tags = _initial_tags(request, owner, store) if (
            separator and request.method == "PUT" and not is_object_tagging
        ) else None
        catalog = BucketCatalog(ManifestStore(db))

        if not bucket:
            return Response(
                list_buckets_xml(catalog, request.query_params, master, owner),
                media_type="application/xml",
            )

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
                        marker = catalog.delete_marker(bucket, key_to_delete)
                    except ValueError:
                        problem = ET.SubElement(result, "Error")
                        ET.SubElement(problem, "Key").text = key_to_delete
                        ET.SubElement(problem, "Code").text = "InvalidObjectName"
                        ET.SubElement(problem, "Message").text = "Invalid object name"
                        continue
                    if not batch.quiet:
                        deleted = ET.SubElement(result, "Deleted")
                        ET.SubElement(deleted, "Key").text = key_to_delete
                        ET.SubElement(deleted, "VersionId").text = marker
                        ET.SubElement(deleted, "DeleteMarker").text = "true"
                        ET.SubElement(deleted, "DeleteMarkerVersionId").text = marker
                return Response(ET.tostring(result, encoding="utf-8", xml_declaration=True),
                                media_type="application/xml", status_code=200)
            if is_bucket_policy_status:
                if bucket not in catalog.list_buckets():
                    return _error("NoSuchBucket", "Bucket does not exist", 404)
                # Public grants and bucket policies cannot be enabled through
                # this private-only gateway. This is NOT a bucket policy engine.
                return Response(_xml("PolicyStatus", [("IsPublic", "false")]),
                                media_type="application/xml")
            if is_bucket_lifecycle:
                if bucket not in catalog.list_buckets():
                    return _error("NoSuchBucket", "Bucket not found", 404)
                rules = BucketLifecycleStore(catalog)
                if request.method == "GET":
                    value = rules.get(bucket)
                    if value is None:
                        return _error("NoSuchLifecycleConfiguration", "No lifecycle configuration", 404)
                    return Response(lifecycle_xml(value), media_type="application/xml")
                if request.method == "PUT":
                    data = bytearray()
                    async for block in request.stream():
                        data.extend(block)
                        if len(data) > MAX_LIFECYCLE_XML:
                            raise LifecycleError("MalformedXML", "Lifecycle XML exceeds 32 KiB")
                    _verify_bucket_control_payload(request, bytes(data))
                    rules.replace(bucket, parse_lifecycle_xml(bytes(data)))
                    return Response(status_code=200)
                if request.method == "DELETE":
                    async for block in request.stream():
                        if block:
                            raise LifecycleError("InvalidRequest", "DeleteBucketLifecycle cannot have body")
                    rules.replace(bucket, None)
                    return Response(status_code=204)
                return _error("NotImplemented", "Unsupported lifecycle method", 501)
            if is_public_access_block:
                if bucket not in catalog.list_buckets():
                    return _error("NoSuchBucket", "Bucket does not exist", 404)
                if request.method == "GET":
                    return Response(public_access_xml(), media_type="application/xml")
                if request.method == "PUT":
                    payload = bytearray()
                    async for block in request.stream():
                        payload.extend(block)
                        if len(payload) > MAX_PUBLIC_ACCESS_XML_BYTES:
                            raise PublicAccessError("MalformedXML",
                                                    "Public access XML exceeds 16 KiB")
                    _verify_bucket_control_payload(request, bytes(payload))
                    parse_public_access_xml(bytes(payload))
                    return Response(status_code=200)
                return _error("NotImplemented", "Unsupported public access method", 501)
            if is_bucket_versioning:
                if bucket not in catalog.list_buckets():
                    return _error("NoSuchBucket", "Bucket does not exist", 404)
                if request.method == "GET":
                    return Response(versioning_xml(), media_type="application/xml")
                if request.method == "PUT":
                    payload = bytearray()
                    async for block in request.stream():
                        payload.extend(block)
                        if len(payload) > MAX_VERSIONING_XML_BYTES:
                            raise VersioningError("MalformedXML",
                                                  "Versioning XML exceeds 16 KiB")
                    _verify_bucket_control_payload(request, bytes(payload))
                    parse_versioning_xml(bytes(payload))
                    return Response(status_code=200)
                return _error("NotImplemented", "Unsupported bucket versioning method", 501)
            if is_bucket_cors:
                if bucket not in catalog.list_buckets():
                    return _error("NoSuchBucket", "Bucket does not exist", 404)
                cors_store = BucketCORSStore(catalog)
                if request.method == "GET":
                    rules = cors_store.get(bucket)
                    if rules is None:
                        return _error("NoSuchCORSConfiguration", "No CORS configuration", 404)
                    return Response(cors_xml(rules), media_type="application/xml")
                if request.method == "PUT":
                    body = bytearray()
                    async for block in request.stream():
                        body.extend(block)
                        if len(body) > MAX_CORS_XML_BYTES:
                            raise CORSError("MalformedXML", "CORS XML exceeds 64 KiB")
                    _verify_upload_sha256(
                        request, hashlib.sha256(body).digest(),
                        crc64nvme_value=crc64nvme(body),
                    )
                    md5_header = request.headers.get("content-md5")
                    if md5_header and not hmac.compare_digest(
                        md5_header,
                        base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode()
                    ):
                        raise CORSError("BadDigest", "Content-MD5 mismatch")
                    cors_store.replace(bucket, parse_cors_xml(bytes(body)))
                    return Response(status_code=200)
                if request.method == "DELETE":
                    async for block in request.stream():
                        if block:
                            raise CORSError("InvalidRequest", "DeleteBucketCors must not have a body")
                    cors_store.replace(bucket, [])
                    return Response(status_code=204)
                return _error("NotImplemented", "Unsupported bucket CORS operation", 501)
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
                    tagstore.replace(bucket, await _tag_request_tags(request, bucket=True))
                    return Response(status_code=200)
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
            if "location" in request.query_params:
                if request.method != "GET":
                    return _error("MethodNotAllowed", "GetBucketLocation requires GET", 405)
                if bucket not in catalog.list_buckets():
                    return _error("NoSuchBucket", "The specified bucket does not exist", 404)
                # AWS represents us-east-1 as an empty LocationConstraint.
                root = ET.Element("LocationConstraint", xmlns=NAMESPACE)
                return Response(
                    ET.tostring(root, encoding="utf-8", xml_declaration=True),
                    media_type="application/xml",
                )
            if is_version_listing:
                if len(request.query_params.getlist("versions")) != 1 or (
                    request.query_params.get("versions") not in ("",)
                ):
                    return _error("InvalidArgument", "Invalid versions selector", 400)
                for marker in ("prefix", "max-keys", "key-marker", "version-id-marker"):
                    if len(request.query_params.getlist(marker)) > 1:
                        return _error("InvalidArgument", "Ambiguous version listing parameter", 400)
                try:
                    maximum = int(request.query_params.get("max-keys", "1000"))
                    key_marker = request.query_params.get("key-marker", "")
                    version_marker = request.query_params.get("version-id-marker", "")
                    prefix = request.query_params.get("prefix", "")
                    entries, has_more = catalog.list_bucket_versions(
                        bucket, prefix=prefix, key_marker=key_marker,
                        version_marker=version_marker, limit=maximum,
                    )
                except ValueError:
                    return _error("InvalidArgument", "Invalid version list cursor or limit", 400)
                root = ET.Element("ListVersionsResult", xmlns=NAMESPACE)
                for label, value in (
                    ("Name", bucket), ("Prefix", prefix),
                    ("KeyMarker", key_marker), ("VersionIdMarker", version_marker),
                    ("MaxKeys", maximum), ("IsTruncated", str(has_more).lower()),
                ):
                    ET.SubElement(root, label).text = str(value)
                if has_more and entries:
                    ET.SubElement(root, "NextKeyMarker").text = entries[-1]["key"]
                    ET.SubElement(root, "NextVersionIdMarker").text = entries[-1]["version_id"]
                mpu = MultipartStore(catalog)
                for entry in entries:
                    element = ET.SubElement(
                        root, "DeleteMarker" if entry["is_delete_marker"] else "Version"
                    )
                    fields = [
                        ("Key", entry["key"]), ("VersionId", entry["version_id"]),
                        ("IsLatest", str(entry["is_current"]).lower()),
                        ("LastModified", time.strftime(
                            "%Y-%m-%dT%H:%M:%S.000Z",
                            time.gmtime(entry["committed_ns"] // 1_000_000_000),
                        )),
                    ]
                    if not entry["is_delete_marker"]:
                        fields += [
                            ("ETag", '"' + mpu.object_etag(
                                entry["version_id"], entry["sha256"]
                            ) + '"'),
                            ("Size", entry["size"]), ("StorageClass", "STANDARD"),
                        ]
                    for label, value in fields:
                        ET.SubElement(element, label).text = str(value)
                return Response(
                    ET.tostring(root, encoding="utf-8", xml_declaration=True),
                    media_type="application/xml",
                )
            if mpu_bucket_list:
                query = request.query_params
                prefix = query.get("prefix", "")
                delimiter = query.get("delimiter", "")
                encoding = query.get("encoding-type")
                if encoding not in (None, "url"):
                    return _error("InvalidArgument", "Only encoding-type=url is supported", 400)
                if len(prefix.encode("utf-8")) > 1024 or len(delimiter.encode("utf-8")) > 16:
                    return _error("InvalidArgument", "Multipart prefix/delimiter too long", 400)
                try:
                    limit = int(query.get("max-uploads", "1000"))
                except ValueError:
                    return _error("InvalidArgument", "Invalid max-uploads", 400)
                if not 1 <= limit <= 1000:
                    return _error("InvalidArgument", "max-uploads must be 1..1000", 400)
                marker_key = query.get("key-marker", "")
                marker_id = query.get("upload-id-marker", "")
                if marker_id and not marker_key:
                    return _error("InvalidArgument", "Upload-id marker requires key marker", 400)
                multipart = MultipartStore(catalog)
                if delimiter:
                    entries, more, next_marker = multipart.list_uploads_grouped(
                        bucket, owner, prefix=prefix, delimiter=delimiter,
                        after_key=marker_key, after_id=marker_id, limit=limit,
                    )
                else:
                    records, more = multipart.list_uploads(
                        bucket, owner, prefix=prefix, after_key=marker_key,
                        after_id=marker_id, limit=limit,
                    )
                    entries = [("upload", record) for record in records]
                    next_marker = ((records[-1].object_key, records[-1].id)
                                   if records else ("", ""))

                encode = (lambda value: quote(value, safe="/") if encoding == "url" else value)
                root = ET.Element("ListMultipartUploadsResult", xmlns=NAMESPACE)
                for label, val in (
                    ("Bucket", bucket), ("KeyMarker", encode(marker_key)),
                    ("UploadIdMarker", marker_id), ("Prefix", encode(prefix)),
                    ("MaxUploads", limit), ("IsTruncated", str(more).lower()),
                ):
                    ET.SubElement(root, label).text = str(val)
                if delimiter:
                    ET.SubElement(root, "Delimiter").text = encode(delimiter)
                if encoding:
                    ET.SubElement(root, "EncodingType").text = encoding
                if more:
                    ET.SubElement(root, "NextKeyMarker").text = encode(next_marker[0])
                    ET.SubElement(root, "NextUploadIdMarker").text = next_marker[1]
                for kind, value in entries:
                    if kind == "prefix":
                        ET.SubElement(
                            ET.SubElement(root, "CommonPrefixes"), "Prefix"
                        ).text = encode(value)
                        continue
                    item = ET.SubElement(root, "Upload")
                    for label, contents in (
                        ("Key", encode(value.object_key)), ("UploadId", value.id),
                        ("StorageClass", "STANDARD"),
                        ("Initiated", time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                                    time.gmtime(value.created_at))),
                    ):
                        ET.SubElement(item, label).text = str(contents)
                return Response(ET.tostring(root, encoding="utf-8", xml_declaration=True),
                                media_type="application/xml")
            list_type = request.query_params.get("list-type")
            if list_type not in (None, "2"):
                return _error("NotImplemented", "Only ListObjectsV1/V2 are supported", 501)
            version_one = list_type is None
            prefix = request.query_params.get("prefix", "")
            delimiter = request.query_params.get("delimiter", "")
            encoding_type = request.query_params.get("encoding-type")
            if encoding_type not in (None, "url"):
                return _error("InvalidArgument", "Unsupported list encoding-type", 400)
            if len(prefix.encode("utf-8")) > 1024 or len(delimiter.encode("utf-8")) > 16:
                return _error("InvalidArgument", "Prefix or delimiter is too large", 400)
            if version_one:
                if any(p in request.query_params for p in ("continuation-token", "start-after")):
                    return _error("InvalidArgument", "V2 cursor is not a V1 marker", 400)
                after = request.query_params.get("marker", "")
                # V1 NextMarker may denote a CommonPrefix. Skip every
                # descendant of that prefix instead of returning it again.
                skip_prefix = after if delimiter and after.endswith(delimiter) else ""
                token = None
            else:
                if "marker" in request.query_params:
                    return _error("InvalidArgument", "V1 marker cannot be used with V2", 400)
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
            next_token = (_cursor_encode(master, bucket, prefix, delimiter, last, skipped)
                          if more and not version_one else None)
            next_marker = (max([item.item.key for item in keys] + common)
                           if more and version_one and (keys or common) else None)
            tags = MultipartStore(catalog)
            return Response(_list_xml(bucket, keys, common, prefix, limit,
                                      after if version_one or not token else "",
                                      delimiter, more, next_token,
                                      lambda obj: tags.object_etag(obj.id, obj.sha256),
                                      version_one=version_one, next_marker=next_marker,
                                      encoding_type=encoding_type),
                            media_type="application/xml")

        if bucket not in catalog.list_buckets():
            return _error("NoSuchBucket", "The specified bucket does not exist", 404)
        if not key:
            return _error("InvalidObjectName", "Object key is empty", 400)

        if is_object_tagging:
            if is_version_tagging:
                selected_id = request.query_params.get("versionId", "")
                if not selected_id or len(selected_id) > 80:
                    return _error("InvalidArgument", "Invalid object VersionId", 400)
                if not catalog.is_visible_version(bucket, key, selected_id):
                    return _error("NoSuchVersion", "Object version is not published", 404)
                try:
                    version_item = catalog.manifests.get(selected_id)
                except FileNotFoundError:
                    return _error("NoSuchVersion", "Object version is not ready", 404)
                if version_item.bucket != bucket or version_item.key != key:
                    return _error("NoSuchVersion", "Version ownership mismatch", 404)
                target_id = selected_id
            else:
                try:
                    target_id = catalog.get(bucket, key).item.id
                except FileNotFoundError:
                    return _error("NoSuchKey", "Object does not exist", 404)
            tags = ObjectTagStore(catalog)
            if request.method == "GET":
                return Response(_tag_xml(tags.get(target_id)),
                                media_type="application/xml", status_code=200,
                                headers={"x-amz-version-id": target_id})
            if request.method == "PUT":
                parsed = await _tag_request_tags(request)
                if is_version_tagging:
                    tags.replace_published(bucket, key, target_id, parsed)
                else:
                    tags.replace_current(bucket, key, parsed)
                return Response(status_code=200, headers={"x-amz-version-id": target_id})
            if request.method == "DELETE":
                async for block in request.stream():
                    if block:
                        raise TaggingError("InvalidRequest", "DeleteObjectTagging must have no body")
                if is_version_tagging:
                    tags.replace_published(bucket, key, target_id, [])
                else:
                    tags.replace_current(bucket, key, [])
                return Response(status_code=204, headers={"x-amz-version-id": target_id})
            return _error("NotImplemented", "Unsupported object tagging method", 501)

        if mpu_start or mpu_part or mpu_complete or mpu_abort or mpu_list:
            mpu = MultipartStore(catalog)
            if mpu_start:
                # Initiate is metadata-only. Reject any supplied body before
                # allocating a durable multipart session, including if a
                # leaked presigned URL is replayed with altered content.
                async for block in request.stream():
                    if block:
                        raise MultipartError(
                            "InvalidRequest", "CreateMultipartUpload requires an empty body"
                        )
                http_properties = parse_object_metadata(
                    request.headers, signed_header_names(request),
                )
                upload_id = mpu.initiate(
                    bucket, key, owner, http_metadata=http_properties,
                )
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
                try:
                    raw_marker = request.query_params.get("part-number-marker", "0")
                    raw_limit = request.query_params.get("max-parts", "1000")
                    if (re.fullmatch(r"[0-9]{1,5}", raw_marker) is None
                            or re.fullmatch(r"[0-9]{1,4}", raw_limit) is None):
                        raise ValueError("Invalid decimal")
                    marker = int(raw_marker)
                    limit = int(raw_limit)
                except ValueError:
                    raise MultipartError("InvalidArgument", "Invalid ListParts pagination")
                selected_parts, more = mpu.list_parts_page(
                    upload_id, bucket, key, owner, marker=marker, limit=limit,
                )
                result = ET.Element("ListPartsResult", xmlns=NAMESPACE)
                for title, val in (
                    ("Bucket", bucket), ("Key", key), ("UploadId", upload_id),
                    ("PartNumberMarker", marker), ("MaxParts", limit),
                    ("StorageClass", "STANDARD"),
                    ("IsTruncated", str(more).lower()),
                ):
                    ET.SubElement(result, title).text = str(val)
                if more:
                    ET.SubElement(result, "NextPartNumberMarker").text = str(
                        selected_parts[-1].number
                    )
                for part in selected_parts:
                    row = ET.SubElement(result, "Part")
                    ET.SubElement(row, "PartNumber").text = str(part.number)
                    ET.SubElement(row, "ETag").text = '"' + part.etag + '"'
                    ET.SubElement(row, "LastModified").text = time.strftime(
                        "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(part.updated_at),
                    )
                    ET.SubElement(row, "Size").text = str(part.size)
                return Response(ET.tostring(result, encoding="utf-8", xml_declaration=True),
                                media_type="application/xml")
            if mpu_complete:
                mpu.require(upload_id, bucket, key, owner, allow_completed=True)
                expected_completion_head = check_write_precondition(
                    request, catalog, bucket, key,
                )
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
                finished = await mpu.complete(
                    upload_id, bucket, key, owner, selected, engine,
                    expected_generation=(
                        expected_completion_head.generation
                        if expected_completion_head else None
                    ),
                    expected_object_id=(
                        expected_completion_head.object_id
                        if expected_completion_head else None
                    ),
                )
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
                sha1 = hashlib.sha1(usedforsecurity=False)
                crc = 0
                crc32c_value = 0
                crc64_value = 0
                size = 0
                with os.fdopen(fd, "wb") as output:
                    async for block in request.stream():
                        size += len(block)
                        if max_bytes < 1 or size > max_bytes:
                            raise MultipartError("EntityTooLarge", "Part exceeds configured upload maximum", 413)
                        sha.update(block)
                        md5.update(block)
                        sha1.update(block)
                        crc = zlib.crc32(block, crc)
                        crc32c_value = crc32c(block, crc32c_value)
                        crc64_value = crc64nvme(block, crc64_value)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                _verify_upload_sha256(
                    request, sha.digest(), md5_digest=md5.digest(),
                    sha1_digest=sha1.digest(), crc64nvme_value=crc64_value,
                )
                checksum_crc = request.headers.get("x-amz-checksum-crc32")
                if checksum_crc and checksum_crc != base64.b64encode(
                    (crc & 0xffffffff).to_bytes(4, "big")
                ).decode():
                    raise MultipartError("BadDigest", "CRC32 checksum mismatch")
                declared_crc32c = request.headers.get("x-amz-checksum-crc32c")
                if declared_crc32c is not None and not hmac.compare_digest(
                    declared_crc32c,
                    base64.b64encode(crc32c_value.to_bytes(4, "big")).decode("ascii"),
                ):
                    raise MultipartError("BadDigest", "CRC32C checksum mismatch")
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

        if is_version_delete:
            if any(h in request.headers for h in ("if-match", "if-none-match")):
                return _error("NotImplemented", "Conditional VersionId delete unsupported", 501)
            version_id = request.query_params["versionId"]
            if not version_id or len(version_id) > 80:
                return _error("InvalidArgument", "Invalid version ID", 400)
            is_marker = db.connection.execute(select(objects.c.status).where(
                objects.c.id == version_id,
                objects.c.bucket == bucket,
                objects.c.object_key == key,
            )).scalar_one_or_none() == "delete-marker"
            if not catalog.delete_version(bucket, key, version_id):
                return _error("NoSuchVersion", "Version does not exist", 404)
            headers = {"x-amz-version-id": version_id}
            if is_marker:
                headers["x-amz-delete-marker"] = "true"
            return Response(status_code=204, headers=headers)

        expected_head = None
        if request.method in ("PUT", "DELETE") and not request.headers.get("x-amz-copy-source"):
            expected_head = check_write_precondition(
                request, catalog, bucket, key, delete=request.method == "DELETE"
            )

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
            if (request.headers.get("x-amz-metadata-directive") is not None
                    and "x-amz-metadata-directive" not in signed_headers):
                raise SigV4Error(
                    "AccessDenied", "Copy metadata directive must be signed"
                )
            metadata_directive = request.headers.get(
                "x-amz-metadata-directive", "COPY"
            )
            if metadata_directive not in {"COPY", "REPLACE"}:
                raise ObjectMetadataError("InvalidArgument", "Invalid metadata directive")
            copy_properties = parse_object_metadata(
                request.headers, signed_header_names(request),
            )
            if metadata_directive == "COPY" and copy_properties:
                raise ObjectMetadataError(
                    "InvalidRequest", "Object metadata requires MetadataDirective=REPLACE"
                )
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
            # A signed CopySource may name a specific historical version.
            # Never treat arbitrary query selectors or duplicate VersionIds as
            # part of the object key; URI escapes are decoded only afterwards.
            if source_header.startswith("//") or "://" in source_header:
                return _error("InvalidArgument", "Copy source must be a local bucket/object path", 400)
            original_header, divider, source_query = source_header.partition("?")
            source_version_id = None
            if divider:
                try:
                    qualifiers = parse_qsl(
                        source_query, keep_blank_values=True, strict_parsing=True,
                        encoding="utf-8", errors="strict", max_num_fields=2,
                    )
                except (UnicodeError, ValueError):
                    return _error("InvalidArgument", "Malformed CopySource version selector", 400)
                if len(qualifiers) != 1 or qualifiers[0][0] != "versionId":
                    return _error("InvalidArgument", "Only a single source VersionId is supported", 400)
                source_version_id = qualifiers[0][1]
                if not source_version_id or len(source_version_id) > 80:
                    return _error("InvalidArgument", "Invalid CopySource version ID", 400)
            original_path = unquote(
                original_header[1:] if original_header.startswith("/") else original_header
            )
            src_bucket, slash, src_key = original_path.partition("/")
            if not slash or not src_bucket or not src_key:
                return _error("InvalidArgument", "Invalid CopySource bucket/object key", 400)
            if src_bucket not in catalog.list_buckets():
                return _error("NoSuchBucket", "CopySource bucket does not exist", 404)
            if source_version_id is not None:
                if not catalog.is_visible_version(src_bucket, src_key, source_version_id):
                    return _error("NoSuchVersion", "CopySource version is not published", 404)
                try:
                    source_item = catalog.manifests.get(source_version_id)
                except FileNotFoundError:
                    return _error("NoSuchVersion", "CopySource version not readable", 404)
                if source_item.bucket != src_bucket or source_item.key != src_key:
                    return _error("NoSuchVersion", "CopySource key mismatch", 404)
            else:
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
                    source_version_id=source_version_id,
                    replacement_tags=(inline_tags or []) if tag_directive == "REPLACE" else None,
                    replacement_metadata=copy_properties if metadata_directive == "REPLACE" else None,
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
            try:
                marker = catalog.delete_marker(
                    bucket, key,
                    expected_generation=expected_head.generation if expected_head else None,
                    expected_object_id=expected_head.object_id if expected_head else None,
                )
            except ValueError as exc:
                if expected_head is not None:
                    raise WritePreconditionError(
                        "PreconditionFailed", "Object changed during conditional DELETE", 412
                    ) from exc
                raise
            return Response(status_code=204, headers={
                "x-amz-delete-marker": "true", "x-amz-version-id": marker,
            })

        if request.method == "PUT":
            root = Path(os.environ.get("TELEDRIVE_STORAGE_SCRATCH_DIR", "/data/tmp"))
            _secure_mkdir(root)
            max_bytes = int(os.environ.get("TELEDRIVE_REST_MAX_UPLOAD_BYTES", str(2 * 1024**3)))
            transport = storage_bot_transport(db)
            await transport.verify_target()
            fd, name = tempfile.mkstemp(prefix="s3-upload-", dir=root)
            stage = Path(name)
            digest = hashlib.sha256()
            md5 = hashlib.md5(usedforsecurity=False)
            sha1 = hashlib.sha1(usedforsecurity=False)
            crc = 0
            crc32c_value = 0
            crc64_value = 0
            length = 0
            try:
                with os.fdopen(fd, "wb") as dest:
                    async for block in request.stream():
                        length += len(block)
                        if max_bytes < 1 or length > max_bytes:
                            return _error("EntityTooLarge", "Upload exceeds configured maximum", 413)
                        digest.update(block)
                        md5.update(block)
                        sha1.update(block)
                        crc = zlib.crc32(block, crc)
                        crc32c_value = crc32c(block, crc32c_value)
                        crc64_value = crc64nvme(block, crc64_value)
                        dest.write(block)
                    dest.flush()
                    os.fsync(dest.fileno())
                _verify_upload_sha256(
                    request, digest.digest(), md5_digest=md5.digest(),
                    sha1_digest=sha1.digest(), crc64nvme_value=crc64_value,
                )
                checksum_crc = request.headers.get("x-amz-checksum-crc32")
                if checksum_crc and checksum_crc != base64.b64encode((crc & 0xffffffff).to_bytes(4, "big")).decode():
                    return _error("BadDigest", "CRC32 checksum mismatch", 400)
                declared_crc32c = request.headers.get("x-amz-checksum-crc32c")
                if declared_crc32c is not None and not hmac.compare_digest(
                    declared_crc32c,
                    base64.b64encode(crc32c_value.to_bytes(4, "big")).decode("ascii"),
                ):
                    return _error("BadDigest", "CRC32C checksum mismatch", 400)
                engine = TelegramObjectStorage(
                    catalog.manifests, transport,
                    chunk_size=BOT_RECOMMENDED_CHUNK_SIZE, scratch_directory=root,
                )
                # Metadata, tags and the head pointer share one SQL
                # transaction. A failed metadata write cannot publish a
                # partially described object.
                http_properties = parse_object_metadata(
                    request.headers, signed_header_names(request),
                )
                http_store = ObjectMetadataStore(catalog)
                tag_store = ObjectTagStore(catalog) if inline_tags is not None else None
                item = await engine.put_file(bucket, key, stage)
                http_store.attach_uncommitted(item.id, http_properties)
                if tag_store is not None:
                    tag_store.attach_uncommitted(item.id, inline_tags)
                try:
                    catalog.publish(
                        item.id, now=int(time.time()),
                        expected_generation=expected_head.generation if expected_head else None,
                        expected_object_id=expected_head.object_id if expected_head else None,
                    )
                except ValueError as exc:
                    if expected_head is not None:
                        raise WritePreconditionError(
                            "PreconditionFailed", "Object changed during conditional PUT", 412
                        ) from exc
                    raise
                return Response(status_code=200, headers={
                    "ETag": '"' + item.sha256 + '"',
                    "x-amz-version-id": item.id,
                })
            finally:
                stage.unlink(missing_ok=True)

        if is_version_read:
            version_id = request.query_params["versionId"]
            if not version_id or len(version_id) > 80:
                return _error("InvalidArgument", "Invalid version ID", 400)
            if not catalog.is_visible_version(bucket, key, version_id):
                return _error("NoSuchVersion", "Object version is not published", 404)
            status_row = db.connection.execute(select(
                objects.c.status, objects.c.created_at
            ).where(objects.c.id == version_id)).one_or_none()
            if status_row is not None and status_row[0] == "delete-marker":
                response = _error(
                    "MethodNotAllowed", "Requested version is a delete marker", 405,
                )
                response.headers.update({
                    "x-amz-delete-marker": "true", "x-amz-version-id": version_id,
                    "Last-Modified": http_last_modified(int(status_row[1])),
                })
                return response
            try:
                item = catalog.manifests.get(version_id)
            except FileNotFoundError:
                return _error("NoSuchVersion", "Object version is not readable", 404)
            if item.bucket != bucket or item.key != key:
                return _error("NoSuchVersion", "Version ownership mismatch", 404)
        else:
            try:
                item = catalog.get(bucket, key).item
            except FileNotFoundError:
                marker = catalog.current_marker(bucket, key)
                if marker is not None:
                    result = _error("NoSuchKey", "Current version is a delete marker", 404)
                    result.headers["x-amz-delete-marker"] = "true"
                    result.headers["x-amz-version-id"] = marker
                    return result
                return _error("NoSuchKey", "The specified key does not exist", 404)
        item_etag = MultipartStore(catalog).object_etag(item.id, item.sha256)
        if is_object_attributes:
            raw = request.headers.get("x-amz-object-attributes", "")
            signed = signed_header_names(request)
            if "x-amz-object-attributes" not in signed:
                raise SigV4Error("AccessDenied", "Object attributes header must be signed")
            if len(request.headers.getlist("x-amz-object-attributes")) != 1:
                return _error("InvalidArgument", "Duplicate object attributes header", 400)
            requested = raw.split(",")
            if (not requested or any(not name for name in requested)
                    or len(set(requested)) != len(requested)):
                return _error("InvalidArgument", "Invalid object attributes list", 400)
            permitted = {"ETag", "ObjectSize", "Checksum", "StorageClass", "ObjectParts"}
            if any(name not in permitted for name in requested):
                return _error("NotImplemented", "Requested object attributes are unsupported", 501)

            options = {}
            for name, default, low, high in (
                ("x-amz-max-parts", 1000, 1, 1000),
                ("x-amz-part-number-marker", 0, 0, MAX_PARTS),
            ):
                values = request.headers.getlist(name)
                if values:
                    if name not in signed:
                        raise SigV4Error("AccessDenied", "Object parts pagination must be signed")
                    if (len(values) != 1
                            or re.fullmatch(r"[0-9]{1,5}", values[0]) is None
                            or not low <= int(values[0]) <= high):
                        return _error("InvalidArgument", "Invalid object parts pagination", 400)
                    if "ObjectParts" not in requested:
                        return _error("InvalidArgument", "Part pagination requires ObjectParts", 400)
                    options[name] = int(values[0])
                else:
                    options[name] = default
            result = ET.Element("GetObjectAttributesOutput", xmlns=NAMESPACE)
            if "ETag" in requested:
                ET.SubElement(result, "ETag").text = '"' + item_etag + '"'
            if "ObjectSize" in requested:
                ET.SubElement(result, "ObjectSize").text = str(item.size)
            if "Checksum" in requested:
                checksum = ET.SubElement(result, "Checksum")
                # Immutable object SHA-256 covers the full plaintext bytes,
                # independent of multipart ETag and physical Telegram chunks.
                ET.SubElement(checksum, "ChecksumSHA256").text = base64.b64encode(
                    bytes.fromhex(item.sha256)
                ).decode("ascii")
                ET.SubElement(checksum, "ChecksumType").text = "FULL_OBJECT"
            if "StorageClass" in requested:
                # TeleDrive exposes one immutable STANDARD-like tier; archived
                # or replica-backed AWS storage classes do not exist here.
                ET.SubElement(result, "StorageClass").text = "STANDARD"
            if "ObjectParts" in requested:
                layout = MultipartStore(catalog).object_parts_for_version(item.id, item.size)
                # AWS omits ObjectParts for a plain single PUT. This must not
                # fabricate logical parts out of Telegram's internal chunks.
                if layout is not None:
                    marker = options["x-amz-part-number-marker"]
                    limit = options["x-amz-max-parts"]
                    remaining = [part for part in layout if part[0] > marker]
                    page = remaining[:limit]
                    listing = ET.SubElement(result, "ObjectParts")
                    for name, value in (
                        ("PartsCount", len(layout)),
                        ("PartNumberMarker", marker),
                        ("MaxParts", limit),
                        ("IsTruncated", str(len(remaining) > len(page)).lower()),
                    ):
                        ET.SubElement(listing, name).text = str(value)
                    if len(remaining) > len(page):
                        ET.SubElement(listing, "NextPartNumberMarker").text = str(page[-1][0])
                    for number, size, digest, _etag in page:
                        part_node = ET.SubElement(listing, "Part")
                        ET.SubElement(part_node, "PartNumber").text = str(number)
                        ET.SubElement(part_node, "Size").text = str(size)
                        ET.SubElement(part_node, "ChecksumSHA256").text = base64.b64encode(
                            bytes.fromhex(digest)
                        ).decode("ascii")
            return Response(
                ET.tostring(result, encoding="utf-8", xml_declaration=True),
                media_type="application/xml", status_code=200,
                headers={
                    "x-amz-version-id": item.id,
                    "Last-Modified": http_last_modified(item.created_at),
                },
            )
        http_properties = ObjectMetadataStore(catalog).get(item.id)
        precondition = read_precondition(request, item_etag, item.created_at)
        if precondition == 304:
            return Response(status_code=304, headers={
                "ETag": '"' + item_etag + '"',
                "Last-Modified": http_last_modified(item.created_at),
            })
        if precondition == 412:
            return _error("PreconditionFailed", "Object precondition failed", 412)
        if request.method == "HEAD":
            return Response(status_code=200, headers=_headers(
                item, item_etag, http_properties,
                checksum_mode=(checksum_mode == "ENABLED"),
            ))
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
        headers = _headers(item, item_etag, http_properties,
                           checksum_mode=(checksum_mode == "ENABLED"))
        headers["Content-Length"] = str(end - start)
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end-1}/{item.size}"
        return StreamingResponse(stream(), status_code=206 if partial else 200, headers=headers)
    except WritePreconditionError as exc:
        response = _error(exc.code, str(exc), exc.status)
        if request.method == "PUT":
            # Conditional writes can fail before their request body is read.
            # Never reuse an HTTP/1.1 connection with an unread PUT payload:
            # boto3 otherwise receives an invalid next-request 400 from h11.
            response.headers["Connection"] = "close"
        return response
    except BatchDeleteError as exc:
        return _error(exc.code, str(exc), exc.status)
    except S3QueryError as exc:
        return _error(exc.code, str(exc), exc.status)
    except BucketListingError as exc:
        return _error(exc.code, str(exc), exc.status)
    except LifecycleError as exc:
        return _error(exc.code, str(exc), exc.status)
    except PublicAccessError as exc:
        return _error(exc.code, str(exc), exc.status)
    except VersioningError as exc:
        return _error(exc.code, str(exc), exc.status)
    except CORSError as exc:
        return _error(exc.code, str(exc), exc.status)
    except TaggingError as exc:
        return _error(exc.code, str(exc), exc.status)
    except ObjectMetadataError as exc:
        return _error(exc.code, str(exc), exc.status)
    except MultipartError as exc:
        return _error(exc.code, str(exc), exc.status)
    except SigV4Error as exc:
        return _error(exc.code, exc.message, {
            "RequestTimeTooSkewed": 403, "NotImplemented": 501,
            "InvalidArgument": 400, "InvalidRequest": 400,
            "EntityTooLarge": 413,
            "XAmzContentSHA256Mismatch": 400, "BadDigest": 400,
            "InvalidRange": 416,
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


@app.middleware("http")
async def _route_encoded_control_keys(request: Request, call_next):
    """Preserve S3 opaque keys and enforce bucket CORS independently of SigV4.

    A browser OPTIONS preflight has no AWS signature but cannot read/write
    anything. Actual S3 methods still require the normal signed authorization.
    """
    path = request.scope.get("path", "")
    origin = request.headers.get("origin", "")
    wants_cors = bool(origin and path != "/__health" and path.startswith("/"))
    if wants_cors:
        logical = unquote((request.scope.get("raw_path") or b"/").decode("ascii").lstrip("/"))
        try:
            virtual_bucket = _virtual_host_bucket(request)
        except SigV4Error as exc:
            return _error(exc.code, exc.message, 400)
        bucket = virtual_bucket if virtual_bucket is not None else logical.split("/", 1)[0]
        if bucket:
            connection = None
            try:
                connection = db_connect()
                catalog = BucketCatalog(ManifestStore(connection))
                if bucket in catalog.list_buckets():
                    rules = BucketCORSStore(catalog).get(bucket) or []
                else:
                    rules = []
                is_preflight = request.method == "OPTIONS"
                cors_method = (request.headers.get("access-control-request-method", "")
                               if is_preflight else request.method)
                headers_string = (request.headers.get("access-control-request-headers", "")
                                  if is_preflight else "")
                requested = ([h.strip() for h in headers_string.split(",") if h.strip()]
                             if headers_string else [])
                # Cross-origin browser clients may send many headers; bound
                # their CORS preflight processing without trusting the Origin.
                if (len(headers_string) > 2048 or len(requested) > 32
                        or any(not h.isascii() for h in requested)):
                    return _error("AccessForbidden", "CORS preflight rejected", 403)
                matched = matching_rule(rules, origin, cors_method, requested)
                if is_preflight:
                    if not matched:
                        return _error("AccessForbidden", "CORS preflight rejected", 403)
                    return Response(status_code=200, headers=response_cors_headers(
                        matched, origin, preflight=True, requested_headers=requested
                    ))
            except Exception:
                if request.method == "OPTIONS":
                    return _error("ServiceUnavailable", "CORS lookup unavailable", 503)
                raise
            finally:
                if connection is not None:
                    connection.close()
            # Do not bypass signature validation for actual HTTP methods.
            if path != "/__health" and any(ord(ch) < 32 for ch in path):
                response = await _handle(request)
            else:
                response = await call_next(request)
            if matched:
                for name, value in response_cors_headers(
                    matched, origin, preflight=False
                ).items():
                    response.headers[name] = value
            return response
    if request.method == "OPTIONS":
        return _error("AccessForbidden", "CORS preflight rejected", 403)
    if path != "/__health" and any(ord(character) < 32 for character in path):
        return await _handle(request)
    return await call_next(request)


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
