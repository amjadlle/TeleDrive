"""Strict S3 DeleteObjects request validation.

No Telegram message is deleted by this parser. A valid batch produces a
bounded list of logical keys for the catalog to unlink one at a time.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import zlib
from dataclasses import dataclass
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from xml.etree.ElementTree import ParseError


MAX_BATCH_OBJECTS = 1000
MAX_XML_BYTES = 1024 * 1024


class BatchDeleteError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class BatchDeleteRequest:
    keys: tuple[str, ...]
    quiet: bool


def verify_body(body: bytes, headers: dict | object) -> None:
    """Reject corrupted or unsigned batch manifests before any unlink.

    S3 requires Content-MD5 or one of the modern checksum headers; at least
    one must match. The SigV4-bound SHA-256 must match independently.
    """
    signature_sha = headers.get("x-amz-content-sha256", "")
    if not isinstance(signature_sha, str) or not hmac.compare_digest(
        signature_sha, hashlib.sha256(body).hexdigest()
    ):
        raise BatchDeleteError("XAmzContentSHA256Mismatch",
                               "DeleteObjects signed SHA-256 does not match")
    content_md5 = headers.get("content-md5")
    crc32 = headers.get("x-amz-checksum-crc32")
    sha256 = headers.get("x-amz-checksum-sha256")
    if not any((content_md5, crc32, sha256)):
        raise BatchDeleteError("InvalidRequest", "DeleteObjects requires a body checksum")
    expected = {
        "content-md5": hashlib.md5(body, usedforsecurity=False).digest(),
        "x-amz-checksum-crc32": (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big"),
        "x-amz-checksum-sha256": hashlib.sha256(body).digest(),
    }
    for name, supplied in (("content-md5", content_md5),
                           ("x-amz-checksum-crc32", crc32),
                           ("x-amz-checksum-sha256", sha256)):
        if supplied is None:
            continue
        try:
            provided = base64.b64decode(supplied, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise BatchDeleteError("InvalidDigest", f"Invalid {name} base64") from exc
        if not hmac.compare_digest(provided, expected[name]):
            raise BatchDeleteError("BadDigest", f"Incorrect {name}")


def parse_batch(body: bytes) -> BatchDeleteRequest:
    if not body or len(body) > MAX_XML_BYTES:
        raise BatchDeleteError("MalformedXML", "Invalid DeleteObjects XML length")
    # XML entities, DTDs and external resources must never be resolved.
    try:
        root = SafeET.fromstring(body)
    except (ParseError, DefusedXmlException) as exc:
        raise BatchDeleteError("MalformedXML", "Unsafe or malformed XML") from exc

    def local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    if local_name(root.tag) != "Delete" or root.attrib:
        raise BatchDeleteError("MalformedXML", "Invalid DeleteObjects root")

    keys: list[str] = []
    quiet = False
    quiet_seen = False
    for entry in root:
        if entry.attrib:
            raise BatchDeleteError("MalformedXML", "Unexpected XML attributes")
        name = local_name(entry.tag)
        if name == "Quiet":
            if quiet_seen or list(entry) or (entry.text or "").strip().lower() not in (
                "true", "false"
            ):
                raise BatchDeleteError("MalformedXML", "Invalid Quiet field")
            quiet = (entry.text or "").strip().lower() == "true"
            quiet_seen = True
        elif name == "Object":
            if len(keys) >= MAX_BATCH_OBJECTS:
                raise BatchDeleteError("MalformedXML", "Too many DeleteObjects entries")
            fields = list(entry)
            if (len(fields) != 1 or local_name(fields[0].tag) != "Key"
                    or fields[0].attrib or list(fields[0])):
                # VersionId is intentionally refused until real S3 version
                # addressing/retention can be honored.
                raise BatchDeleteError("NotImplemented",
                                       "DeleteObjects VersionId or extension unsupported", 501)
            key = fields[0].text or ""
            if not (1 <= len(key.encode("utf-8")) <= 1024):
                raise BatchDeleteError("InvalidObjectName", "Invalid S3 key")
            keys.append(key)
        else:
            raise BatchDeleteError("MalformedXML", "Unexpected DeleteObjects field")
    if not keys:
        raise BatchDeleteError("MalformedXML", "No objects in DeleteObjects request")
    return BatchDeleteRequest(tuple(keys), quiet)
