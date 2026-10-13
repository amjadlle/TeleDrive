"""Real signed S3 CopyObject contracts with Telegram-only immutable data.

These tests verify source-read AND destination-write scope, ETag preconditions,
zero Telegram transfers, cross-bucket durability and alias correctness.
"""
from __future__ import annotations

import hashlib
from urllib.parse import quote

import boto3
from botocore.config import Config
import pytest
from sqlalchemy import select

from test_s3_gateway import service, signed
from test_s3_multipart import begin, complete, part
from teledrive.s3.credentials import S3CredentialStore
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore, chunks, object_aliases


def copy(svc, source, target, **headers):
    return signed(
        svc, "PUT", target,
        extra_headers={"x-amz-copy-source": source, **headers},
    )


def test_metadata_only_copy_durable_after_source_delete_and_overwrite(service):
    assert signed(service, "PUT", "/origin-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    body = b"telegram-referenced-payload" * 200
    assert signed(service, "PUT", "/origin-bucket/original.bin", body).status_code == 200
    before_uploads = service[2].uploads
    before_downloads = service[2].downloads
    response = copy(service, "/origin-bucket/original.bin", "/backup-bucket/copy.bin")
    assert response.status_code == 200, response.text
    assert service[2].uploads == before_uploads
    assert service[2].downloads == before_downloads
    etag = '"' + hashlib.sha256(body).hexdigest() + '"'
    assert etag.encode() in response.content
    assert signed(service, "HEAD", "/backup-bucket/copy.bin").headers["etag"] == etag

    connection = service[3]()
    try:
        manifests = ManifestStore(connection)
        original = BucketCatalog(manifests).get("origin-bucket", "original.bin").item
        clone = BucketCatalog(manifests).get("backup-bucket", "copy.bin").item
        assert original.id != clone.id
        alias = connection.connection.execute(
            select(object_aliases.c.source_object_id)
            .where(object_aliases.c.object_id == clone.id)
        ).scalar_one()
        assert alias == original.id
        assert len(manifests.parts(clone.id)) == len(manifests.parts(original.id)) == 1
        # A copied object must never duplicate the same Telegram reference
        # and violate the global unique-message constraint.
        assert connection.connection.execute(select(chunks.c.object_id)).all() == [(original.id,)]
    finally:
        connection.close()

    assert signed(service, "DELETE", "/origin-bucket/original.bin").status_code == 204
    assert signed(service, "PUT", "/origin-bucket/original.bin", b"replaced").status_code == 200
    assert signed(service, "GET", "/backup-bucket/copy.bin").content == body
    assert signed(service, "GET", "/origin-bucket/original.bin").content == b"replaced"


def test_copy_chain_flattens_and_remains_readable_when_heads_disappear(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    payload = b"chain-immutable" * 333
    assert signed(service, "PUT", "/backup-bucket/first", payload).status_code == 200
    assert copy(service, "/backup-bucket/first", "/backup-bucket/second").status_code == 200
    assert copy(service, "/backup-bucket/second", "/backup-bucket/third").status_code == 200
    assert signed(service, "DELETE", "/backup-bucket/first").status_code == 204
    assert signed(service, "DELETE", "/backup-bucket/second").status_code == 204
    assert signed(service, "GET", "/backup-bucket/third").content == payload
    conn = service[3]()
    try:
        manifests = ManifestStore(conn)
        head = BucketCatalog(manifests).get("backup-bucket", "third")
        first_id = manifests.resolve_content_id(head.item.id)
        assert first_id != head.item.id
        assert conn.connection.execute(select(object_aliases.c.source_object_id)
                    .where(object_aliases.c.object_id == head.item.id)).scalar_one() == first_id
    finally:
        conn.close()


def test_copy_preconditions_are_signed_and_failed_copy_never_publishes(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    data = b"safe-current"
    assert signed(service, "PUT", "/backup-bucket/source", data).status_code == 200
    assert signed(service, "PUT", "/backup-bucket/dest", b"previous").status_code == 200
    actual = '"' + hashlib.sha256(data).hexdigest() + '"'
    before = service[2].uploads
    unmatched = copy(service, "/backup-bucket/source", "/backup-bucket/dest",
                     **{"x-amz-copy-source-if-match": '"not-the-etag"'})
    assert unmatched.status_code == 412
    assert b"PreconditionFailed" in unmatched.content
    matched_none = copy(service, "/backup-bucket/source", "/backup-bucket/dest",
                        **{"x-amz-copy-source-if-none-match": actual})
    assert matched_none.status_code == 412
    assert signed(service, "GET", "/backup-bucket/dest").content == b"previous"
    works = copy(service, "/backup-bucket/source", "/backup-bucket/dest",
                 **{"x-amz-copy-source-if-match": actual})
    assert works.status_code == 200, works.text
    assert service[2].uploads == before
    assert signed(service, "GET", "/backup-bucket/dest").content == data


def test_copy_requires_both_get_and_put_permissions(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/source", b"read-permission").status_code == 200
    db = service[3]()
    try:
        store = S3CredentialStore(db, service[4])
        only_write = store.create("copy-denied", ["s3:PutObject"])
        only_read = store.create("copy-readonly", ["s3:GetObject"])
    finally:
        db.close()
    denied = signed(service, "PUT", "/backup-bucket/out",
                    extra_headers={"x-amz-copy-source": "/backup-bucket/source"},
                    access=only_write.access_key, secret=only_write.secret_key)
    assert denied.status_code == 403
    assert b"AccessDenied" in denied.content
    rejected = signed(service, "PUT", "/backup-bucket/out",
                      extra_headers={"x-amz-copy-source": "/backup-bucket/source"},
                      access=only_read.access_key, secret=only_read.secret_key)
    assert rejected.status_code == 403
    assert signed(service, "GET", "/backup-bucket/out").status_code == 404


def test_copy_does_not_ignore_unsupported_metadata_changes(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/source", b"payload").status_code == 200
    before = service[2].uploads
    replacement = copy(service, "/backup-bucket/source", "/backup-bucket/out",
                       **{"x-amz-metadata-directive": "REPLACE"})
    assert replacement.status_code == 501
    ignored_range = copy(service, "/backup-bucket/source", "/backup-bucket/out",
                         **{"x-amz-copy-source-range": "bytes=0-2"})
    assert ignored_range.status_code == 501
    assert service[2].uploads == before
    assert signed(service, "GET", "/backup-bucket/out").status_code == 404


@pytest.mark.parametrize("key", ["folder/a%20space.txt", "caf%C3%A9.bin", "percent%2525.txt"])
def test_copy_preserves_percent_encoded_source_names(service, key):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    body = key.encode()
    assert signed(service, "PUT", "/backup-bucket/"+key, body).status_code == 200
    response = copy(service, "/backup-bucket/"+key, "/backup-bucket/destination.bin")
    assert response.status_code == 200, response.text
    assert signed(service, "GET", "/backup-bucket/destination.bin").content == body


def test_copy_of_multipart_keeps_composite_etag_without_new_media(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    uid = begin(service, key="mpu.bin")
    etag = part(service, uid, 1, b"multipart-one-part", key="mpu.bin")
    finished = complete(service, uid, [(1, etag)], key="mpu.bin")
    assert finished.status_code == 200, finished.text
    composite = '"' + hashlib.md5(
        bytes.fromhex(etag.strip('"')), usedforsecurity=False
    ).hexdigest() + '-1"'
    before = service[2].uploads
    response = copy(service, "/backup-bucket/mpu.bin", "/backup-bucket/clone.bin")
    assert response.status_code == 200, response.text
    assert composite.encode() in response.content
    assert service[2].uploads == before
    assert signed(service, "HEAD", "/backup-bucket/clone.bin").headers["etag"] == composite
    assert signed(service, "GET", "/backup-bucket/clone.bin").content == b"multipart-one-part"


def test_real_boto3_copy_object_over_http(service):
    import socket
    import threading
    import time
    import uvicorn
    import web.s3 as gateway

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(16)
    server = uvicorn.Server(uvicorn.Config(
        gateway.app, host="127.0.0.1", port=port, lifespan="off", log_level="error"
    ))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
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
        client.create_bucket(Bucket="sdk-copy")
        body = b"real-http-s3-copy" * 19
        client.put_object(Bucket="sdk-copy", Key="old", Body=body)
        before = service[2].uploads
        copied = client.copy_object(Bucket="sdk-copy", Key="new",
                                    CopySource={"Bucket":"sdk-copy", "Key":"old"})
        assert copied["CopyObjectResult"]["ETag"] == '"' + hashlib.sha256(body).hexdigest() + '"'
        assert service[2].uploads == before
        client.delete_object(Bucket="sdk-copy", Key="old")
        assert client.get_object(Bucket="sdk-copy", Key="new")["Body"].read() == body
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()
