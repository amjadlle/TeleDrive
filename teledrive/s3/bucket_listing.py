"""AWS-compatible ListBuckets pagination for TeleDrive's single private region.

No remote Telegram I/O: listing uses SQL bucket records and the shared catalog.
Cursor signing binds a lexicographic high-water mark to the S3 access key,
prefix and fixed region; invalid/tampered tokens cannot change the filter.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from xml.etree import ElementTree as ET

from teledrive.storage.catalog import BucketCatalog

NAMESPACE = "http://s3.amazonaws.com/doc/2006-03-01/"
BUCKET_REGION = "us-east-1"
MAX_BUCKETS = 10000
MAX_CURSOR_LENGTH = 1024
MAX_PREFIX_LENGTH = 255


class BucketListingError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def _error(message: str) -> BucketListingError:
    return BucketListingError("InvalidArgument", message)


def _signed_cursor(master: bytes, owner: str, prefix: str, after: str) -> str:
    payload = json.dumps(
        {"v": 1, "owner": owner, "prefix": prefix, "after": after,
         "region": BUCKET_REGION},
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    mac = hmac.new(master, b"teledrive:list-buckets:v1:" + encoded.encode(),
                   hashlib.sha256).hexdigest()
    token = encoded + "." + mac
    if len(token) > MAX_CURSOR_LENGTH:
        raise _error("Bucket cursor exceeds maximum length")
    return token


def _read_cursor(token: str, master: bytes, owner: str, prefix: str) -> str:
    if not token or len(token) > MAX_CURSOR_LENGTH:
        raise _error("Invalid ListBuckets continuation token")
    try:
        encoded, mac = token.split(".")
        if len(mac) != 64:
            raise ValueError("Bad token MAC")
        computed = hmac.new(
            master, b"teledrive:list-buckets:v1:" + encoded.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(computed, mac):
            raise ValueError("Bad token signature")
        payload = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True,
        )
        decoded = json.loads(payload)
        if not isinstance(decoded, dict) or set(decoded) != {
            "v", "owner", "prefix", "after", "region",
        }:
            raise ValueError("Invalid token structure")
        if (type(decoded["v"]) is not int or decoded["v"] != 1
                or decoded["owner"] != owner or decoded["prefix"] != prefix
                or decoded["region"] != BUCKET_REGION
                or not isinstance(decoded["after"], str)
                or not decoded["after"] or len(decoded["after"]) > 63):
            raise ValueError("Token does not match S3 request")
        return decoded["after"]
    except (ValueError, TypeError, UnicodeError, KeyError, binascii.Error) as exc:
        raise _error("Invalid or mismatched ListBuckets continuation token") from exc


def list_buckets_xml(catalog: BucketCatalog, params, master: bytes,
                     owner: str) -> bytes:
    """Produce SDK-parsable XML with a stable, bounded SQL keyset cursor.

    Page boundaries are lexicographic, not offsets: a bucket inserted before
    the cursor cannot cause duplication when listing continues. Like S3,
    this is not a snapshot across concurrent bucket creations/deletions.
    """
    prefix = params.get("prefix", "")
    if len(prefix) > MAX_PREFIX_LENGTH:
        raise _error("Bucket prefix is too long")
    requested_region = params.get("bucket-region")
    if requested_region is not None and requested_region != BUCKET_REGION:
        raise BucketListingError(
            "InvalidRequest", "ListBuckets region does not match the endpoint",
        )
    raw_max = params.get("max-buckets")
    if raw_max is None:
        maximum = MAX_BUCKETS
    else:
        if not raw_max.isdecimal() or not 1 <= len(raw_max) <= 5:
            raise _error("max-buckets must be an integer from 1 to 10000")
        maximum = int(raw_max)
        if maximum < 1 or maximum > MAX_BUCKETS:
            raise _error("max-buckets must be from 1 to 10000")

    resume = params.get("continuation-token")
    after = _read_cursor(resume, master, owner, prefix) if resume is not None else ""
    page, more = catalog.list_bucket_records(
        prefix=prefix, after=after, limit=maximum,
    )
    root = ET.Element("ListAllMyBucketsResult", xmlns=NAMESPACE)
    owner_tag = ET.SubElement(root, "Owner")
    ET.SubElement(owner_tag, "ID").text = hmac.new(
        master, b"teledrive:s3:deployment-owner:v1", hashlib.sha256,
    ).hexdigest()
    buckets = ET.SubElement(root, "Buckets")
    show_region = any(
        key in params for key in (
            "max-buckets", "bucket-region", "continuation-token", "prefix",
        )
    )
    for name, created_at in page:
        entry = ET.SubElement(buckets, "Bucket")
        ET.SubElement(entry, "Name").text = name
        ET.SubElement(entry, "CreationDate").text = time.strftime(
            "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(created_at),
        )
        if show_region:
            ET.SubElement(entry, "BucketRegion").text = BUCKET_REGION
    if "prefix" in params:
        ET.SubElement(root, "Prefix").text = prefix
    if more and page:
        ET.SubElement(root, "ContinuationToken").text = _signed_cursor(
            master, owner, prefix, page[-1][0],
        )
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
