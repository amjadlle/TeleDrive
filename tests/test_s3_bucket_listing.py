"""Real SigV4 + SQL ListBuckets pagination (AWS S3 private-region contract).

Object bytes never reach Telegram: ListBuckets accesses only SQL metadata.
Cross-key tampering and reuse of prefix/region cursors must fail closed.
"""
from __future__ import annotations

import hashlib
import socket
import threading
import time
from xml.etree import ElementTree as ET
from urllib.parse import quote

import boto3
import pytest
import uvicorn
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import insert

import web.s3 as gateway
from teledrive.s3.credentials import S3CredentialStore
from teledrive.storage.catalog import BucketCatalog, buckets
from teledrive.storage.manifest import ManifestStore
from test_s3_gateway import service, signed

_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _xml_bucket_names(reply) -> tuple[list[str], str | None]:
    assert reply.status_code == 200, reply.text
    root = ET.fromstring(reply.content)
    assert root.tag == _NS + "ListAllMyBucketsResult"
    return [el.text for el in root.findall(_NS + "Buckets/" + _NS + "Bucket/" + _NS + "Name")], (
        root.findtext(_NS + "ContinuationToken")
    )


def test_unpaginated_buckets_contain_sql_creation_date_and_stable_owner(service):
    assert signed(service, "PUT", "/list-ci-apple").status_code == 200
    assert signed(service, "PUT", "/list-ci-banana").status_code == 200
    answer = signed(service, "GET", "/")
    names, token = _xml_bucket_names(answer)
    assert names == ["list-ci-apple", "list-ci-banana"] and token is None
    root = ET.fromstring(answer.content)
    owner_id = root.findtext(_NS + "Owner/" + _NS + "ID")
    assert owner_id and len(owner_id) == 64
    created = root.findall(_NS + "Buckets/" + _NS + "Bucket/" + _NS + "CreationDate")
    assert len(created) == 2
    assert all(date.text and date.text.endswith("Z") for date in created)
    assert all(date.text[:4] == time.strftime("%Y", time.gmtime()) for date in created)
    assert not root.findall(_NS + "Buckets/" + _NS + "Bucket/" + _NS + "BucketRegion")
    paged = signed(service, "GET", "/?max-buckets=2")
    assert ET.fromstring(paged.content).findtext(_NS + "Owner/" + _NS + "ID") == owner_id
    assert [r.text for r in ET.fromstring(paged.content).findall(
        _NS + "Buckets/" + _NS + "Bucket/" + _NS + "BucketRegion"
    )] == ["us-east-1", "us-east-1"]
    assert service[2].uploads == service[2].downloads == 0


def test_s3_list_buckets_paginated_prefix_and_page_boundaries(service):
    names = ["list-ci-alpha", "list-ci-beta", "list-ci-gamma", "other-bucket"]
    for name in reversed(names):
        assert signed(service, "PUT", "/" + name).status_code == 200
    requested = []
    token = None
    for index in range(4):
        query = "/?max-buckets=1&prefix=list-ci-&bucket-region=us-east-1"
        if token:
            query += "&continuation-token=" + quote(token, safe="")
        result = signed(service, "GET", query)
        found, next_token = _xml_bucket_names(result)
        if index < 3:
            assert len(found) == 1
        requested.extend(found)
        token = next_token
        if token is None:
            break
    assert requested == sorted([n for n in names if n.startswith("list-ci-")])
    assert token is None
    assert _xml_bucket_names(signed(service, "GET",
         "/?prefix=OTHER&max-buckets=1"))[0] == []
    assert _xml_bucket_names(signed(service, "GET",
         "/?prefix=list-ci-&max-buckets=10000"))[0] == requested


def test_cursor_valid_with_concurrent_bucket_create_but_not_duplicate(service):
    for name in ("list-ci-a", "list-ci-c"):
        assert signed(service, "PUT", "/" + name).status_code == 200
    first, token = _xml_bucket_names(
        signed(service, "GET", "/?prefix=list-ci-&max-buckets=1")
    )
    assert first == ["list-ci-a"] and token
    # Insert a record lexicographically after the consumed high-water mark.
    assert signed(service, "PUT", "/list-ci-b").status_code == 200
    second, token2 = _xml_bucket_names(signed(
        service, "GET", "/?max-buckets=1&prefix=list-ci-&continuation-token="
        + quote(token, safe=""),
    ))
    assert second == ["list-ci-b"] and token2
    last, token3 = _xml_bucket_names(signed(
        service, "GET", "/?max-buckets=1&prefix=list-ci-&continuation-token="
        + quote(token2, safe=""),
    ))
    assert last == ["list-ci-c"] and token3 is None


def test_list_buckets_cursor_hmac_tamper_context_and_permission_scope(service):
    for name in ("list-ci-a", "list-ci-b"):
        assert signed(service, "PUT", "/" + name).status_code == 200
    _, token = _xml_bucket_names(signed(service, "GET", "/?max-buckets=1"))
    assert token
    db = service[3]()
    try:
        keys = S3CredentialStore(db, service[4])
        owner2 = keys.create("second-list-key", ["s3:ListAllMyBuckets"])
        read_only = keys.create("object-read-only", ["s3:GetObject"])
    finally:
        db.close()
    trials = [
        "/?max-buckets=1&continuation-token=" + quote(token + "tampered"),
        "/?max-buckets=1&prefix=different&continuation-token=" + quote(token),
        "/?max-buckets=1&bucket-region=us-east-2&continuation-token=" + quote(token),
    ]
    for path in trials:
        denied = signed(service, "GET", path)
        assert denied.status_code == 400, denied.text
        assert b"Invalid" in denied.content
    other = signed(service, "GET", "/?continuation-token=" + quote(token),
                   access=owner2.access_key, secret=owner2.secret_key)
    assert other.status_code == 400
    assert signed(service, "GET", "/", access=owner2.access_key,
                  secret=owner2.secret_key).status_code == 200
    assert signed(service, "GET", "/", access=read_only.access_key,
                  secret=read_only.secret_key).status_code == 403
    assert service[0].get("/").status_code == 403
    assert service[2].uploads == service[2].downloads == 0


@pytest.mark.parametrize("path,expected", [
    ("/?max-buckets=0", 400),
    ("/?max-buckets=-1", 400),
    ("/?max-buckets=10001", 400),
    ("/?max-buckets=2.0", 400),
    ("/?max-buckets=2&max-buckets=3", 400),
    ("/?prefix=a&prefix=b", 400),
    ("/?continuation-token=", 400),
    ("/?continuation-token=" + "x"*1025, 400),
    ("/?bucket-region=eu-west-1", 400),
    ("/?bucket-region=us-east-1&bucket-region=us-east-1", 400),
    ("/?prefix=" + "z"*256, 400),
    ("/?acl", 501),
    ("/?policy", 501),
    ("/?list-type=2", 501),
    ("/?x-id=ListBuckets&unexpected=x", 501),
])
def test_invalid_root_list_parameters_fail_closed(service, path, expected):
    response = signed(service, "GET", path)
    assert response.status_code == expected, (path, response.text)
    assert service[2].uploads == service[2].downloads == 0


def test_sql_page_limit_10000_and_10001st_record(service):
    """Do not load all buckets before limiting even for a default root GET."""
    db = service[3]()
    try:
        BucketCatalog(ManifestStore(db))  # create shared SQL bucket schema
        db.connection.execute(insert(buckets), [
            {"name": f"test-list-{i:05d}", "created_at": 1714000000}
            for i in range(10001)
        ])
        db.commit()
    finally:
        db.close()
    first, cursor = _xml_bucket_names(signed(service, "GET", "/"))
    assert len(first) == 10000 and cursor
    assert first[0] == "test-list-00000" and first[-1] == "test-list-09999"
    tail, final = _xml_bucket_names(
        signed(service, "GET", "/?max-buckets=10000&continuation-token=" + quote(cursor)),
    )
    assert tail == ["test-list-10000"] and final is None


def test_boto3_list_buckets_parameters_actual_http(service):
    assert signed(service, "PUT", "/list-ci-a").status_code == 200
    assert signed(service, "PUT", "/list-ci-b").status_code == 200
    assert signed(service, "PUT", "/other-bucket").status_code == 200
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        gateway.app, host="127.0.0.1", port=port, lifespan="off",
        log_level="error",
    ))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                              daemon=True)
    worker.start()
    try:
        end = time.monotonic() + 10
        while not server.started and time.monotonic() < end:
            time.sleep(0.03)
        assert server.started
        key = service[1]
        sdk = boto3.client(
            "s3", endpoint_url=f"http://127.0.0.1:{port}",
            region_name="us-east-1", aws_access_key_id=key.access_key,
            aws_secret_access_key=key.secret_key,
            config=Config(signature_version="s3v4",
                          s3={"addressing_style": "path"},
                          retries={"max_attempts": 0}),
        )
        page1 = sdk.list_buckets(MaxBuckets=1, Prefix="list-ci-", BucketRegion="us-east-1")
        assert len(page1["Buckets"]) == 1
        assert page1["Buckets"][0]["Name"] == "list-ci-a"
        assert page1["Buckets"][0]["BucketRegion"] == "us-east-1"
        assert page1["Buckets"][0]["CreationDate"].year >= 2025
        assert page1["Owner"]["ID"]
        page2 = sdk.list_buckets(
            MaxBuckets=1, Prefix="list-ci-", BucketRegion="us-east-1",
            ContinuationToken=page1["ContinuationToken"],
        )
        assert [b["Name"] for b in page2["Buckets"]] == ["list-ci-b"]
        assert "ContinuationToken" not in page2
        all_buckets = sdk.list_buckets()
        assert {b["Name"] for b in all_buckets["Buckets"]} == {
            "list-ci-a", "list-ci-b", "other-bucket",
        }
        with pytest.raises(ClientError):
            sdk.list_buckets(MaxBuckets=10001)
        assert service[2].uploads == service[2].downloads == 0
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        sock.close()
        assert not worker.is_alive()
