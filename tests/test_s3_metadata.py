"""S3 HTTP metadata compatibility with signed real HTTP and immutable SQL manifests.

Tests verify bytes remain exclusively in Telegram, metadata survives overwrite,
CopyObject COPY/REPLACE, multipart publication, actual boto3 behavior, and
malformed/unsigned header abuse without granting partial success.
"""
from __future__ import annotations

import hashlib
import os
import socket
import threading
import time
from urllib.parse import quote

import boto3
import pytest
import uvicorn
from botocore.config import Config
from botocore.awsrequest import AWSRequest
from botocore.auth import S3SigV4Auth
from botocore.credentials import Credentials

from test_s3_gateway import service, signed
from test_s3_multipart import begin, part, complete, setup
from teledrive.db import connect_database
from teledrive.s3.object_metadata import ObjectMetadataStore
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore


def put(service, key, body, headers=None):
    return signed(service, "PUT", "/backup-bucket/" + key, body,
                  extra_headers=headers)


def headers(service, key):
    return signed(service, "HEAD", "/backup-bucket/" + key).headers


def copy(service, source, dest, **extras):
    return signed(service, "PUT", "/backup-bucket/" + dest,
                  extra_headers={"x-amz-copy-source": "/backup-bucket/" + source,
                                 **extras})


def test_metadata_roundtrip_is_exact_and_no_extra_telegram_reads(service):
    setup(service)
    body = os.urandom(32769)
    props = {
        "Content-Type": "application/pdf",
        "Cache-Control": "private, max-age=300",
        "Content-Disposition": 'attachment; filename="report.pdf"',
        "Content-Encoding": "gzip",
        "Content-Language": "en-US",
        "x-amz-meta-origin": "teledrive-e2e",
        "x-amz-meta-checksum": "annotated-only",
    }
    result = put(service, "report.pdf", body, props)
    assert result.status_code == 200, result.text
    remote = service[2]
    before = remote.downloads
    h = headers(service, "report.pdf")
    assert h["content-length"] == str(len(body))
    for field, expected in props.items():
        assert h[field.lower()] == expected
    assert remote.downloads == before
    restored = signed(service, "GET", "/backup-bucket/report.pdf",
                      extra_headers={"Range": "bytes=3-19"})
    assert restored.status_code == 206
    assert restored.content == body[3:20]
    assert restored.headers["content-type"] == "application/pdf"
    assert restored.headers["x-amz-meta-origin"] == "teledrive-e2e"


def test_immutable_metadata_isolated_between_overwrites(service):
    setup(service)
    first = put(service, "overwritten.bin", b"first", {
        "Content-Type": "text/plain", "x-amz-meta-generation": "one",
    })
    assert first.status_code == 200, first.text
    db = service[3]()
    try:
        catalog = BucketCatalog(ManifestStore(db))
        before = catalog.get("backup-bucket", "overwritten.bin").item.id
    finally:
        db.close()
    second = put(service, "overwritten.bin", b"second", {
        "Content-Type": "image/png", "x-amz-meta-generation": "two",
    })
    assert second.status_code == 200, second.text
    assert headers(service, "overwritten.bin")["x-amz-meta-generation"] == "two"
    db = service[3]()
    try:
        catalog = BucketCatalog(ManifestStore(db))
        after = catalog.get("backup-bucket", "overwritten.bin").item.id
        assert before != after
        store = ObjectMetadataStore(catalog)
        assert store.get(before)["content-type"] == "text/plain"
        assert store.get(before)["x-amz-meta-generation"] == "one"
        assert store.get(after)["content-type"] == "image/png"
    finally:
        db.close()


def test_copy_preserves_metadata_and_replace_isolation_without_media_io(service):
    setup(service)
    assert put(service, "origin", b"immutable", {
        "Content-Type": "application/json",
        "x-amz-meta-team": "s3",
    }).status_code == 200
    before = service[2].uploads
    inherited = copy(service, "origin", "clone")
    assert inherited.status_code == 200, inherited.text
    assert headers(service, "clone")["x-amz-meta-team"] == "s3"
    assert headers(service, "clone")["content-type"] == "application/json"

    replaced = copy(service, "origin", "replacement",
                    **{"x-amz-metadata-directive": "REPLACE",
                       "Content-Type": "image/jpeg", "x-amz-meta-team": "ops"})
    assert replaced.status_code == 200, replaced.text
    assert headers(service, "replacement")["x-amz-meta-team"] == "ops"
    assert headers(service, "replacement")["content-type"] == "image/jpeg"
    assert headers(service, "origin")["x-amz-meta-team"] == "s3"
    assert service[2].uploads == before
    assert signed(service, "DELETE", "/backup-bucket/origin").status_code == 204
    assert signed(service, "GET", "/backup-bucket/replacement").content == b"immutable"
    assert headers(service, "clone")["x-amz-meta-team"] == "s3"


def test_multipart_initial_metadata_commits_only_at_complete(service):
    setup(service)
    key = "multipart-metadata.bin"
    started = signed(service, "POST", "/backup-bucket/" + key + "?uploads",
                     extra_headers={"Content-Type": "application/custom",
                                    "x-amz-meta-review": "pending"})
    assert started.status_code == 200, started.text
    import xml.etree.ElementTree as ET
    uid = ET.fromstring(started.content).findtext(
        "{http://s3.amazonaws.com/doc/2006-03-01/}UploadId")
    assert uid
    assert signed(service, "HEAD", "/backup-bucket/" + key).status_code == 404
    payload = b"single-final-part"
    tag = part(service, uid, 1, payload, key=key)
    finished = complete(service, uid, [(1, tag)], key=key)
    assert finished.status_code == 200, finished.text
    h = headers(service, key)
    assert h["content-type"] == "application/custom"
    assert h["x-amz-meta-review"] == "pending"
    assert signed(service, "GET", "/backup-bucket/" + key).content == payload


@pytest.mark.parametrize("bad_props", [
    {"x-amz-meta-": "empty"},
    {"x-amz-meta-ab@cd": "bad"},
    {"x-amz-meta-note": "a" * 1030},
    {"Cache-Control": "a\nb"},
])
def test_malformed_metadata_rejected_before_telegram_upload(service, bad_props):
    setup(service)
    before = service[2].uploads
    try:
        result = put(service, "unsafe", b"never-uploaded", bad_props)
    except (ValueError, UnicodeError):
        # HTTP libraries reject raw CR/LF at wire parsing; still fail closed.
        assert "\n" in bad_props.get("Cache-Control", "")
    else:
        assert result.status_code in (400, 403), result.text
    assert service[2].uploads == before
    assert signed(service, "GET", "/backup-bucket/unsafe").status_code == 404


def test_repeated_metadata_header_and_unsigned_signedfield_refused(service):
    setup(service)
    body = b"unchanged"
    request = AWSRequest(method="PUT",
                         url="http://localhost:9000/backup-bucket/unsigned",
                         data=body, headers={
                             "x-amz-content-sha256": hashlib.sha256(body).hexdigest(),
                         })
    request.context["payload_signing_enabled"] = True
    S3SigV4Auth(Credentials(service[1].access_key, service[1].secret_key),
                "s3", "us-east-1").add_auth(request)
    h = dict(request.headers)
    h["x-amz-meta-risk"] = "unsanctioned"
    denied = service[0].put("/backup-bucket/unsigned", content=body, headers=h)
    assert denied.status_code == 403
    assert b"AccessDenied" in denied.content
    assert service[2].uploads == 0
    dupe = service[0].put(
        "/backup-bucket/unsigned", content=body,
        headers=[*dict(request.headers).items(),
                 ("x-amz-meta-note", "one"), ("x-amz-meta-note", "two")],
    )
    assert dupe.status_code in (400, 403)
    assert service[2].uploads == 0


def test_real_boto3_metadata_over_http_port(service):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(16)
    import web.s3 as gateway
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
        client.create_bucket(Bucket="sdk-metadata")
        payload = os.urandom(3101)
        assert client.put_object(Bucket="sdk-metadata", Key="source",
                                 Body=payload, ContentType="image/svg+xml",
                                 Metadata={"project": "TeleDrive", "mode": "telegram"})[
                                     "ResponseMetadata"]["HTTPStatusCode"] == 200
        assert client.head_object(Bucket="sdk-metadata", Key="source")[
            "Metadata"] == {"project": "TeleDrive", "mode": "telegram"}
        client.copy_object(Bucket="sdk-metadata", Key="copy",
                           CopySource={"Bucket": "sdk-metadata", "Key": "source"})
        assert client.head_object(Bucket="sdk-metadata", Key="copy")[
            "ContentType"] == "image/svg+xml"
        client.copy_object(Bucket="sdk-metadata", Key="replace",
                           CopySource={"Bucket": "sdk-metadata", "Key": "source"},
                           MetadataDirective="REPLACE", ContentType="text/plain",
                           Metadata={"project": "Different"})
        assert client.head_object(Bucket="sdk-metadata", Key="replace")[
            "Metadata"] == {"project": "Different"}
        assert client.get_object(Bucket="sdk-metadata", Key="replace")[
            "Body"].read() == payload
        assert service[2].uploads == 1
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()
