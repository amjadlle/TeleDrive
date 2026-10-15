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
        single = sdk.get_object_attributes(
            Bucket="sdk-attributes", Key="item", ObjectAttributes=["ObjectParts"],
        )
        assert "ObjectParts" not in single, "A simple PUT must not expose Telegram chunks as S3 parts"

        # Real boto3 GetObjectAttributes must parse the AWS-shaped XML and
        # use header pagination, not the ListParts query parameters.
        created = sdk.create_multipart_upload(Bucket="sdk-attributes", Key="multi")
        uid = created["UploadId"]
        first_bytes = b"x" * (5 * 1024 * 1024)
        last_bytes = b"genuine-second-part" * 33
        p1 = sdk.upload_part(
            Bucket="sdk-attributes", Key="multi", UploadId=uid,
            PartNumber=1, Body=first_bytes,
        )
        p2 = sdk.upload_part(
            Bucket="sdk-attributes", Key="multi", UploadId=uid,
            PartNumber=2, Body=last_bytes,
        )
        completed = sdk.complete_multipart_upload(
            Bucket="sdk-attributes", Key="multi", UploadId=uid,
            MultipartUpload={"Parts": [
                {"PartNumber": 1, "ETag": p1["ETag"]},
                {"PartNumber": 2, "ETag": p2["ETag"]},
            ]},
        )
        old_version = sdk.head_object(Bucket="sdk-attributes", Key="multi")["VersionId"]
        before = service[2].downloads
        page1 = sdk.get_object_attributes(
            Bucket="sdk-attributes", Key="multi",
            ObjectAttributes=["ETag", "ObjectParts", "Checksum"],
            MaxParts=1,
        )
        parts1 = page1["ObjectParts"]
        assert parts1["TotalPartsCount"] == 2
        assert parts1["IsTruncated"] is True
        assert parts1["NextPartNumberMarker"] == 1
        assert parts1["Parts"][0]["PartNumber"] == 1
        assert parts1["Parts"][0]["Size"] == len(first_bytes)
        assert parts1["Parts"][0]["ChecksumSHA256"] == base64.b64encode(
            hashlib.sha256(first_bytes).digest()
        ).decode("ascii")
        assert page1["ETag"] == completed["ETag"]

        page2 = sdk.get_object_attributes(
            Bucket="sdk-attributes", Key="multi",
            ObjectAttributes=["ObjectParts"], MaxParts=1, PartNumberMarker=1,
        )["ObjectParts"]
        assert page2["IsTruncated"] is False
        assert page2["Parts"][0]["PartNumber"] == 2
        assert page2["Parts"][0]["Size"] == len(last_bytes)
        assert page2["Parts"][0]["ChecksumSHA256"] == base64.b64encode(
            hashlib.sha256(last_bytes).digest()
        ).decode("ascii")
        assert service[2].downloads == before, "Attribute pagination must not read Telegram"

        sdk.create_bucket(Bucket="sdk-attributes-copy")
        sdk.copy_object(
            Bucket="sdk-attributes-copy", Key="clone",
            CopySource={"Bucket": "sdk-attributes", "Key": "multi"},
        )
        clone = sdk.get_object_attributes(
            Bucket="sdk-attributes-copy", Key="clone",
            ObjectAttributes=["ObjectParts", "ETag"],
        )
        assert clone["ObjectParts"]["TotalPartsCount"] == 2
        assert [p["PartNumber"] for p in clone["ObjectParts"]["Parts"]] == [1, 2]
        assert clone["ETag"] == completed["ETag"]
        sdk.put_object(Bucket="sdk-attributes", Key="multi", Body=b"new-head")
        historical = sdk.get_object_attributes(
            Bucket="sdk-attributes", Key="multi", VersionId=old_version,
            ObjectAttributes=["ObjectParts"], MaxParts=1, PartNumberMarker=1,
        )
        assert historical["VersionId"] == old_version
        assert historical["ObjectParts"]["Parts"][0]["PartNumber"] == 2
        assert service[2].downloads == before
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


def test_object_parts_pagination_validation_and_unsigned_injection(service):
    assert signed(service, "PUT", "/attrs-bucket").status_code == 200
    assert signed(service, "PUT", "/attrs-bucket/key", b"contents").status_code == 200
    downloads = service[2].downloads
    for header, value, fields in (
        ("x-amz-max-parts", "0", "ObjectParts"),
        ("x-amz-max-parts", "1001", "ObjectParts"),
        ("x-amz-max-parts", "not-an-int", "ObjectParts"),
        ("x-amz-max-parts", "1.0", "ObjectParts"),
        ("x-amz-part-number-marker", "-1", "ObjectParts"),
        ("x-amz-part-number-marker", "10001", "ObjectParts"),
        ("x-amz-part-number-marker", "nope", "ObjectParts"),
        ("x-amz-max-parts", "1", "ETag"),
        ("x-amz-part-number-marker", "1", "ETag"),
    ):
        response = signed(
            service, "GET", "/attrs-bucket/key?attributes",
            extra_headers={"x-amz-object-attributes": fields, header: value},
        )
        assert response.status_code == 400, (header, value, response.text)
        assert b"InvalidArgument" in response.content

    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    request = AWSRequest(
        method="GET", url="http://localhost:9000/attrs-bucket/key?attributes",
        headers={"x-amz-object-attributes": "ObjectParts"},
    )
    S3SigV4Auth(
        Credentials(service[1].access_key, service[1].secret_key),
        "s3", "us-east-1",
    ).add_auth(request)
    headers = dict(request.headers)
    headers["x-amz-max-parts"] = "1"
    injected = service[0].get("/attrs-bucket/key?attributes", headers=headers)
    assert injected.status_code == 403
    assert b"AccessDenied" in injected.content

    for attr in ("x-amz-max-parts", "x-amz-part-number-marker"):
        unrelated = signed(
            service, "GET", "/attrs-bucket/key",
            extra_headers={attr: "1"},
        )
        assert unrelated.status_code == 501
    assert service[2].downloads == downloads


def test_signed_sparse_multipart_historical_copy_and_legacy_fallback(service):
    from sqlalchemy import delete, select, update
    from teledrive.s3.multipart import multipart_object_parts
    from test_s3_multipart import begin, complete, part

    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    first = b"a" * (5 * 1024 * 1024)
    tail = b"the-last-part"
    uid = begin(service)
    p1 = part(service, uid, 1, first)
    part(service, uid, 2, b"not-committed")
    p3 = part(service, uid, 3, tail)
    done = complete(service, uid, [(1, p1), (3, p3)])
    assert done.status_code == 200, done.text
    version = signed(service, "HEAD", "/backup-bucket/asset.bin").headers[
        "x-amz-version-id"
    ]
    before = service[2].downloads

    def fetch(marker=0, limit=1, *, version_id=version, bucket="backup-bucket",
              key="asset.bin"):
        reply = signed(
            service, "GET",
            f"/{bucket}/{key}?attributes&versionId={version_id}",
            extra_headers={
                "x-amz-object-attributes": "ObjectParts,Checksum",
                "x-amz-max-parts": str(limit),
                "x-amz-part-number-marker": str(marker),
            },
        )
        assert reply.status_code == 200, reply.text
        root = ET.fromstring(reply.content)
        ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
        parts = root.find(ns + "ObjectParts")
        assert parts is not None
        return parts, ns

    first_page, ns = fetch()
    assert first_page.findtext(ns + "PartsCount") == "2"
    assert first_page.findtext(ns + "NextPartNumberMarker") == "1"
    assert first_page.findtext(ns + "IsTruncated") == "true"
    assert first_page.findtext(ns + "Part/" + ns + "PartNumber") == "1"
    second_page, ns = fetch(1)
    assert second_page.findtext(ns + "Part/" + ns + "PartNumber") == "3"
    assert second_page.findtext(ns + "Part/" + ns + "Size") == str(len(tail))
    assert second_page.findtext(ns + "IsTruncated") == "false"
    assert second_page.findtext(ns + "Part/" + ns + "ChecksumSHA256") == (
        base64.b64encode(hashlib.sha256(tail).digest()).decode("ascii")
    )
    empty_page, _ = fetch(3)
    assert empty_page.find(ns + "Part") is None

    # Direct SQL snapshots are durable before any HEAD overwrite; these
    # metadata calls never download object bytes from Telegram.
    assert service[2].downloads == before
    assert signed(service, "PUT", "/backup-bucket/asset.bin", b"new-head").status_code == 200
    historical, _ = fetch(1)
    assert historical.findtext(ns + "Part/" + ns + "PartNumber") == "3"
    copied = signed(
        service, "PUT", "/backup-bucket/clone.bin",
        extra_headers={"x-amz-copy-source":
                       f"/backup-bucket/asset.bin?versionId={version}"},
    )
    assert copied.status_code == 200, copied.text
    copied_id = signed(service, "HEAD", "/backup-bucket/clone.bin").headers[
        "x-amz-version-id"
    ]
    cloned, _ = fetch(1, version_id=copied_id, key="clone.bin")
    assert cloned.findtext(ns + "Part/" + ns + "PartNumber") == "3"

    # A pre-upgrade object has no new snapshot table. The old committed
    # multipart journal is an independently verified read-only fallback.
    db = service[3]()
    try:
        db.connection.execute(delete(multipart_object_parts).where(
            multipart_object_parts.c.object_id == version
        ))
        db.commit()
    finally:
        db.close()
    from_legacy, _ = fetch(1)
    assert from_legacy.findtext(ns + "Part/" + ns + "PartNumber") == "3"

    # Any partial or tampered snapshot must fail closed, rather than silently
    # falling back to a plausible but inconsistent historical multipart list.
    db = service[3]()
    try:
        db.connection.execute(update(multipart_object_parts).where(
            multipart_object_parts.c.object_id == copied_id,
            multipart_object_parts.c.number == 3,
        ).values(size=1))
        db.commit()
    finally:
        db.close()
    corrupt = signed(
        service, "GET",
        f"/backup-bucket/clone.bin?attributes&versionId={copied_id}",
        extra_headers={"x-amz-object-attributes": "ObjectParts"},
    )
    assert corrupt.status_code == 409
    assert b"InvalidObjectState" in corrupt.content
    assert service[2].downloads == before

