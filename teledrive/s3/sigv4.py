"""AWS Signature V4 authorization for path-style S3 requests.

Supports signed headers and short-lived query presigns. A presigned PUT must
bind a SHA-256 checksum in its signed headers; anonymous mutable payload
presigns, AWS chunked streaming and SigV2 are deliberately refused.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlsplit

from starlette.requests import Request


class SigV4Error(ValueError):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, data: str) -> bytes:
    return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()


MAX_QUERY_BYTES = 16 * 1024
MAX_QUERY_FIELDS = 100


def _query_pairs(raw: bytes) -> list[tuple[str, str]]:
    """Reject malformed or oversized wire queries before canonicalizing them.

    urllib.parse defaults to replacement characters on invalid UTF-8; a
    signature verifier must not authenticate normalized substitute bytes.
    """
    if len(raw) > MAX_QUERY_BYTES:
        raise SigV4Error("InvalidArgument", "S3 query string exceeds 16 KiB")
    try:
        return parse_qsl(
            raw.decode("ascii"), keep_blank_values=True,
            encoding="utf-8", errors="strict", max_num_fields=MAX_QUERY_FIELDS,
        )
    except (UnicodeError, ValueError) as exc:
        raise SigV4Error("InvalidArgument", "Invalid S3 query string") from exc


def _canonical_query(raw: bytes) -> str:
    pairs = _query_pairs(raw)
    return "&".join(
        f"{quote(key, safe='-_.~')}={quote(value, safe='-_.~')}"
        for key, value in sorted(pairs, key=lambda x: (quote(x[0], safe='-_.~'), quote(x[1], safe='-_.~')))
    )


def _canonical_path(request: Request) -> str:
    # S3SigV4Auth (unlike generic SigV4Auth) deliberately does NOT re-encode
    # or normalize the already URL-encoded request path. Preserve percent
    # escapes, double slashes and dot segments byte-for-byte.
    raw = request.scope.get("raw_path") or request.url.path.encode("utf-8")
    try:
        path = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise SigV4Error("SignatureDoesNotMatch", "Invalid encoded URL path") from exc
    if not path.startswith("/"):
        raise SigV4Error("SignatureDoesNotMatch", "Invalid absolute URL path")
    return path


def _presigned_signature(request: Request, lookup_secret, permission: str,
                         now: datetime | None) -> str:
    if request.method not in {"GET", "HEAD", "PUT", "DELETE"}:
        raise SigV4Error("NotImplemented", "Presigned multipart POST is unsupported")
    if request.headers.get("authorization"):
        raise SigV4Error("AccessDenied", "Ambiguous Authorization and query signature")
    pairs = _query_pairs(request.scope.get("query_string", b""))
    required = {
        "X-Amz-Algorithm", "X-Amz-Credential", "X-Amz-Date",
        "X-Amz-Expires", "X-Amz-SignedHeaders", "X-Amz-Signature",
    }
    values = {}
    for key, value in pairs:
        if key in values:
            raise SigV4Error("SignatureDoesNotMatch", "Repeated presigned query parameter")
        values[key] = value
    if not required.issubset(values):
        raise SigV4Error("AccessDenied", "Incomplete presigned S3 URL")
    if values["X-Amz-Algorithm"] != "AWS4-HMAC-SHA256":
        raise SigV4Error("AccessDenied", "Only AWS Signature V4 is supported")
    if "X-Amz-Security-Token" in values:
        raise SigV4Error("InvalidToken", "Temporary AWS session tokens are unsupported")
    credential = re.fullmatch(
        r"([^/\s]{1,64})/(\d{8})/([a-z0-9-]{1,32})/s3/aws4_request",
        values["X-Amz-Credential"],
    )
    if credential is None:
        raise SigV4Error("AccessDenied", "Invalid presigned credential scope")
    access_key, date, region = credential.groups()
    signed_string = values["X-Amz-SignedHeaders"]
    signed = signed_string.split(";")
    if (not signed or signed != sorted(set(signed)) or "host" not in signed
            or any(not re.fullmatch(r"[a-z0-9-]+", x) for x in signed)):
        raise SigV4Error("SignatureDoesNotMatch", "Invalid presigned SignedHeaders")
    headers = {}
    for name in signed:
        matching = [v.decode("latin-1") for k, v in request.scope["headers"]
                    if k.decode("ascii").lower() == name]
        if len(matching) != 1:
            raise SigV4Error("SignatureDoesNotMatch", "Ambiguous or missing signed header")
        headers[name] = " ".join(matching[0].strip().split())
    if request.method == "PUT":
        # Never expose an S3 write URL which permits arbitrary replacement
        # with a different payload by anyone who obtains the link.
        if not {"x-amz-checksum-sha256", "x-amz-content-sha256"}.intersection(signed):
            raise SigV4Error("AccessDenied", "Presigned PUT requires signed SHA-256 checksum")
        if "x-amz-content-sha256" in signed and not re.fullmatch(
            r"[0-9a-f]{64}", headers["x-amz-content-sha256"]
        ):
            raise SigV4Error("XAmzContentSHA256Mismatch", "Invalid signed upload checksum")
        if "x-amz-checksum-sha256" in signed:
            import base64
            import binascii
            try:
                checksum = base64.b64decode(headers["x-amz-checksum-sha256"], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise SigV4Error("XAmzContentSHA256Mismatch", "Invalid signed upload checksum") from exc
            if len(checksum) != 32:
                raise SigV4Error("XAmzContentSHA256Mismatch", "Invalid signed upload checksum")
    stamp = values["X-Amz-Date"]
    if not re.fullmatch(r"\d{8}T\d{6}Z", stamp) or stamp[:8] != date:
        raise SigV4Error("AccessDenied", "Invalid presigned date")
    try:
        instant = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        expires = int(values["X-Amz-Expires"])
    except ValueError as exc:
        raise SigV4Error("AccessDenied", "Invalid presigned expiration") from exc
    if not (1 <= expires <= 604800):
        raise SigV4Error("AccessDenied", "Presigned URL expiry must be 1..604800 seconds")
    clock = now or datetime.now(timezone.utc)
    if (clock - instant).total_seconds() > expires:
        raise SigV4Error("AccessDenied", "Presigned URL expired")
    if (instant - clock).total_seconds() > 900:
        raise SigV4Error("RequestTimeTooSkewed", "Presigned timestamp too far in future")
    signature = values["X-Amz-Signature"]
    if not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise SigV4Error("SignatureDoesNotMatch", "Invalid signature format")
    canonical_pairs = [(k, v) for k, v in pairs if k != "X-Amz-Signature"]
    canonical_query = "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
        for k, v in sorted(canonical_pairs, key=lambda p:
                           (quote(p[0], safe='-_.~'), quote(p[1], safe='-_.~')))
    )
    canonical = (
        f"{request.method}\n{_canonical_path(request)}\n{canonical_query}\n"
        + "".join(f"{name}:{headers[name]}\n" for name in signed)
        + f"\n{signed_string}\nUNSIGNED-PAYLOAD"
    )
    scope = f"{date}/{region}/s3/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{stamp}\n{scope}\n{_sha(canonical.encode())}"
    )
    secret = lookup_secret(access_key, permission)
    if secret is None:
        raise SigV4Error("AccessDenied", "Unknown, expired, revoked or unauthorized access key")
    key = _hmac(b"AWS4" + secret.encode(), date)
    for word in (region, "s3", "aws4_request"):
        key = _hmac(key, word)
    expected = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise SigV4Error("SignatureDoesNotMatch", "Presigned signature mismatch")
    return access_key


def verify_header_signature(request: Request, lookup_secret, permission: str, *,
                            now: datetime | None = None) -> str:
    """Return authenticated access-key ID; never reveal the secret on failure.

    lookup_secret(access_key, permission) must atomically honor revocation and
    scope. The body digest is separately checked by the upload handler.
    """
    if "X-Amz-Algorithm" in request.query_params or "X-Amz-Signature" in request.query_params:
        return _presigned_signature(request, lookup_secret, permission, now)
    auth = request.headers.get("authorization", "")
    match = re.fullmatch(
        r"AWS4-HMAC-SHA256 Credential=([^\s,/]{1,64})/(\d{8})/([a-z0-9-]{1,32})/s3/aws4_request,"
        r"\s*SignedHeaders=([a-z0-9;\-]+),\s*Signature=([0-9a-f]{64})",
        auth,
    )
    if match is None:
        raise SigV4Error("AccessDenied", "AWS Signature V4 Authorization required")
    access_key, date, region, headers_part, actual_signature = match.groups()
    signed = headers_part.split(";")
    if signed != sorted(set(signed)) or "host" not in signed or "x-amz-date" not in signed:
        raise SigV4Error("SignatureDoesNotMatch", "Invalid SignedHeaders")
    headers: dict[str, str] = {}
    for name in signed:
        # Ambiguous repeated signed headers are rejected rather than combined.
        values = [v.decode("latin-1") for k, v in request.scope["headers"]
                  if k.decode("ascii").lower() == name]
        if len(values) != 1:
            raise SigV4Error("SignatureDoesNotMatch", "Missing or ambiguous signed header")
        headers[name] = " ".join(values[0].strip().split())
    request_date = request.headers.get("x-amz-date", "")
    if not re.fullmatch(r"\d{8}T\d{6}Z", request_date) or request_date[:8] != date:
        raise SigV4Error("RequestTimeTooSkewed", "Invalid signed timestamp")
    try:
        instant = datetime.strptime(request_date, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise SigV4Error("RequestTimeTooSkewed", "Invalid request timestamp") from exc
    clock = now or datetime.now(timezone.utc)
    if abs((clock - instant).total_seconds()) > 900:
        raise SigV4Error("RequestTimeTooSkewed", "Request timestamp outside 15-minute window")
    payload = request.headers.get("x-amz-content-sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", payload):
        if payload != "UNSIGNED-PAYLOAD":
            raise SigV4Error("XAmzContentSHA256Mismatch", "Signed SHA-256 payload required")
        if request.method in {"PUT", "POST"} and not (
            "content-md5" in signed or "x-amz-checksum-sha256" in signed
        ):
            raise SigV4Error(
                "XAmzContentSHA256Mismatch",
                "Unsigned upload requires a signed Content-MD5 or SHA-256 checksum",
            )
    canonical = (
        f"{request.method}\n{_canonical_path(request)}\n"
        f"{_canonical_query(request.scope.get('query_string', b''))}\n"
        + "".join(f"{key}:{headers[key]}\n" for key in signed)
        + f"\n{headers_part}\n{payload}"
    )
    scope = f"{date}/{region}/s3/aws4_request"
    to_sign = f"AWS4-HMAC-SHA256\n{request_date}\n{scope}\n{_sha(canonical.encode())}"
    secret = lookup_secret(access_key, permission)
    if secret is None:
        raise SigV4Error("AccessDenied", "Unknown, expired, revoked or unauthorized access key")
    key = _hmac(b"AWS4" + secret.encode(), date)
    for word in (region, "s3", "aws4_request"):
        key = _hmac(key, word)
    expected = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, actual_signature):
        raise SigV4Error("SignatureDoesNotMatch", "Request signature mismatch")
    return access_key
