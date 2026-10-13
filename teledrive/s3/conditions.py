"""Read preconditions for S3 GetObject / HeadObject.

Matches use the object ETag (multipart or standard), not necessarily SHA256.
Only the immutable manifest timestamp is used for HTTP date comparisons.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from starlette.requests import Request


def http_last_modified(created_at: int) -> str:
    return formatdate(int(created_at), usegmt=True)


def _matches(raw: str, current_etag: str) -> bool:
    # S3 condition headers use quoted ETags; '*' follows HTTP semantics.
    expected = '"' + current_etag + '"'
    return any(v.strip() in (expected, "*") for v in raw.split(","))


def _date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def read_precondition(request: Request, etag: str, created_at: int) -> int | None:
    """Return 304, 412, or None without initiating a Telegram transfer.

    ETag conditions take precedence over date conditions of the same class.
    An unmatched If-Match is 412; matched If-None-Match is 304.
    S3-specific combinations of conditions beyond these standard HTTP
    outcomes will be expanded when tested against more client SDKs.
    """
    if_match = request.headers.get("if-match")
    if_none_match = request.headers.get("if-none-match")
    if if_match is not None and not _matches(if_match, etag):
        return 412
    if if_none_match is not None and _matches(if_none_match, etag):
        return 304

    created = datetime.fromtimestamp(created_at, tz=timezone.utc)
    created = created.replace(microsecond=0)

    # RFC 9110: If-Unmodified-Since is ignored if If-Match is present;
    # If-Modified-Since is ignored if If-None-Match is present.
    unmodified_since = _date(request.headers.get("if-unmodified-since"))
    if if_match is None and unmodified_since is not None and created > unmodified_since:
        return 412
    modified_since = _date(request.headers.get("if-modified-since"))
    if if_none_match is None and modified_since is not None and created <= modified_since:
        return 304
    return None
