"""Strict SigV4 wire-query validation in both header and presigned modes."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from starlette.requests import Request

from teledrive.s3.sigv4 import (
    MAX_QUERY_BYTES, MAX_QUERY_FIELDS, SigV4Error,
    _canonical_query, verify_header_signature,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def _request(query: bytes, presigned: bool) -> Request:
    headers = [(b"host", b"localhost:9000")]
    if not presigned:
        auth = (
            "AWS4-HMAC-SHA256 "
            "Credential=TDTEST/20261013/us-east-1/s3/aws4_request,"
            "SignedHeaders=host;x-amz-date,Signature=" + "0" * 64
        )
        headers.extend((
            (b"x-amz-date", b"20261013T120000Z"),
            (b"x-amz-content-sha256", b"UNSIGNED-PAYLOAD"),
            (b"authorization", auth.encode("ascii")),
        ))
    return Request({
        "type": "http", "method": "GET",
        "scheme": "http", "server": ("localhost", 9000),
        "path": "/private/obj.bin", "raw_path": b"/private/obj.bin",
        "query_string": query, "headers": headers,
    })


@pytest.mark.parametrize("query", (
    b"prefix=valid&bad=\xff",
    b"prefix=%FF",
    b"prefix=%C3%28",
    b"&".join([b"parameter=value"] * (MAX_QUERY_FIELDS + 1)),
    b"prefix=" + b"x" * MAX_QUERY_BYTES,
))
@pytest.mark.parametrize("presigned", [False, True])
def test_invalid_queries_fail_as_s3_protocol_errors(query, presigned):
    if presigned:
        query = b"X-Amz-Algorithm=AWS4-HMAC-SHA256&" + query
    request = _request(query, presigned)
    with pytest.raises(SigV4Error) as error:
        verify_header_signature(
            request,
            lambda *_: pytest.fail("Untrusted query reached credential lookup"),
            "s3:GetObject", now=NOW,
        )
    assert error.value.code == "InvalidArgument"


def test_valid_unicode_and_duplicate_keys_canonicalize_stably():
    assert _canonical_query(
        b"prefix=%C3%A9&delimiter=%2F&empty=&prefix=a"
    ) == "delimiter=%2F&empty=&prefix=%C3%A9&prefix=a"


def test_query_limits_are_inclusive_not_off_by_one():
    wire = b"k=" + b"x" * (MAX_QUERY_BYTES - 2)
    assert _canonical_query(wire) == wire.decode("ascii")
    pairs = b"&".join([b"k=v"] * MAX_QUERY_FIELDS)
    assert _canonical_query(pairs) == pairs.decode("ascii")
