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
from teledrive.storage.manifest import ManifestStore, objects
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
        from sqlalchemy import select
        recorded = db.connection.execute(
            select(objects.c.id, objects.c.sha256).where(
                objects.c.object_key == "file.bin"
            )
        ).all()
        assert len(recorded) == 2
        by_digest = {row.sha256: row.id for row in recorded}
        old_version_id = by_digest[hashlib.sha256(b"v1").hexdigest()]
        current_version_id = by_digest[hashlib.sha256(b"v2").hexdigest()]
        assert catalog.get("backup-bucket", "file.bin").item.id == current_version_id
        tagger = ObjectTagStore(catalog)
        assert tagger.get(old_version_id) == [("stage", "modified")]
        assert tagger.get(current_version_id) == []
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
    assert signed(service, "GET", "/backup-bucket/file.bin?tagging&versionId=old").status_code == 404
    assert signed(service, "GET", "/backup-bucket/file.bin?versionId=old").status_code == 404
    assert signed(service, "PUT", "/backup-bucket/file.bin", b"new",
                  extra_headers={"x-amz-tagging": "k=v"}).status_code == 200
    assert extract_tags(signed(service, "GET", path)) == [("k", "v")]


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
        sdk.put_bucket_tagging(Bucket="backup-bucket", Tagging={
            "TagSet":[{"Key":"environment","Value":"ci"}]
        })
        assert sdk.get_bucket_tagging(Bucket="backup-bucket")["TagSet"] == [
            {"Key":"environment","Value":"ci"}
        ]
        sdk.delete_bucket_tagging(Bucket="backup-bucket")
        inserted = sdk.put_object(Bucket="backup-bucket", Key="sdk-inline",
                                  Body=b"sdk-inline-bytes", Tagging="source=sdk")
        assert inserted["ResponseMetadata"]["HTTPStatusCode"] == 200
        assert sdk.get_object_tagging(Bucket="backup-bucket", Key="sdk-inline")["TagSet"] == [
            {"Key":"source","Value":"sdk"}
        ]
        uploaded = remote.uploads
        sdk.copy_object(Bucket="backup-bucket", Key="sdk-inline-copy",
                        CopySource={"Bucket":"backup-bucket", "Key":"sdk-inline"},
                        TaggingDirective="REPLACE", Tagging="source=copy")
        assert sdk.get_object_tagging(Bucket="backup-bucket", Key="sdk-inline-copy")["TagSet"] == [
            {"Key":"source","Value":"copy"}
        ]
        assert remote.uploads == uploaded == before + 1
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        socket_obj.close()
        assert not worker.is_alive()


def test_bucket_tagging_correct_s3_missing_semantics_and_safe_removal(service):
    remote = service[2]
    assert signed(service, "PUT", "/bucket-with-tags").status_code == 200
    url = "/bucket-with-tags?tagging"
    initial = signed(service, "GET", url)
    assert initial.status_code == 404
    assert b"NoSuchTagSet" in initial.content
    before = (remote.uploads, remote.downloads)
    set_tags = signed(service, "PUT", url, tag_xml([("cost", "ops"), ("zone", "eu")]))
    assert set_tags.status_code == 200, set_tags.text
    assert extract_tags(signed(service, "GET", url)) == [("cost", "ops"), ("zone", "eu")]
    assert before == (remote.uploads, remote.downloads)
    erased = signed(service, "DELETE", url)
    assert erased.status_code == 204
    assert signed(service, "GET", url).status_code == 404
    # Removing a physically empty bucket must not leave a FK-bound tag row.
    assert signed(service, "PUT", url, tag_xml([("role","temporary")])).status_code == 200
    assert signed(service, "DELETE", "/bucket-with-tags").status_code == 204
    assert signed(service, "GET", url).status_code == 404


def test_bucket_tagging_scopes_and_checksum_fail_closed(service):
    assert signed(service, "PUT", "/bucket-with-tags").status_code == 200
    db = service[3]()
    try:
        keys = S3CredentialStore(db, service[4])
        data_reader = keys.create("object-read", ["s3:GetObject"])
        tag_reader = keys.create("bucket-tag-read", ["s3:GetBucketTagging"])
        tag_writer = keys.create("bucket-tag-write", ["s3:PutBucketTagging"])
    finally:
        db.close()
    path = "/bucket-with-tags?tagging"
    denied = signed(service, "GET", path, access=data_reader.access_key,
                    secret=data_reader.secret_key)
    assert denied.status_code == 403
    allowed = signed(service, "PUT", path, tag_xml([("account", "1")]),
                     access=tag_writer.access_key, secret=tag_writer.secret_key)
    assert allowed.status_code == 200
    assert extract_tags(signed(service, "GET", path, access=tag_reader.access_key,
                               secret=tag_reader.secret_key)) == [("account","1")]
    assert signed(service, "DELETE", path, access=tag_writer.access_key,
                  secret=tag_writer.secret_key).status_code == 403
    assert signed(service, "PUT", path, b"<Tagging><broken/>").status_code == 400
    assert extract_tags(signed(service, "GET", path)) == [("account","1")]


def test_bucket_tagging_is_isolated_from_object_tags(service):
    create(service)
    path = "/backup-bucket/file.bin?tagging"
    assert signed(service, "PUT", path, tag_xml([("object", "only")])).status_code == 200
    assert signed(service, "PUT", "/backup-bucket?tagging",
                  tag_xml([("bucket", "only")])).status_code == 200
    assert extract_tags(signed(service, "GET", path)) == [("object", "only")]
    assert extract_tags(signed(service, "GET", "/backup-bucket?tagging")) == [("bucket", "only")]
    assert signed(service, "DELETE", "/backup-bucket?tagging").status_code == 204
    assert extract_tags(signed(service, "GET", path)) == [("object", "only")]


def test_signed_inline_tags_are_published_with_new_telegram_object(service):
    create(service, data=b"old-blob")
    remote = service[2]
    response = signed(service, "PUT", "/backup-bucket/file.bin", b"new-blob",
                      extra_headers={"x-amz-tagging": "role=storage&city=%C4%B0stanbul"})
    assert response.status_code == 200, response.text
    assert extract_tags(signed(service, "GET", "/backup-bucket/file.bin?tagging")) == [
        ("city", "İstanbul"), ("role", "storage")
    ]
    assert signed(service, "GET", "/backup-bucket/file.bin").content == b"new-blob"
    assert remote.uploads == 2


def test_invalid_inline_tag_or_missing_tagging_permission_never_uploads(service):
    create(service)
    remote = service[2]
    before = remote.uploads
    denied = signed(service, "PUT", "/backup-bucket/file.bin", b"new-blob",
                    extra_headers={"x-amz-tagging": "k=first&k=duplicate"})
    assert denied.status_code == 400
    assert remote.uploads == before
    db = service[3]()
    try:
        limited = S3CredentialStore(db, service[4]).create(
            "put-no-tag-rights", ["s3:PutObject"]
        )
    finally:
        db.close()
    forbidden = signed(service, "PUT", "/backup-bucket/file.bin", b"new-blob",
                       extra_headers={"x-amz-tagging": "status=private"},
                       access=limited.access_key, secret=limited.secret_key)
    assert forbidden.status_code == 403
    assert remote.uploads == before
    assert signed(service, "GET", "/backup-bucket/file.bin").content == b"Telegram-backed-body"


def test_copy_tag_directive_replace_snapshots_new_tags_without_telegram_upload(service):
    create(service, data=b"source-body")
    assert signed(service, "PUT", "/backup-bucket/file.bin?tagging",
                  tag_xml([("original", "source")])).status_code == 200
    before = service[2].uploads
    replace = copy(service, "/backup-bucket/file.bin", "/backup-bucket/replaced",
                   **{"x-amz-tagging-directive": "REPLACE",
                      "x-amz-tagging": "destination=copy"})
    assert replace.status_code == 200, replace.text
    assert service[2].uploads == before
    assert extract_tags(signed(service, "GET", "/backup-bucket/replaced?tagging")) == [
        ("destination", "copy")
    ]
    assert extract_tags(signed(service, "GET", "/backup-bucket/file.bin?tagging")) == [
        ("original", "source")
    ]
    bad = copy(service, "/backup-bucket/file.bin", "/backup-bucket/should-not-exist",
               **{"x-amz-tagging": "not-replace=forbidden"})
    assert bad.status_code == 400
    assert signed(service, "HEAD", "/backup-bucket/should-not-exist").status_code == 404


def test_copy_tagging_cannot_bypass_separate_read_write_scopes(service):
    create(service)
    source = "/backup-bucket/file.bin"
    assert signed(service, "PUT", source + "?tagging",
                  tag_xml([("confidential", "label")])).status_code == 200
    db = service[3]()
    try:
        limited = S3CredentialStore(db, service[4]).create(
            "basic-copy-only", ["s3:GetObject", "s3:PutObject"]
        )
        replace_only = S3CredentialStore(db, service[4]).create(
            "can-change-tags", ["s3:GetObject", "s3:PutObject", "s3:PutObjectTagging"]
        )
    finally:
        db.close()
    # A client with ordinary CopyObject permissions cannot exfiltrate
    # protected source tags by copying the object to its own key.
    denied = signed(service, "PUT", "/backup-bucket/no-leak",
                    extra_headers={"x-amz-copy-source": source},
                    access=limited.access_key, secret=limited.secret_key)
    assert denied.status_code == 403
    assert signed(service, "HEAD", "/backup-bucket/no-leak").status_code == 404
    # Explicit REPLACE may deliberately strip tags, but only with permission.
    replacement = signed(service, "PUT", "/backup-bucket/unlabeled",
                         extra_headers={"x-amz-copy-source": source,
                                        "x-amz-tagging-directive": "REPLACE"},
                         access=replace_only.access_key, secret=replace_only.secret_key)
    assert replacement.status_code == 200, replacement.text
    assert extract_tags(signed(service, "GET", "/backup-bucket/unlabeled?tagging")) == []


def test_bucket_tagging_full_50_tag_boundary_utf16_and_large_xml(service):
    """50 high-entropy UTF-16-valid tags exceed prior 10-tag/16 KiB limits."""
    assert signed(service, "PUT", "/tag-boundary-ci").status_code == 200
    url = "/tag-boundary-ci?tagging"
    tags = [(f"key-{n:02d}", "é" * 256) for n in range(50)]
    xml = tag_xml(tags)
    assert len(xml) > 16 * 1024
    assert len(xml) < 256 * 1024
    first = signed(service, "PUT", url, xml)
    assert first.status_code == 200, first.text
    expected = sorted(tags)
    assert extract_tags(signed(service, "GET", url)) == expected
    assert signed(service, "PUT", url, xml).status_code == 200
    assert extract_tags(signed(service, "GET", url)) == expected
    # The object path retains the smaller AWS 10-tag maximum.
    assert signed(service, "PUT", "/tag-boundary-ci/data", b"unchanged").status_code == 200
    invalid_object = signed(service, "PUT", "/tag-boundary-ci/data?tagging",
                            tag_xml(tags[:11]))
    assert invalid_object.status_code == 400
    assert extract_tags(signed(service, "GET", url)) == expected
    assert service[2].uploads == 1


def test_bucket_tagging_rejects_51_tags_and_duplicate_fields_atomically(service):
    assert signed(service, "PUT", "/tag-boundary-ci").status_code == 200
    url = "/tag-boundary-ci?tagging"
    assert signed(service, "PUT", url, tag_xml([("unchanged", "yes")])).status_code == 200
    attacks = [
        tag_xml([(f"k{n}", str(n)) for n in range(51)]),
        tag_xml([("duplicate", "a"), ("duplicate", "b")]),
        tag_xml([("aws:reserved", "no")]),
        tag_xml([("", "no")]),
        tag_xml([("k", "x" * 257)]),
        tag_xml([("k" * 129, "v")]),
        b'<Tagging xmlns="urn:evil"><TagSet /></Tagging>',
        b'<Tagging><TagSet><Tag><Key>one</Key><Key>two</Key></Tag></TagSet></Tagging>',
        b'<!DOCTYPE doc [<!ENTITY a "one">]><Tagging><TagSet>'
        b'<Tag><Key>&a;</Key><Value>x</Value></Tag></TagSet></Tagging>',
    ]
    for payload in attacks:
        rejected = signed(service, "PUT", url, payload)
        assert rejected.status_code == 400, (payload[:100], rejected.text)
        assert extract_tags(signed(service, "GET", url)) == [("unchanged", "yes")]
    assert service[2].uploads == service[2].downloads == 0


def test_bucket_tagging_valid_crc32_and_corrupt_checksum_do_not_update_tags(service):
    import zlib

    assert signed(service, "PUT", "/tag-checksum-ci").status_code == 200
    url = "/tag-checksum-ci?tagging"
    payload = tag_xml([("checksum", "valid")])
    crc = base64.b64encode((zlib.crc32(payload) & 0xffffffff).to_bytes(4, "big")).decode()
    accepted = signed(service, "PUT", url, payload, extra_headers={
        "x-amz-checksum-crc32": crc, "x-amz-sdk-checksum-algorithm": "CRC32",
    })
    assert accepted.status_code == 200, accepted.text
    rejected = signed(service, "PUT", url, tag_xml([("checksum", "invalid")]),
                      extra_headers={"x-amz-checksum-crc32": crc,
                                     "x-amz-sdk-checksum-algorithm": "CRC32"})
    assert rejected.status_code == 400, rejected.text
    mismatch_md5 = signed(service, "PUT", url, tag_xml([("checksum", "invalid")]),
                          extra_headers={"Content-MD5": base64.b64encode(
                              b"0" * 16).decode()})
    assert mismatch_md5.status_code == 400
    assert extract_tags(signed(service, "GET", url)) == [("checksum", "valid")]


def test_boto3_full_bucket_tagging_limit_and_replacement_over_http(service):
    import uvicorn
    import web.s3 as gateway
    from botocore.exceptions import ClientError

    _, issued, remote, _, _ = service
    assert signed(service, "PUT", "/tag-boundary-ci").status_code == 200
    socket_obj = socket.socket()
    socket_obj.bind(("127.0.0.1", 0))
    socket_obj.listen(16)
    port = socket_obj.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        gateway.app, host="127.0.0.1", port=port,
        lifespan="off", log_level="error",
    ))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [socket_obj]},
                              daemon=True)
    worker.start()
    try:
        for _ in range(150):
            if server.started:
                break
            time.sleep(0.04)
        assert server.started
        sdk = boto3.client(
            "s3", endpoint_url=f"http://127.0.0.1:{port}",
            aws_access_key_id=issued.access_key, aws_secret_access_key=issued.secret_key,
            region_name="us-east-1",
            config=Config(signature_version="s3v4",
                          s3={"addressing_style": "path"},
                          retries={"max_attempts": 0}),
        )
        pairs = [{"Key": f"tag-{n:02d}", "Value": "v" * 256} for n in range(50)]
        answer = sdk.put_bucket_tagging(
            Bucket="tag-boundary-ci", Tagging={"TagSet": pairs},
        )
        assert answer["ResponseMetadata"]["HTTPStatusCode"] == 200
        assert sdk.get_bucket_tagging(Bucket="tag-boundary-ci")["TagSet"] == pairs
        with pytest.raises(ClientError):
            sdk.put_bucket_tagging(
                Bucket="tag-boundary-ci",
                Tagging={"TagSet": pairs + [{"Key": "too-many", "Value": "v"}]},
            )
        assert sdk.get_bucket_tagging(Bucket="tag-boundary-ci")["TagSet"] == pairs
        assert sdk.put_bucket_tagging(
            Bucket="tag-boundary-ci",
            Tagging={"TagSet": [{"Key": "replaced", "Value": "yes"}]},
        )["ResponseMetadata"]["HTTPStatusCode"] == 200
        assert sdk.get_bucket_tagging(Bucket="tag-boundary-ci")["TagSet"] == [
            {"Key": "replaced", "Value": "yes"}
        ]
        assert remote.uploads == remote.downloads == 0
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        socket_obj.close()
        assert not worker.is_alive()
