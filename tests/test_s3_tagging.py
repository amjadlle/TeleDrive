"""S3 object tagging: signed real HTTP, immutable version metadata and boto3.

No Telegram payload is reuploaded on tag edits/copies. Exercise actual SQL
transaction boundaries, XML parsing and permission-scoped credentials.
"""
from __future__ import annotations

import base64
import hashlib
import socket
import threading
import time
import xml.etree.ElementTree as ET

import boto3
from botocore.config import Config
from teledrive.s3.credentials import S3CredentialStore
from teledrive.s3.tagging import ObjectTagStore, validate_tags, TaggingError
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore
from test_s3_gateway import service, signed
from test_s3_copy import copy

_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def tag_xml(entries):
    root = ET.Element("Tagging")
    set_el = ET.SubElement(root, "TagSet")
    for key, value in entries:
        item = ET.SubElement(set_el, "Tag")
        ET.SubElement(item, "Key").text = key
        ET.SubElement(item, "Value").text = value
    return ET.tostring(root, encoding="utf-8")


def extract_tags(response):
    assert response.status_code == 200, response.text
    root = ET.fromstring(response.content)
    return [(tag.findtext(_NS + "Key"), tag.findtext(_NS + "Value"))
            for tag in root.findall(_NS + "TagSet/" + _NS + "Tag")]


def create(service, *, key="file.bin", data=b"Telegram-backed-body"):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/" + key, data).status_code == 200


def test_tags_roundtrip_no_telegram_download_or_upload(service):
    create(service)
    remote = service[2]
    uploads, downloads = remote.uploads, remote.downloads
    url = "/backup-bucket/file.bin?tagging"
    before = signed(service, "GET", url)
    assert extract_tags(before) == []
    changed = signed(service, "PUT", url, tag_xml([("project", "TeleDrive"), ("şehir", "İstanbul")]))
    assert changed.status_code == 200, changed.text
    assert extract_tags(signed(service, "GET", url)) == [("project", "TeleDrive"), ("şehir", "İstanbul")]
    assert remote.uploads == uploads and remote.downloads == downloads
    cleared = signed(service, "DELETE", url)
    assert cleared.status_code == 204
    assert extract_tags(signed(service, "GET", url)) == []
    assert remote.uploads == uploads and remote.downloads == downloads


def test_tags_copy_snapshot_isolated_from_source_and_overwrites(service):
    create(service, data=b"v1")
    url = "/backup-bucket/file.bin?tagging"
    assert signed(service, "PUT", url, tag_xml([("stage", "original")])).status_code == 200
    copied = copy(service, "/backup-bucket/file.bin", "/backup-bucket/duplicate")
    assert copied.status_code == 200, copied.text
    dest = "/backup-bucket/duplicate?tagging"
    assert extract_tags(signed(service, "GET", dest)) == [("stage", "original")]
    assert signed(service, "PUT", url, tag_xml([("stage", "modified")])).status_code == 200
    assert extract_tags(signed(service, "GET", dest)) == [("stage", "original")]
    old_uploads = service[2].uploads
    assert signed(service, "PUT", "/backup-bucket/file.bin", b"v2").status_code == 200
    assert service[2].uploads == old_uploads + 1
    assert extract_tags(signed(service, "GET", url)) == []
    assert extract_tags(signed(service, "GET", dest)) == [("stage", "original")]
    # Previous immutable version metadata remains available internally.
    db = service[3]()
    try:
        catalog = BucketCatalog(ManifestStore(db))
        old = db.connection.execute(
            __import__("sqlalchemy").select(
                __import__("teledrive.storage.manifest", fromlist=["objects"]).objects.c.id
            ).where(
                __import__("teledrive.storage.manifest", fromlist=["objects"]).objects.c.object_key == "file.bin"
            ).order_by(
                __import__("teledrive.storage.manifest", fromlist=["objects"]).objects.c.created_at
            )
        ).scalars().all()
        assert len(old) == 2
        tagger = ObjectTagStore(catalog)
        assert tagger.get(old[0]) == [("stage", "modified")]
        assert tagger.get(old[1]) == []
    finally:
        db.close()


def test_tagging_scopes_do_not_leak_through_generic_object_permissions(service):
    create(service)
    db = service[3]()
    try:
        credentials = S3CredentialStore(db, service[4])
        reader = credentials.create("read-only-data", ["s3:GetObject"])
        tag_reader = credentials.create("read-tag", ["s3:GetObjectTagging"])
        tag_writer = credentials.create("write-tag", ["s3:PutObjectTagging"])
    finally:
        db.close()
    path = "/backup-bucket/file.bin?tagging"
    denied = signed(service, "GET", path,
                    access=reader.access_key, secret=reader.secret_key)
    assert denied.status_code == 403
    allowed = signed(service, "GET", path,
                     access=tag_reader.access_key, secret=tag_reader.secret_key)
    assert extract_tags(allowed) == []
    denied_put = signed(service, "PUT", path, tag_xml([("k", "v")]),
                        access=tag_reader.access_key, secret=tag_reader.secret_key)
    assert denied_put.status_code == 403
    allowed_put = signed(service, "PUT", path, tag_xml([("k", "v")]),
                         access=tag_writer.access_key, secret=tag_writer.secret_key)
    assert allowed_put.status_code == 200
    denied_delete = signed(service, "DELETE", path,
                           access=tag_writer.access_key, secret=tag_writer.secret_key)
    assert denied_delete.status_code == 403
    assert extract_tags(signed(service, "GET", path)) == [("k", "v")]


def test_wrong_digest_and_malformed_xml_never_mutate_tags(service):
    create(service)
    path = "/backup-bucket/file.bin?tagging"
    assert signed(service, "PUT", path, tag_xml([("safe", "old")])).status_code == 200
    malformed = signed(service, "PUT", path, b"<Tagging><TagSet><Tag>")
    assert malformed.status_code == 400
    assert b"MalformedXML" in malformed.content
    attack = (b'<!DOCTYPE x [<!ENTITY name "BOMB">]>'
              b'<Tagging><TagSet><Tag><Key>&name;</Key><Value>x</Value></Tag></TagSet></Tagging>')
    hostile = signed(service, "PUT", path, attack)
    assert hostile.status_code == 400
    assert extract_tags(signed(service, "GET", path)) == [("safe", "old")]
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
    original = tag_xml([("safe", "new")])
    req = AWSRequest(
        method="PUT", url="http://localhost:9000" + path,
        data=original, headers={"x-amz-content-sha256": hashlib.sha256(original).hexdigest()}
    )
    req.context["payload_signing_enabled"] = True
    S3SigV4Auth(Credentials(service[1].access_key, service[1].secret_key),
                "s3", "us-east-1").add_auth(req)
    changed_body = service[0].put(path, headers=dict(req.headers), content=b"forged body")
    assert changed_body.status_code in (400, 403)
    assert extract_tags(signed(service, "GET", path)) == [("safe", "old")]


def test_limits_duplicate_keys_and_unsupported_version_are_rejected(service):
    create(service)
    path = "/backup-bucket/file.bin?tagging"
    invalid_sets = [
        [("a", "b")] * 2, [(f"k{i}", "v") for i in range(11)],
        [("aws:secret", "value")], [("x"*129, "v")],
        [("key", "v"*257)], [("", "invalid")]
    ]
    for entries in invalid_sets:
        r = signed(service, "PUT", path, tag_xml(entries))
        assert r.status_code == 400, (entries, r.text)
        assert b"InvalidTag" in r.content
    assert signed(service, "GET", "/backup-bucket/file.bin?tagging&versionId=old").status_code == 501
    assert signed(service, "GET", "/backup-bucket/file.bin?versionId=old").status_code == 501
    assert signed(service, "PUT", "/backup-bucket/file.bin", b"new",
                  extra_headers={"x-amz-tagging": "k=v"}).status_code == 501
    assert extract_tags(signed(service, "GET", path)) == []


def test_boto3_tagging_over_actual_http_server(service):
    import uvicorn
    import web.s3 as gateway

    client, issued, remote, _, _ = service
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/SDK", b"original").status_code == 200
    socket_obj = socket.socket()
    socket_obj.bind(("127.0.0.1", 0))
    port = socket_obj.getsockname()[1]
    socket_obj.listen(16)
    server = uvicorn.Server(uvicorn.Config(
        gateway.app, host="127.0.0.1", port=port, lifespan="off", log_level="error"
    ))
    worker = threading.Thread(target=server.run, kwargs={"sockets":[socket_obj]}, daemon=True)
    worker.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        sdk = boto3.client("s3", endpoint_url=f"http://127.0.0.1:{port}",
            aws_access_key_id=issued.access_key, aws_secret_access_key=issued.secret_key,
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style":"path", "payload_signing_enabled":True},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"max_attempts":0},
            )
        )
        before = remote.uploads
        put = sdk.put_object_tagging(Bucket="backup-bucket", Key="SDK",
            Tagging={"TagSet":[{"Key":"a","Value":"1"},{"Key":"b","Value":"2"}]})
        assert put["ResponseMetadata"]["HTTPStatusCode"] == 200
        assert sdk.get_object_tagging(Bucket="backup-bucket", Key="SDK")["TagSet"] == [
            {"Key":"a", "Value":"1"},{"Key":"b", "Value":"2"}
        ]
        sdk.delete_object_tagging(Bucket="backup-bucket", Key="SDK")
        assert sdk.get_object_tagging(Bucket="backup-bucket", Key="SDK")["TagSet"] == []
        assert remote.uploads == before
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        socket_obj.close()
        assert not worker.is_alive()
