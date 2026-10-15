"""P2-0 protocol selector audit: unsupported controls must never mutate.

Tests go through the real SigV4 HTTP app, SQLite and deterministic Telegram
adapter; valid signatures must not turn unsupported AWS methods into success.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode

import pytest
from starlette.datastructures import QueryParams

from test_s3_gateway import service, signed
from teledrive.s3.route_guard import validate_s3_query, S3QueryError

ROOT = Path(__file__).resolve().parents[1]
CONTROL_SELECTORS = (
    "acl", "policy", "website", "notification",
    "notificationConfiguration", "logging", "replication", "encryption",
    "object-lock", "retention", "legal-hold", "publicAccessBlock-malformed",
    "ownershipControls", "accelerate", "requestPayment", "select",
    "select-type", "restore", "torrent", "analytics", "inventory",
    "metrics", "intelligent-tiering", "x-unknown-provider-option",
)
EXACT_SUPPORTED = {"location", "cors", "tagging", "versioning",
                   "publicAccessBlock", "lifecycle"}


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "DELETE"])
@pytest.mark.parametrize("selector", CONTROL_SELECTORS)
def test_unknown_bucket_control_never_falls_back_to_bucket_crud(
    service, method, selector,
):
    assert signed(service, "PUT", "/guard-bucket").status_code == 200
    before = service[2].uploads
    reply = signed(service, method, f"/guard-bucket?{selector}")
    assert reply.status_code == 501, (method, selector, reply.text)
    if method != "HEAD":
        assert b"NotImplemented" in reply.content
    assert signed(service, "HEAD", "/guard-bucket").status_code == 200
    assert service[2].uploads == before


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "DELETE"])
@pytest.mark.parametrize("selector", CONTROL_SELECTORS)
def test_unknown_object_control_never_falls_back_to_data_or_delete(
    service, method, selector,
):
    assert signed(service, "PUT", "/guard-bucket").status_code == 200
    assert signed(service, "PUT", "/guard-bucket/asset.bin", b"old-data").status_code == 200
    sent = service[2].uploads
    reply = signed(service, method, f"/guard-bucket/asset.bin?{selector}",
                   b"new-data" if method == "PUT" else b"")
    assert reply.status_code == 501, (method, selector, reply.text)
    assert signed(service, "GET", "/guard-bucket/asset.bin").content == b"old-data"
    assert service[2].uploads == sent


@pytest.mark.parametrize("method,path", [
    ("DELETE", "/guard-bucket?prefix=backup"),
    ("DELETE", "/guard-bucket?max-keys=5"),
    ("PUT", "/guard-bucket?max-keys=10"),
    ("GET", "/guard-bucket?continuation-token=abc"),
    ("GET", "/guard-bucket?marker=x&list-type=2"),
    ("GET", "/guard-bucket?tagging&prefix=secret"),
    ("GET", "/guard-bucket?uploads&versions"),
    ("POST", "/guard-bucket?delete&uploads"),
    ("GET", "/guard-bucket/data?response-content-type=text/plain"),
    ("PUT", "/guard-bucket/data?prefix=x"),
    ("DELETE", "/guard-bucket/data?versioning"),
    ("DELETE", "/guard-bucket/data?lifecycle"),
    ("GET", "/guard-bucket/data?uploads"),
    ("GET", "/guard-bucket/data?uploadId=uid&prefix=x"),
    ("POST", "/guard-bucket/data?select&select-type=2"),
])
def test_conflicting_or_wrong_scope_controls_fail_closed(service, method, path):
    assert signed(service, "PUT", "/guard-bucket").status_code == 200
    code = signed(service, method, path).status_code
    assert code in (400, 501), (method, path, code)
    assert signed(service, "HEAD", "/guard-bucket").status_code == 200
    assert service[2].uploads == 0


@pytest.mark.parametrize("path", [
    "/guard-bucket?prefix=x&prefix=y",
    "/guard-bucket?list-type=2&list-type=2",
    "/guard-bucket?tagging&tagging",
    "/guard-bucket?versioning=Enabled",
    "/guard-bucket?lifecycle=Enabled",
    "/guard-bucket?uploads=1",
    "/guard-bucket/data?tagging=1",
    "/guard-bucket/data?versionId=a&versionId=b",
])
def test_duplicate_or_nonempty_subresources_rejected(service, path):
    assert signed(service, "PUT", "/guard-bucket").status_code == 200
    result = signed(service, "GET", path)
    assert result.status_code == 400, result.text
    assert service[2].uploads == 0


def test_existing_sdk_query_and_paginated_listings_still_work(service):
    assert signed(service, "PUT", "/guard-bucket").status_code == 200
    assert signed(service, "PUT", "/guard-bucket/example.bin", b"ok").status_code == 200
    v2 = signed(service, "GET", "/guard-bucket?list-type=2&prefix=example&max-keys=5&x-id=ListObjectsV2")
    assert v2.status_code == 200, v2.text
    assert b"example.bin" in v2.content
    v1 = signed(service, "GET", "/guard-bucket?prefix=example&max-keys=5")
    assert v1.status_code == 200, v1.text
    assert b"example.bin" in v1.content


def test_116_operation_manifest_exactly_matches_reviewed_spec():
    doc = (ROOT / "docs/S3_SPECS.md").read_text()
    data = json.loads((ROOT / "teledrive/s3/operation_manifest.json").read_text())
    pattern = (
        r"^\| \[([A-Za-z0-9]+)\]\("
        r"https://docs\.aws\.amazon\.com/AmazonS3/latest/API/API_"
        r"([A-Za-z0-9]+)\.html\) \| (✅ Supported|🟡 Partial|❌ Unsupported) \|"
    )
    rows = re.findall(pattern, doc, flags=re.MULTILINE)
    mapped = {"✅ Supported":"supported", "🟡 Partial":"partial",
              "❌ Unsupported":"unsupported"}
    expected = {name: mapped[status] for name, api_name, status in rows
                if name == api_name}
    assert len(rows) == len(expected) == len(data["operations"]) == 116
    actual = {item["name"]: item["status"] for item in data["operations"]}
    assert expected == actual
    assert Counter(actual.values()) == {
        "supported":11, "partial":27, "unsupported":78,
    }
    assert set(item["workstream"] for item in data["operations"]) >= {
        "implemented", "P2-C", "P2-B", "P2-D", "P2-E", "P2-F", "P3-G", "P3-H",
    }
    assert all(set(item) == {"name","status","workstream"}
               for item in data["operations"])


def test_unknown_parameter_is_rejected_before_object_transport():
    # No FastAPI/SQL is needed to prove the guard is independent of secrets.
    with pytest.raises(S3QueryError) as denied:
        validate_s3_query("DELETE", "source", False, QueryParams("policy"))
    assert denied.value.code == "NotImplemented"


@pytest.mark.parametrize("method,headers", [
    ("DELETE", {"x-amz-bypass-governance-retention": "true"}),
    ("DELETE", {"x-amz-mfa": "123456 123456"}),
    ("DELETE", {"x-amz-expected-bucket-owner": "123456789012"}),
    ("DELETE", {"x-amz-copy-source": "/guard-bucket/asset.bin"}),
    ("DELETE", {"x-amz-tagging": "class=archive"}),
    ("HEAD", {"x-amz-request-payer": "requester"}),
    ("GET", {"x-amz-server-side-encryption-customer-algorithm": "AES256"}),
])
def test_unsupported_header_contract_does_not_change_or_expose_object(
    service, method, headers,
):
    assert signed(service, "PUT", "/guard-bucket").status_code == 200
    assert signed(service, "PUT", "/guard-bucket/asset.bin",
                  b"protected-content").status_code == 200
    before = service[2].uploads
    forbidden = signed(
        service, method, "/guard-bucket/asset.bin",
        extra_headers=headers,
    )
    assert forbidden.status_code == 501, (method, headers, forbidden.text)
    assert signed(service, "GET", "/guard-bucket/asset.bin").content == b"protected-content"
    assert service[2].uploads == before


def test_real_sha256_checksum_mode_readback_for_aws_cli_and_sdk(service):
    """SDK-requested checksum headers bind the actual immutable object bytes."""
    import base64
    import hashlib

    assert signed(service, "PUT", "/guard-bucket").status_code == 200
    blob = b"checksum-bound-exact-plaintext" * 17
    put = signed(service, "PUT", "/guard-bucket/checksummed", blob)
    assert put.status_code == 200, put.text
    version = put.headers["x-amz-version-id"]
    encoded = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii")
    for method, path in (
        ("GET", "/guard-bucket/checksummed"),
        ("HEAD", "/guard-bucket/checksummed"),
        ("GET", "/guard-bucket/checksummed?versionId=" + version),
    ):
        reply = signed(service, method, path,
                       extra_headers={"x-amz-checksum-mode": "ENABLED"})
        assert reply.status_code == 200, reply.text
        assert reply.headers["x-amz-checksum-sha256"] == encoded
        assert reply.headers["x-amz-checksum-type"] == "FULL_OBJECT"
        if method == "GET":
            assert reply.content == blob
    assert signed(service, "GET", "/guard-bucket/checksummed").headers.get(
        "x-amz-checksum-sha256"
    ) is None

    for method, path, mode, expected in (
        ("GET", "/guard-bucket/checksummed", "BAD", 400),
        ("PUT", "/guard-bucket/checksummed", "ENABLED", 501),
        ("DELETE", "/guard-bucket/checksummed", "ENABLED", 501),
        ("GET", "/guard-bucket", "ENABLED", 501),
    ):
        result = signed(service, method, path, extra_headers={
            "x-amz-checksum-mode": mode,
        })
        assert result.status_code == expected, result.text
    assert signed(service, "GET", "/guard-bucket/checksummed").content == blob


def test_root_prefix_is_intentional_supported_list_buckets_parameter(service):
    assert signed(service, "PUT", "/guard-bucket").status_code == 200
    result = signed(service, "GET", "/?prefix=guard-")
    assert result.status_code == 200, result.text
    assert b"guard-bucket" in result.content
    assert service[2].uploads == service[2].downloads == 0
