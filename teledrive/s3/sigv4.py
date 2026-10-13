"""AWS Signature Version 4 header authorization for path-style S3 requests.

Only explicitly supported signed-header requests are admitted; AWS chunked
streaming, presigned query auth and SigV2 must fail closed until implemented.
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


def _canonical_query(raw: bytes) -> str:
    pairs = parse_qsl(raw.decode("ascii"), keep_blank_values=True)
    return "&".join(
        f"{quote(key, safe='-_.~')}={quote(value, safe='-_.~')}"
        for key, value in sorted(pairs, key=lambda x: (quote(x[0], safe='-_.~'), quote(x[1], safe='-_.~')))
    )


def _canonical_path(request: Request) -> str:
    # S3 does not normalize double slashes or dot segments in object keys.
    # Botocore signs the URL-encoded path, with each '%' encoded a second time.
    raw = request.scope.get("raw_path") or request.url.path.encode("utf-8")
    return quote(raw.decode("ascii"), safe="/~")


def verify_header_signature(request: Request, lookup_secret, permission: str, *,
                            now: datetime | None = None) -> str:
    """Return authenticated access-key ID; never reveal the secret on failure.

    lookup_secret(access_key, permission) must atomically honor revocation and
    scope. The body digest is separately checked by the upload handler.
    """
    if "X-Amz-Algorithm" in request.query_params or "X-Amz-Signature" in request.query_params:
        raise SigV4Error("NotImplemented", "Presigned URLs are not supported yet")
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
        if request.method not in {"GET", "HEAD", "DELETE"} or payload != "UNSIGNED-PAYLOAD":
            raise SigV4Error("XAmzContentSHA256Mismatch", "Signed SHA-256 payload required")
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
