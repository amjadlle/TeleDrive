"""S3 GetObjectAttributes — strict signed metadata-only compatibility tests."""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import threading
import time
from xml.etree import ElementTree as ET

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import uvicorn

import web.s3 as gateway
from teledrive.s3.credentials import S3CredentialStore
from test_s3_gateway import service, signed


def test_signed_object_attributes_return_exact_metadata_without_telegram_get(service):
    assert signed(service, "PUT", "/attrs-bucket").status_code == 200
    payload = os.urandom(12003)
    put = signed(service, "PUT", "/attrs-bucket/data.bin", payload)
    assert put.status_code == 200
    fake = service[2]
    before = fake.downloads
    result = signed(
        service, "GET", "/attrs-bucket/data.bin?attributes",
        extra_headers={"x-amz-object-attributes": "ETag,ObjectSize,Checksum,StorageClass"},
    )
    assert result.status_code == 200, result.text
    assert b"<ObjectSize>12003</ObjectSize>" in result.content
    assert put.headers["etag"].encode() in result.content
    assert b"<StorageClass>STANDARD</StorageClass>" in result.content
    assert b"<ChecksumType>FULL_OBJECT</ChecksumType>" in result.content
    assert base64.b64encode(hashlib.sha256(payload).digest()) in result.content
    assert result.headers["x-amz-version-id"] == put.headers["x-amz-version-id"]
    assert "last-modified" in result.headers
    assert fake.downloads == before, "metadata lookup must not download Telegram chunks"


def test_attributes_require_separate_permission_and_header_signature(service):
    assert signed(service, "PUT", "/attrs-bucket").status_code == 200
    assert signed(service, "PUT", "/attrs-bucket/data", b"secret").status_code == 200
    db = service[3]()
    try:
        restricted = S3CredentialStore(db, service[4]).create(
            "reader-no-attributes", ["s3:GetObject"],
        )
    finally:
        db.close()
    denied = signed(
        service, "GET", "/attrs-bucket/data?attributes",
        extra_headers={"x-amz-object-attributes": "ETag,ObjectSize"},
        access=restricted.access_key, secret=restricted.secret_key,
    )
    assert denied.status_code == 403
    assert b"AccessDenied" in denied.content

    # Sign GET normally, then inject a new unsigned attributes header.
    # No amount of valid read permission can bypass SignedHeaders enforcement.
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
    auth = AWSRequest(method="GET", url="http://localhost:9000/attrs-bucket/data?attributes")
    S3SigV4Auth(
        Credentials(service[1].access_key, service[1].secret_key),
        "s3", "us-east-1",
    ).add_auth(auth)
    headers = dict(auth.headers)
    headers["x-amz-object-attributes"] = "ETag,ObjectSize"
    forged = service[0].get("/attrs-bucket/data?attributes", headers=headers)
    assert forged.status_code == 403
    assert b"AccessDenied" in forged.content


def test_unsupported_or_duplicate_attributes_refuse_false_success(service):
    assert signed(service, "PUT", "/attrs-bucket").status_code == 200
    assert signed(service, "PUT", "/attrs-bucket/data", b"data").status_code == 200
    downloads = service[2].downloads
    for requested, code, status in (
        ("ObjectParts", b"NotImplemented", 501),
        ("ObjectParts,ETag", b"NotImplemented", 501),
        ("UnknownAttribute", b"NotImplemented", 501),
        ("ETag,ETag", b"InvalidArgument", 400),
        ("", b"InvalidArgument", 400),
    ):
        response = signed(
            service, "GET", "/attrs-bucket/data?attributes",
            extra_headers={"x-amz-object-attributes": requested},
        )
        assert response.status_code == status, (requested, response.text)
        assert code in response.content
    assert service[2].downloads == downloads


def test_real_boto3_get_object_attributes_over_live_http(service):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(16)
    server = uvicorn.Server(uvicorn.Config(
        gateway.app, host="127.0.0.1", port=port, lifespan="off", log_level="error",
    ))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        creds = service[1]
        sdk = boto3.client(
            "s3", endpoint_url=f"http://127.0.0.1:{port}",
            aws_access_key_id=creds.access_key,
            aws_secret_access_key=creds.secret_key,
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path", "payload_signing_enabled": True},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"max_attempts": 0},
            ),
        )
        sdk.create_bucket(Bucket="sdk-attributes")
        body = b"telegram-metadata-only" * 97
        put = sdk.put_object(Bucket="sdk-attributes", Key="item", Body=body)
        before = service[2].downloads
        attributes = sdk.get_object_attributes(
            Bucket="sdk-attributes", Key="item",
            ObjectAttributes=["ETag", "ObjectSize", "Checksum", "StorageClass"],
        )
        assert attributes["ObjectSize"] == len(body)
        assert attributes["ETag"] == put["ETag"]
        assert attributes["VersionId"] == put["VersionId"]
        assert attributes["StorageClass"] == "STANDARD"
        assert attributes["Checksum"]["ChecksumSHA256"] == base64.b64encode(
            hashlib.sha256(body).digest()
        ).decode("ascii")
        assert attributes["Checksum"]["ChecksumType"] == "FULL_OBJECT"
        assert attributes["LastModified"]
        assert service[2].downloads == before
        try:
            sdk.get_object_attributes(
                Bucket="sdk-attributes", Key="item",
                ObjectAttributes=["ObjectParts"],
            )
        except ClientError as exc:
            assert exc.response["Error"]["Code"] == "NotImplemented"
            assert exc.response["ResponseMetadata"]["HTTPStatusCode"] == 501
        else:
            raise AssertionError("Unsupported S3 multipart ObjectParts claimed success")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def test_historical_attributes_use_exact_published_version_not_current_head(service):
    assert signed(service, "PUT", "/attrs-bucket").status_code == 200
    old = b"first-immutable-plaintext" * 19
    new = b"second-immutable-plaintext" * 11
    first = signed(service, "PUT", "/attrs-bucket/history", old)
    second = signed(service, "PUT", "/attrs-bucket/history", new)
    assert first.status_code == second.status_code == 200
    old_id = first.headers["x-amz-version-id"]
    new_id = second.headers["x-amz-version-id"]
    downloads = service[2].downloads
    ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"

    for version_id, body in ((old_id, old), (new_id, new)):
        reply = signed(
            service, "GET", f"/attrs-bucket/history?attributes&versionId={version_id}",
            extra_headers={
                "x-amz-object-attributes": "Checksum,ETag,ObjectSize,StorageClass",
            },
        )
        assert reply.status_code == 200, reply.text
        assert reply.headers["x-amz-version-id"] == version_id
        root = ET.fromstring(reply.content)
        assert root.findtext(ns + "ObjectSize") == str(len(body))
        assert root.findtext(ns + "Checksum/" + ns + "ChecksumSHA256") == (
            base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
        )
        assert root.findtext(ns + "Checksum/" + ns + "ChecksumType") == "FULL_OBJECT"
        assert root.findtext(ns + "StorageClass") == "STANDARD"

    for path, code in (
        (f"/attrs-bucket/other-key?attributes&versionId={old_id}", 404),
        ("/attrs-bucket/history?attributes&versionId=missing", 404),
        (f"/attrs-bucket/history?attributes&versionId={old_id}&versionId={new_id}", 400),
        (f"/attrs-bucket/history?versionId={old_id}&attributes&tagging", 501),
    ):
        reply = signed(
            service, "GET", path,
            extra_headers={"x-amz-object-attributes": "ETag"},
        )
        assert reply.status_code == code, (path, reply.text)

    assert signed(service, "DELETE", "/attrs-bucket/history").status_code == 204
    marker = signed(
        service, "GET", "/attrs-bucket/history?attributes",
        extra_headers={"x-amz-object-attributes": "ETag"},
    )
    assert marker.status_code == 404 and marker.headers["x-amz-delete-marker"] == "true"
    historical = signed(
        service, "GET", f"/attrs-bucket/history?attributes&versionId={old_id}",
        extra_headers={"x-amz-object-attributes": "ETag"},
    )
    assert historical.status_code == 200
    assert signed(service, "DELETE", f"/attrs-bucket/history?versionId={old_id}").status_code == 204
    removed = signed(
        service, "GET", f"/attrs-bucket/history?attributes&versionId={old_id}",
        extra_headers={"x-amz-object-attributes": "ETag"},
    )
    assert removed.status_code == 404
    assert service[2].downloads == downloads


def test_object_parts_pagination_headers_reject_instead_of_losing_data(service):
    assert signed(service, "PUT", "/attrs-bucket").status_code == 200
    assert signed(service, "PUT", "/attrs-bucket/key", b"contents").status_code == 200
    before = service[2].downloads
    for header, value in (
        ("x-amz-max-parts", "1"),
        ("x-amz-part-number-marker", "1"),
    ):
        response = signed(
            service, "GET", "/attrs-bucket/key?attributes",
            extra_headers={"x-amz-object-attributes": "ETag,ObjectSize",
                           header: value},
        )
        assert response.status_code == 501
        assert b"NotImplemented" in response.content
    assert service[2].downloads == before
