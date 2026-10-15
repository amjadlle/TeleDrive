"""Legacy S3 ListObjectsV1 contract, including CommonPrefixes paging.

Preserve ListObjectsV2 signed opaque cursors, but accept older SDK clients
using key-based Marker/NextMarker without repeating directories across pages.
"""
from __future__ import annotations

import socket
import threading
import time
from urllib.parse import quote
from xml.etree import ElementTree as ET

import boto3
import uvicorn
from botocore.config import Config

from tests.s3.test_s3_gateway import service, signed

NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def list_v1(service, query=""):
    return signed(service, "GET", "/backup-bucket" + (("?" + query) if query else ""))


def get_data(response):
    assert response.status_code == 200, response.text
    root = ET.fromstring(response.content)
    return root, (
        [c.findtext(NS+"Key") for c in root.findall(NS+"Contents")],
        [c.findtext(NS+"Prefix") for c in root.findall(NS+"CommonPrefixes")],
    )


def test_v1_key_marker_pagination_without_delimiter(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    for key in ("a.txt", "b.txt", "c.txt"):
        assert signed(service, "PUT", "/backup-bucket/" + key, key.encode()).status_code == 200
    first, (keys, prefixes) = get_data(list_v1(service, "max-keys=2"))
    assert keys == ["a.txt", "b.txt"]
    assert prefixes == []
    assert first.findtext(NS+"IsTruncated") == "true"
    assert first.findtext(NS+"KeyCount") is None
    assert first.findtext(NS+"Marker") in ("", None)
    marker = first.findtext(NS+"NextMarker") or keys[-1]
    last, (next_keys, _) = get_data(list_v1(
        service, "max-keys=2&marker=" + quote(marker, safe="")
    ))
    assert next_keys == ["c.txt"]
    assert last.findtext(NS+"IsTruncated") == "false"


def test_v1_folder_marker_never_repeats_common_prefix(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    for key in ("a/1", "a/2", "a/3", "b/one", "root.txt"):
        assert signed(service, "PUT", "/backup-bucket/" + key, key.encode()).status_code == 200
    all_prefixes = []
    all_keys = []
    marker = ""
    for _ in range(5):
        query = "delimiter=%2F&max-keys=1"
        if marker:
            query += "&marker=" + quote(marker, safe="")
        root, (keys, dirs) = get_data(list_v1(service, query))
        all_prefixes.extend(dirs)
        all_keys.extend(keys)
        marker = root.findtext(NS+"NextMarker")
        if root.findtext(NS+"IsTruncated") == "false":
            break
        assert marker
    else:
        raise AssertionError("V1 folder marker pagination never terminated")
    assert all_prefixes == ["a/", "b/"]
    assert all_keys == ["root.txt"]


def test_v1_and_v2_cursor_formats_must_not_be_interchangeable(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    for key in ("a", "b", "c"):
        assert signed(service, "PUT", "/backup-bucket/" + key, b"x").status_code == 200
    invalid_v1 = list_v1(service, "continuation-token=fake")
    assert invalid_v1.status_code == 400
    assert b"InvalidArgument" in invalid_v1.content
    invalid_v2 = list_v1(service, "list-type=2&marker=a")
    assert invalid_v2.status_code == 400
    assert b"InvalidArgument" in invalid_v2.content
    good_v2 = list_v1(service, "list-type=2&max-keys=1")
    root, _ = get_data(good_v2)
    assert root.findtext(NS+"NextContinuationToken")


def test_real_boto3_legacy_list_objects_over_http(service):
    import web.s3 as gateway

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(16)
    server = uvicorn.Server(uvicorn.Config(
        gateway.app, host="127.0.0.1", port=port, lifespan="off", log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        client = boto3.client(
            "s3", endpoint_url=f"http://127.0.0.1:{port}",
            aws_access_key_id=service[1].access_key,
            aws_secret_access_key=service[1].secret_key,
            region_name="us-east-1",
            config=Config(signature_version="s3v4",
                          s3={"addressing_style": "path", "payload_signing_enabled": True},
                          request_checksum_calculation="when_required",
                          response_checksum_validation="when_required",
                          retries={"max_attempts": 0}),
        )
        client.create_bucket(Bucket="sdk-list-v1")
        for key in ("prefix/café.txt", "prefix/a.txt", "other/file"):
            client.put_object(Bucket="sdk-list-v1", Key=key, Body=key.encode())
        result = client.list_objects(Bucket="sdk-list-v1", Prefix="prefix/")
        assert {row["Key"] for row in result["Contents"]} == {
            "prefix/café.txt", "prefix/a.txt",
        }
        nested = client.list_objects(Bucket="sdk-list-v1", Delimiter="/")
        assert {row["Prefix"] for row in nested["CommonPrefixes"]} == {
            "other/", "prefix/",
        }
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def test_v1_v2_url_encoding_preserves_xml_control_and_unicode_keys(service):
    from urllib.parse import unquote
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    raw = "nested/line\nbreak-ö.txt"
    path = quote(raw, safe="/")
    assert signed(service, "PUT", "/backup-bucket/" + path, b"control-byte-file").status_code == 200
    for query in ("encoding-type=url", "list-type=2&encoding-type=url"):
        result = list_v1(service, query)
        assert result.status_code == 200, result.text
        root = ET.fromstring(result.content)
        assert root.findtext(NS+"EncodingType") == "url"
        encoded_key = root.findtext(NS+"Contents/"+NS+"Key")
        assert "\n" not in encoded_key
        assert "%0A" in encoded_key
        assert "%C3%B6" in encoded_key
        assert unquote(encoded_key) == raw
    invalid = list_v1(service, "encoding-type=json")
    assert invalid.status_code == 400
    assert b"InvalidArgument" in invalid.content


def test_signed_url_encoded_listing_with_delimiter(service):
    from urllib.parse import unquote
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    for key in ("nested/one", "nested/two", "plain"):
        assert signed(service, "PUT", "/backup-bucket/"+key, key.encode()).status_code == 200
    response = list_v1(service, "delimiter=%2F&encoding-type=url")
    root, (keys, dirs) = get_data(response)
    assert root.findtext(NS+"EncodingType") == "url"
    assert [unquote(entry) for entry in keys] == ["plain"]
    assert [unquote(entry) for entry in dirs] == ["nested/"]
