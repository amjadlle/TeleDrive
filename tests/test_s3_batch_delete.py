"""S3 Multi-Object Delete: real signed HTTP, XML, scopes, and Telegram retention.

DeleteObjects updates ONLY the current object heads. Its underlying Telegram
messages remain intact for reference-audited GC/version retention.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import threading
import time
import xml.etree.ElementTree as ET

import boto3
import pytest
import uvicorn
from botocore.config import Config

from test_s3_gateway import service, signed
from teledrive.s3.credentials import S3CredentialStore
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore
import web.s3 as gateway

NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def delete_xml(keys, *, quiet=False, version=None):
    root = ET.Element("Delete")
    if quiet:
        ET.SubElement(root, "Quiet").text = "true"
    for key in keys:
        item = ET.SubElement(root, "Object")
        ET.SubElement(item, "Key").text = key
        if version is not None:
            ET.SubElement(item, "VersionId").text = version
    return ET.tostring(root, encoding="utf-8")


def batch(svc, body, *, md5=True, crc32=None, access=None, secret=None):
    headers = {}
    if md5:
        headers["Content-MD5"] = base64.b64encode(
            hashlib.md5(body, usedforsecurity=False).digest()
        ).decode("ascii")
    if crc32 is not None:
        headers["x-amz-checksum-crc32"] = crc32
    return signed(svc, "POST", "/backup-bucket?delete", body,
                  extra_headers=headers, access=access, secret=secret)


def make_bucket(svc):
    assert signed(svc, "PUT", "/backup-bucket").status_code == 200


def make_objects(svc, names):
    for key in names:
        response = signed(svc, "PUT", "/backup-bucket/" + key, key.encode("utf-8"))
        assert response.status_code == 200, response.text


def deleted_keys(result):
    root = ET.fromstring(result.content)
    return [child.findtext(NS+"Key") for child in root.findall(NS+"Deleted")]


def error_code(result):
    return ET.fromstring(result.content).findtext("Code")


def test_batch_delete_only_unlinks_heads_and_retains_remote_parts(service):
    make_bucket(service)
    make_objects(service, ["a.bin", "folder/b.bin", "keep.bin"])
    before_remote = dict(service[2].parts)
    uploads = service[2].uploads
    payload = delete_xml(["folder/b.bin", "missing.bin", "a.bin"])
    result = batch(service, payload)
    assert result.status_code == 200, result.text
    assert deleted_keys(result) == ["folder/b.bin", "missing.bin", "a.bin"]
    assert signed(service, "GET", "/backup-bucket/a.bin").status_code == 404
    assert signed(service, "GET", "/backup-bucket/folder/b.bin").status_code == 404
    kept = signed(service, "GET", "/backup-bucket/keep.bin")
    assert kept.status_code == 200 and kept.content == b"keep.bin"
    assert service[2].parts == before_remote
    assert service[2].uploads == uploads
    # Historical manifests remain reachable by immutable internal object ID.
    db = service[3]()
    try:
        manifests = ManifestStore(db)
        assert len(manifests.interrupted()) == 0
        assert any(body == b"a.bin" for body in before_remote.values())
    finally:
        db.close()
    again = batch(service, payload)
    assert again.status_code == 200
    assert deleted_keys(again) == ["folder/b.bin", "missing.bin", "a.bin"]


def test_batch_delete_quiet_omits_successes(service):
    make_bucket(service)
    make_objects(service, ["first.bin", "other.bin"])
    result = batch(service, delete_xml(["first.bin", "absent.bin"], quiet=True))
    assert result.status_code == 200
    assert ET.fromstring(result.content).findall(NS+"Deleted") == []
    assert signed(service, "GET", "/backup-bucket/first.bin").status_code == 404
    assert signed(service, "GET", "/backup-bucket/other.bin").status_code == 200


def test_corrupted_signed_body_and_bad_md5_rejected_before_any_delete(service):
    make_bucket(service)
    make_objects(service, ["target.bin"])
    legitimate = delete_xml(["target.bin"])
    # Valid signature/sha256 but malformed MD5.
    bad_md5 = signed(service, "POST", "/backup-bucket?delete", legitimate,
                     extra_headers={"Content-MD5": "AAAA"})
    assert bad_md5.status_code == 400
    assert error_code(bad_md5) == "BadDigest"
    missing_checksum = batch(service, legitimate, md5=False)
    assert missing_checksum.status_code == 400
    assert error_code(missing_checksum) == "InvalidRequest"
    assert signed(service, "GET", "/backup-bucket/target.bin").status_code == 200


@pytest.mark.parametrize("payload", [
    b"<!DOCTYPE Delete [<!ENTITY payload SYSTEM 'file:///etc/passwd'>]><Delete><Object><Key>&payload;</Key></Object></Delete>",
    b"<Delete><Object><Key>target.bin</Key></Object><Broken></Delete>",
    delete_xml(["target.bin"], version="unknown-version"),
    delete_xml(["target.bin"] * 1001),
    b"<Delete><Object><Key></Key></Object></Delete>",
])
def test_invalid_batch_is_fully_rejected_before_mutation(service, payload):
    make_bucket(service)
    make_objects(service, ["target.bin"])
    res = batch(service, payload)
    assert res.status_code in (400, 501)
    assert error_code(res) in ("MalformedXML", "NotImplemented", "InvalidObjectName")
    assert signed(service, "GET", "/backup-bucket/target.bin").status_code == 200


def test_batch_denies_key_without_delete_scope_and_missing_bucket(service):
    make_bucket(service)
    make_objects(service, ["target.bin"])
    db = service[3]()
    try:
        reader = S3CredentialStore(db, service[4]).create(
            "list-and-read", ["s3:ListBucket", "s3:GetObject"]
        )
    finally:
        db.close()
    res = batch(service, delete_xml(["target.bin"]),
                access=reader.access_key, secret=reader.secret_key)
    assert res.status_code == 403 and error_code(res) == "AccessDenied"
    assert signed(service, "GET", "/backup-bucket/target.bin").status_code == 200
    missing = signed(service, "POST", "/missing-bucket?delete", delete_xml(["x.bin"]),
                     extra_headers={"Content-MD5": base64.b64encode(
                         hashlib.md5(delete_xml(["x.bin"]), usedforsecurity=False).digest()
                     ).decode("ascii")})
    assert missing.status_code == 404 and error_code(missing) == "NoSuchBucket"


def test_boto3_delete_objects_with_real_http_and_crc32(service):
    _, cred, remote, _, _ = service
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(16)
    server = uvicorn.Server(uvicorn.Config(
        gateway.app, host="127.0.0.1", port=port,
        lifespan="off", log_level="error",
    ))
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
            aws_access_key_id=cred.access_key,
            aws_secret_access_key=cred.secret_key,
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path", "payload_signing_enabled": True},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"max_attempts": 0},
            ),
        )
        client.create_bucket(Bucket="sdk-delete")
        for key in ["one", "two", "retain"]:
            client.put_object(Bucket="sdk-delete", Key=key, Body=key.encode())
        before_parts = dict(remote.parts)
        res = client.delete_objects(
            Bucket="sdk-delete",
            Delete={"Objects": [{"Key": "one"}, {"Key": "two"}, {"Key": "absent"}],
                    "Quiet": False},
        )
        assert sorted(x["Key"] for x in res["Deleted"]) == ["absent", "one", "two"]
        remaining = client.list_objects_v2(Bucket="sdk-delete")
        assert [x["Key"] for x in remaining["Contents"]] == ["retain"]
        assert client.get_object(Bucket="sdk-delete", Key="retain")["Body"].read() == b"retain"
        assert remote.parts == before_parts
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()
