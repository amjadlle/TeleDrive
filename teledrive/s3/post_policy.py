"""Strict, checksum-pinned SigV4 browser POST policy verifier."""
import base64
import binascii
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from email import policy as mime_policy
from email.parser import BytesParser
from urllib.parse import urlsplit

from .sigv4 import SigV4Error, _hmac

FORM_LIMIT = 20 * 1024 * 1024
REQUIRED_FIELDS = frozenset(("key", "policy", "x-amz-algorithm",
                             "x-amz-credential", "x-amz-date",
                             "x-amz-signature", "x-amz-checksum-sha256"))
OPTIONAL_FIELDS = frozenset(("success_action_status", "success_action_redirect"))
FIELDS = REQUIRED_FIELDS | OPTIONAL_FIELDS
CONDITIONS = (REQUIRED_FIELDS - {"policy", "x-amz-signature"}) | {"bucket"}


def parse_form(content_type: str, data: bytes):
    """Parse one file, mandatory signing fields and a bounded response status."""
    if len(data) > FORM_LIMIT:
        raise SigV4Error("EntityTooLarge", "Browser form is too large")
    if (len(content_type) > 256 or not content_type.lower().startswith("multipart/form-data;")
            or "\r" in content_type or "\n" in content_type):
        raise SigV4Error("InvalidRequest", "Expected multipart/form-data")
    try:
        mime = ("Content-Type: " + content_type +
                "\r\nMIME-Version: 1.0\r\n\r\n").encode("ascii") + data
        msg = BytesParser(policy=mime_policy.default).parsebytes(mime)
    except (ValueError, UnicodeError) as exc:
        raise SigV4Error("InvalidRequest", "Malformed upload form") from exc
    if not msg.is_multipart() or msg.defects:
        raise SigV4Error("InvalidRequest", "Malformed multipart upload")
    items = list(msg.iter_parts())
    if len(items) > len(FIELDS) + 1:
        raise SigV4Error("InvalidRequest", "Too many browser form parts")
    fields = {}
    file_bytes = None
    for part in items:
        name = part.get_param("name", header="content-disposition")
        if (part.is_multipart() or part.defects or
                part.get_content_disposition() != "form-data" or
                name not in FIELDS | {"file"} or
                part.get("Content-Transfer-Encoding", "binary").lower()
                not in ("binary", "7bit", "8bit")):
            raise SigV4Error("InvalidRequest", "Unsupported form field")
        content = part.get_payload(decode=True)
        if not isinstance(content, bytes):
            raise SigV4Error("InvalidRequest", "Malformed browser form part")
        if name == "file":
            if file_bytes is not None:
                raise SigV4Error("InvalidRequest", "Duplicate form file")
            file_bytes = content
        else:
            if name in fields or len(content) > 12000:
                raise SigV4Error("InvalidRequest", "Duplicate or oversized form field")
            try:
                fields[name] = content.decode("utf-8")
            except UnicodeError as exc:
                raise SigV4Error("InvalidRequest", "Invalid field encoding") from exc
    if (file_bytes is None or not REQUIRED_FIELDS.issubset(fields)
            or not set(fields).issubset(FIELDS)):
        raise SigV4Error("InvalidRequest", "Missing or unsupported signed browser form fields")
    return fields, file_bytes


def verify_post_policy(fields, body, bucket, lookup_secret, *, max_bytes, now=None):
    if len(body) > max_bytes:
        raise SigV4Error("EntityTooLarge", "Browser payload too large")
    scope = re.fullmatch(
        r"([^/\s]{1,64})/(\d{8})/([a-z0-9-]{1,32})/s3/aws4_request",
        fields["x-amz-credential"],
    )
    if scope is None or fields["x-amz-algorithm"] != "AWS4-HMAC-SHA256":
        raise SigV4Error("AccessDenied", "Unsupported browser POST scope")
    access, day, region = scope.groups()
    stamp = fields["x-amz-date"]
    if not re.fullmatch(r"\d{8}T\d{6}Z", stamp) or stamp[:8] != day:
        raise SigV4Error("AccessDenied", "Invalid policy date")
    try:
        signed_at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise SigV4Error("AccessDenied", "Invalid policy signing date") from exc
    now = now or datetime.now(timezone.utc)
    if not -300 <= (now - signed_at).total_seconds() <= 900:
        raise SigV4Error("AccessDenied", "Policy signing date expired")
    status = fields.get("success_action_status")
    redirect = fields.get("success_action_redirect")
    if status is not None and status not in ("200", "201", "204"):
        raise SigV4Error("InvalidArgument", "Unsupported POST success response status")
    # Redirects are only browser instructions (the server never fetches
    # their target). Require an exact signed, HTTPS-only destination and
    # forbid credentials, fragments and ambiguous URL representations.
    if redirect is not None:
        if (status is not None or len(redirect) > 2048
                or any(ord(char) < 32 or char == "\\" for char in redirect)):
            raise SigV4Error("InvalidArgument", "Unsupported POST success redirect")
        try:
            target = urlsplit(redirect)
            valid = (target.scheme == "https" and target.netloc
                     and target.hostname and target.username is None
                     and target.password is None and target.fragment == ""
                     and (target.port is None or target.port > 0))
        except ValueError:
            valid = False
        if not valid:
            raise SigV4Error("InvalidArgument", "Unsafe POST success redirect")
    key = fields["key"]
    if not key or len(key.encode()) > 1024 or "$" + "{" in key:
        raise SigV4Error("InvalidArgument", "Exact non-template object key required")
    try:
        expected_sha = base64.b64decode(fields["x-amz-checksum-sha256"], validate=True)
        raw = base64.b64decode(fields["policy"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SigV4Error("InvalidArgument", "Invalid policy base64") from exc
    if len(expected_sha) != 32 or len(raw) > 8192 or len(raw) == 0:
        raise SigV4Error("InvalidArgument", "Invalid policy or checksum size")
    if not hmac.compare_digest(expected_sha, hashlib.sha256(body).digest()):
        raise SigV4Error("BadDigest", "Browser form checksum mismatch")
    try:
        doc = json.loads(raw)
        expiration = datetime.strptime(doc["expiration"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        clauses = doc["conditions"]
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise SigV4Error("InvalidArgument", "Invalid POST policy JSON") from exc
    if (not isinstance(doc, dict) or set(doc) != {"conditions", "expiration"}
            or not isinstance(clauses, list)
            or len(clauses) != len(CONDITIONS) + 1
            + (status is not None) + (redirect is not None)
            or not now < expiration <= signed_at + timedelta(minutes=15)):
        raise SigV4Error("AccessDenied", "Policy must expire within fifteen minutes")
    bound = {name: fields[name] for name in CONDITIONS if name != "bucket"}
    bound["bucket"] = bucket
    if status is not None:
        bound["success_action_status"] = status
    if redirect is not None:
        bound["success_action_redirect"] = redirect
    expected_conditions = set(bound)
    seen = set()
    has_range = False
    for entry in clauses:
        if (isinstance(entry, list) and len(entry) == 3 and
                entry[0] == "content-length-range"):
            if (has_range or any(type(value) is not int for value in entry[1:])
                    or not 0 <= entry[1] <= len(body) <= entry[2] <= max_bytes):
                raise SigV4Error("AccessDenied", "Invalid signed size range")
            has_range = True
            continue
        # A signed prefix may constrain the browser's destination key.
        # All other fields remain exact matches.
        if (isinstance(entry, list) and len(entry) == 3
                and entry[0] == "starts-with" and entry[1] == "$key"):
            if ("key" in seen or not isinstance(entry[2], str)
                    or not key.startswith(entry[2])):
                raise SigV4Error("AccessDenied", "POST key is outside signed prefix")
            seen.add("key")
            continue
        if isinstance(entry, dict) and len(entry) == 1:
            name, value = next(iter(entry.items()))
        elif (isinstance(entry, list) and len(entry) == 3
              and entry[0] == "eq" and isinstance(entry[1], str)
              and entry[1].startswith("$")):
            name, value = entry[1][1:], entry[2]
        else:
            raise SigV4Error("AccessDenied", "Unsupported POST policy condition")
        if name not in expected_conditions or name in seen or value != bound[name]:
            raise SigV4Error("AccessDenied", "POST policy does not pin every form field")
        seen.add(name)
    if seen != expected_conditions or not has_range:
        raise SigV4Error("AccessDenied", "Incomplete POST upload constraints")
    secret = lookup_secret(access, "s3:PutObject")
    if secret is None:
        raise SigV4Error("AccessDenied", "Revoked or unauthorized POST credential")
    key_bytes = _hmac(b"AWS4" + secret.encode(), day)
    for element in (region, "s3", "aws4_request"):
        key_bytes = _hmac(key_bytes, element)
    signature = hmac.new(key_bytes, fields["policy"].encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, fields["x-amz-signature"]):
        raise SigV4Error("SignatureDoesNotMatch", "Browser form signature mismatch")
    return key
