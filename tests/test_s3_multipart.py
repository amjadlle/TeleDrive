"""Signed S3 multipart regression tests, backed by actual Telegram chunk engine.

Use real SQLite, HTTP requests and Bot-API-compatible fake that stores bytes.
The tests deliberately cover crash/retry, visibility, owner isolation and
zero-reupload Complete (not just XML response status).
"""
from __future__ import annotations

import hashlib
import os
from xml.etree import ElementTree as ET
from urllib.parse import quote

import pytest
from test_s3_gateway import service, signed
from teledrive.s3.credentials import S3CredentialStore

NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def xml_text(response, key):
    root = ET.fromstring(response.content)
    # S3 successful responses have the S3 XML namespace; Error XML does not.
    return root.findtext(key if root.tag == "Error" else NS + key)


def begin(svc, key="asset.bin"):
    response = signed(svc, "POST", "/backup-bucket/" + quote(key, safe="/") + "?uploads")
    assert response.status_code == 200, response.text
    return xml_text(response, "UploadId")


def part(svc, upload_id, number, payload, *, key="asset.bin"):
    url = f"/backup-bucket/{key}?partNumber={number}&uploadId={upload_id}"
    response = signed(svc, "PUT", url, payload)
    assert response.status_code == 200, response.text
    return response.headers["etag"]


def complete(svc, upload_id, selection, *, key="asset.bin"):
    root = ET.Element("CompleteMultipartUpload")
    for number, etag in selection:
        node = ET.SubElement(root, "Part")
        ET.SubElement(node, "PartNumber").text = str(number)
        ET.SubElement(node, "ETag").text = etag
    payload = ET.tostring(root)
    return signed(svc, "POST", f"/backup-bucket/{key}?uploadId={upload_id}", payload)


def setup(svc):
    assert signed(svc, "PUT", "/backup-bucket").status_code == 200


def test_multipart_two_part_roundtrip_without_reupload(service):
    setup(service)
    old = signed(service, "PUT", "/backup-bucket/asset.bin", b"previous")
    assert old.status_code == 200
    uid = begin(service)
    first = os.urandom(5 * 1024 * 1024)
    tail = b"remainder" * 23
    first_etag = part(service, uid, 1, first)
    second_etag = part(service, uid, 2, tail)
    bot_uploads = service[2].uploads
    pending = signed(service, "GET", "/backup-bucket/asset.bin")
    assert pending.content == b"previous"  # staging is invisible to readers
    response = signed(service, "GET", f"/backup-bucket/asset.bin?uploadId={uid}")
    assert response.status_code == 200
    root = ET.fromstring(response.content)
    assert [node.findtext(NS+"PartNumber") for node in root.findall(NS+"Part")] == ["1", "2"]
    done = complete(service, uid, [(1, first_etag), (2, second_etag)])
    assert done.status_code == 200, done.text
    expect_etag = hashlib.md5(
        bytes.fromhex(first_etag.strip('"')) + bytes.fromhex(second_etag.strip('"')),
        usedforsecurity=False,
    ).hexdigest() + "-2"
    assert xml_text(done, "ETag") == '"' + expect_etag + '"'
    assert service[2].uploads == bot_uploads, "Complete must never re-upload Telegram payload"
    meta = signed(service, "HEAD", "/backup-bucket/asset.bin")
    assert meta.status_code == 200
    assert meta.headers["content-length"] == str(len(first) + len(tail))
    assert meta.headers["etag"] == '"' + expect_etag + '"'
    downloaded = signed(service, "GET", "/backup-bucket/asset.bin")
    assert downloaded.status_code == 200
    assert downloaded.content == first + tail
    after_restart = signed(service, "GET", "/backup-bucket/asset.bin",
                           extra_headers={"range": "bytes=5242876-5242888"})
    assert after_restart.status_code == 206
    assert after_restart.content == (first + tail)[5242876:5242889]
    listed = signed(service, "GET", "/backup-bucket?list-type=2")
    assert ('"' + expect_etag + '"').encode() in listed.content
    # Repeating Complete after successful transaction is safe and idempotent.
    again = complete(service, uid, [(1, first_etag), (2, second_etag)])
    assert again.status_code == 200 and service[2].uploads == bot_uploads
    mismatched_retry = complete(service, uid, [(2, second_etag)])
    assert mismatched_retry.status_code == 400
    assert xml_text(mismatched_retry, "Code") == "InvalidPart"


def test_multipart_missing_part_and_minimum_size_fail_without_losing_data(service):
    setup(service)
    uid = begin(service)
    tiny = part(service, uid, 1, b"tiny")
    last = part(service, uid, 2, b"tail")
    invalid = complete(service, uid, [(1, tiny), (2, last)])
    assert invalid.status_code == 400
    assert xml_text(invalid, "Code") == "EntityTooSmall"
    assert signed(service, "GET", "/backup-bucket/asset.bin").status_code == 404
    invalid = complete(service, uid, [(2, last), (1, tiny)])
    assert invalid.status_code == 400 and xml_text(invalid, "Code") == "InvalidPartOrder"
    invalid = complete(service, uid, [(1, '"' + "0"*32 + '"')])
    assert invalid.status_code == 400 and xml_text(invalid, "Code") == "InvalidPart"
    valid = complete(service, uid, [(2, last)])
    assert valid.status_code == 200, valid.text
    assert signed(service, "GET", "/backup-bucket/asset.bin").content == b"tail"


def test_abort_leaves_old_object_visible_and_blocks_more_parts(service):
    setup(service)
    assert signed(service, "PUT", "/backup-bucket/asset.bin", b"before").status_code == 200
    uid = begin(service)
    part(service, uid, 1, b"new")
    before = service[2].uploads
    aborted = signed(service, "DELETE", f"/backup-bucket/asset.bin?uploadId={uid}")
    assert aborted.status_code == 204
    assert service[2].uploads == before
    assert signed(service, "GET", "/backup-bucket/asset.bin").content == b"before"
    assert signed(service, "GET", f"/backup-bucket/asset.bin?uploadId={uid}").status_code == 404
    assert signed(service, "PUT", f"/backup-bucket/asset.bin?partNumber=2&uploadId={uid}",
                  b"late").status_code == 404
    assert complete(service, uid, [(1, '"'+hashlib.md5(b"new", usedforsecurity=False).hexdigest()+'"')]).status_code == 404


def test_part_replacement_uses_latest_etag_and_part_upload_owner_isolation(service):
    setup(service)
    uid = begin(service)
    original = part(service, uid, 1, b"old")
    replacement = part(service, uid, 1, b"new")
    assert original != replacement
    wrong = complete(service, uid, [(1, original)])
    assert wrong.status_code == 400 and xml_text(wrong, "Code") == "InvalidPart"
    db = service[3]()
    try:
        second = S3CredentialStore(db, service[4]).create(
            "unrelated-integration", ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
        )
    finally:
        db.close()
    unauth = signed(service, "GET", f"/backup-bucket/asset.bin?uploadId={uid}",
                    access=second.access_key, secret=second.secret_key)
    assert unauth.status_code == 404 and xml_text(unauth, "Code") == "NoSuchUpload"
    unauthorized_complete = signed(
        service, "DELETE", f"/backup-bucket/asset.bin?uploadId={uid}",
        access=second.access_key, secret=second.secret_key
    )
    assert unauthorized_complete.status_code == 404
    finished = complete(service, uid, [(1, replacement)])
    assert finished.status_code == 200
    assert signed(service, "GET", "/backup-bucket/asset.bin").content == b"new"


def test_invalid_part_checksum_never_calls_telegram(service):
    setup(service)
    uid = begin(service)
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
    body = b"checksum-good"
    url = f"/backup-bucket/asset.bin?partNumber=1&uploadId={uid}"
    req = AWSRequest(method="PUT", url="http://localhost:9000"+url,
                     data=body, headers={"x-amz-content-sha256": hashlib.sha256(body).hexdigest()})
    req.context["payload_signing_enabled"] = True
    S3SigV4Auth(Credentials(service[1].access_key, service[1].secret_key),
                "s3", "us-east-1").add_auth(req)
    before = service[2].uploads
    tampered = service[0].put(url, content=b"checksum-bad", headers=dict(req.headers))
    assert tampered.status_code == 400
    assert xml_text(tampered, "Code") == "XAmzContentSHA256Mismatch"
    assert service[2].uploads == before


def test_list_parts_cursor_is_bounded_and_ordered(service):
    setup(service)
    uid = begin(service)
    part(service, uid, 2, b"b")
    part(service, uid, 1, b"a")
    result = signed(service, "GET", f"/backup-bucket/asset.bin?uploadId={uid}&max-parts=1")
    assert result.status_code == 200, result.text
    assert xml_text(result, "IsTruncated") == "true"
    assert xml_text(result, "NextPartNumberMarker") == "1"
    second = signed(service, "GET", f"/backup-bucket/asset.bin?uploadId={uid}&max-parts=1&part-number-marker=1")
    assert second.status_code == 200
    assert [p.findtext(NS+"PartNumber") for p in ET.fromstring(second.content).findall(NS+"Part")] == ["2"]


def test_rejects_entity_expansion_in_s3_completion(service):
    setup(service)
    uid = begin(service)
    etag = part(service, uid, 1, b"legitimate")
    xml = (
        b'<!DOCTYPE CompleteMultipartUpload [<!ENTITY x "bad">]>'
        b'<CompleteMultipartUpload><Part><PartNumber>1</PartNumber>'
        b'<ETag>&x;</ETag></Part></CompleteMultipartUpload>'
    )
    attempts_before = service[2].downloads
    denied = signed(service, "POST", f"/backup-bucket/asset.bin?uploadId={uid}", xml)
    assert denied.status_code == 400
    assert xml_text(denied, "Code") == "MalformedXML"
    assert service[2].downloads == attempts_before
    # No state change: still completable with the valid part.
    assert complete(service, uid, [(1, etag)]).status_code == 200


def test_boto3_multipart_real_http_transport(service):
    """The real boto3 API must parse S3 multipart XML and recover exact bytes."""
    import socket
    import threading
    import time
    import boto3
    import uvicorn
    from botocore.config import Config
    import web.s3 as gateway

    _, credential, remote, _, _ = service
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
            time.sleep(0.05)
        assert server.started
        client = boto3.client("s3",
            endpoint_url=f"http://127.0.0.1:{port}",
            aws_access_key_id=credential.access_key,
            aws_secret_access_key=credential.secret_key,
            region_name="us-east-1",
            config=Config(signature_version="s3v4",
                s3={"addressing_style": "path", "payload_signing_enabled": True},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"max_attempts": 0}),
        )
        client.create_bucket(Bucket="sdk-multipart")
        created = client.create_multipart_upload(Bucket="sdk-multipart", Key="large.bin")
        uid = created["UploadId"]
        payload1 = os.urandom(5*1024*1024)
        payload2 = b"last-part" * 61
        uploaded1 = client.upload_part(
            Bucket="sdk-multipart", Key="large.bin", UploadId=uid,
            PartNumber=1, Body=payload1,
        )
        uploaded2 = client.upload_part(
            Bucket="sdk-multipart", Key="large.bin", UploadId=uid,
            PartNumber=2, Body=payload2,
        )
        first_page = client.list_parts(
            Bucket="sdk-multipart", Key="large.bin", UploadId=uid, MaxParts=1,
        )
        assert first_page["IsTruncated"] is True
        assert first_page["NextPartNumberMarker"] == 1
        assert first_page["StorageClass"] == "STANDARD"
        assert first_page["Parts"][0]["LastModified"]
        next_page = client.list_parts(
            Bucket="sdk-multipart", Key="large.bin", UploadId=uid,
            MaxParts=1, PartNumberMarker=1,
        )
        assert [x["PartNumber"] for x in next_page["Parts"]] == [2]
        assert next_page["IsTruncated"] is False
        pending = client.list_multipart_uploads(
            Bucket="sdk-multipart", Prefix="large", Delimiter="/",
            EncodingType="url", MaxUploads=1,
        )
        assert pending["Prefix"] == "large"
        assert pending["Delimiter"] == "/"
        assert pending["EncodingType"] == "url"
        assert pending["Uploads"][0]["Key"] == "large.bin"
        assert pending["Uploads"][0]["StorageClass"] == "STANDARD"
        before = remote.uploads
        done = client.complete_multipart_upload(
            Bucket="sdk-multipart", Key="large.bin", UploadId=uid,
            MultipartUpload={"Parts": [
                {"PartNumber": 1, "ETag": uploaded1["ETag"]},
                {"PartNumber": 2, "ETag": uploaded2["ETag"]},
            ]},
        )
        assert done["ETag"].endswith('-2"')
        assert remote.uploads == before
        response = client.get_object(Bucket="sdk-multipart", Key="large.bin")
        assert response["Body"].read() == payload1 + payload2
        assert client.head_object(Bucket="sdk-multipart", Key="large.bin")["ETag"] == done["ETag"]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def test_corrupted_telegram_part_prevents_multipart_publication(service):
    setup(service)
    original = b"untouched-data"
    assert signed(service, "PUT", "/backup-bucket/asset.bin", original).status_code == 200
    uid = begin(service)
    etag = part(service, uid, 1, b"remote-corruption-test")
    bot = service[2]
    latest_file_id = f"file-{bot.uploads}"
    assert latest_file_id in bot.parts
    bot.parts[latest_file_id] = b"tampered-body"
    completion = complete(service, uid, [(1, etag)])
    assert completion.status_code != 200
    # Checksum verification happens before any pointer mutation.
    still_current = signed(service, "GET", "/backup-bucket/asset.bin")
    assert still_current.status_code == 200
    assert still_current.content == original
    # Failure can be inspected/retried; no visible truncated object.
    listed = signed(service, "GET", f"/backup-bucket/asset.bin?uploadId={uid}")
    assert listed.status_code == 200


def test_list_multipart_uploads_is_owner_scoped_and_paginated(service):
    setup(service)
    ids = [begin(service, key=key) for key in ("a.bin", "b.bin", "c.bin")]
    from teledrive.s3.credentials import S3CredentialStore
    connection = service[3]()
    try:
        stranger = S3CredentialStore(connection, service[4]).create(
            "stranger", ["s3:ListBucket"]
        )
    finally:
        connection.close()
    unauthorized = signed(service, "GET", "/backup-bucket?uploads",
                          access=stranger.access_key, secret=stranger.secret_key)
    assert unauthorized.status_code == 200
    assert ET.fromstring(unauthorized.content).findall(NS+"Upload") == []
    first = signed(service, "GET", "/backup-bucket?uploads&max-uploads=2")
    assert first.status_code == 200, first.text
    root = ET.fromstring(first.content)
    assert [e.findtext(NS+"Key") for e in root.findall(NS+"Upload")] == ["a.bin", "b.bin"]
    assert root.findtext(NS+"IsTruncated") == "true"
    key_marker = root.findtext(NS+"NextKeyMarker")
    upload_marker = root.findtext(NS+"NextUploadIdMarker")
    from urllib.parse import quote
    final = signed(service, "GET",
                   "/backup-bucket?uploads&max-uploads=2&key-marker="+quote(key_marker)
                   +"&upload-id-marker="+quote(upload_marker))
    assert final.status_code == 200, final.text
    assert [e.findtext(NS+"Key") for e in ET.fromstring(final.content).findall(NS+"Upload")] == ["c.bin"]
    assert signed(service, "DELETE", "/backup-bucket/b.bin?uploadId="+ids[1]).status_code == 204
    after_abort = signed(service, "GET", "/backup-bucket?uploads")
    assert [e.findtext(NS+"Key") for e in ET.fromstring(after_abort.content).findall(NS+"Upload")] == [
        "a.bin", "c.bin"
    ]



def test_list_parts_sql_paging_and_replaced_part_metadata(service):
    setup(service)
    uid = begin(service)
    part(service, uid, 9, b"original")
    replacement = part(service, uid, 9, b"replacement")
    part(service, uid, 1, b"first")
    part(service, uid, 5, b"middle")
    before_download = service[2].downloads
    first = signed(service, "GET",
                   f"/backup-bucket/asset.bin?uploadId={uid}&max-parts=2")
    assert first.status_code == 200, first.text
    root = ET.fromstring(first.content)
    assert [n.findtext(NS + "PartNumber") for n in root.findall(NS + "Part")] == ["1", "5"]
    assert root.findtext(NS + "StorageClass") == "STANDARD"
    assert root.findtext(NS + "IsTruncated") == "true"
    assert root.findtext(NS + "NextPartNumberMarker") == "5"
    for entry in root.findall(NS + "Part"):
        assert entry.findtext(NS + "LastModified").endswith("Z")
        assert entry.findtext(NS + "Size")
    later = signed(service, "GET",
                   f"/backup-bucket/asset.bin?uploadId={uid}&max-parts=2&part-number-marker=5")
    assert later.status_code == 200
    rest = ET.fromstring(later.content)
    assert rest.findtext(NS + "IsTruncated") == "false"
    parts = rest.findall(NS + "Part")
    assert [p.findtext(NS + "PartNumber") for p in parts] == ["9"]
    assert parts[0].findtext(NS + "ETag") == replacement
    assert service[2].downloads == before_download
    for marker, maxparts in (
        ("-1", "1"), ("10001", "1"), ("no", "1"), ("1.0", "1"),
        ("0", "0"), ("0", "1001"), ("0", "foo"),
    ):
        invalid = signed(service, "GET",
                         f"/backup-bucket/asset.bin?uploadId={uid}"
                         f"&part-number-marker={marker}&max-parts={maxparts}")
        assert invalid.status_code == 400, (marker, maxparts, invalid.text)
        assert b"InvalidArgument" in invalid.content
    assert service[2].downloads == before_download

    # A completed upload is no longer an active ListParts target.
    assert complete(service, uid, [(9, replacement)]).status_code == 200
    inactive = signed(service, "GET", f"/backup-bucket/asset.bin?uploadId={uid}")
    assert inactive.status_code == 404 and b"NoSuchUpload" in inactive.content


def test_list_multipart_uploads_grouping_unicode_encoding_and_cursors(service):
    setup(service)
    # Interleaved prefixes, same-key duplicate uploads and Unicode keys.
    first_a = begin(service, key="assets/a/one")
    begin(service, key="assets/a/two")
    first_b = begin(service, key="assets/b/one")
    begin(service, key="assets/b/two")
    begin(service, key="assets/single")
    begin(service, key="misc/ü(1).bin")
    before_download = service[2].downloads

    grouped = signed(service, "GET",
                     "/backup-bucket?uploads&prefix=assets%2F&delimiter=%2F&max-uploads=2")
    assert grouped.status_code == 200, grouped.text
    root = ET.fromstring(grouped.content)
    assert root.findtext(NS + "Prefix") == "assets/"
    assert root.findtext(NS + "Delimiter") == "/"
    assert [node.findtext(NS + "Prefix") for node in root.findall(NS+"CommonPrefixes")] == [
        "assets/a/", "assets/b/",
    ]
    assert root.findtext(NS + "IsTruncated") == "true"
    assert root.findtext(NS + "NextKeyMarker") == "assets/b/"
    from urllib.parse import quote
    next_page = signed(service, "GET",
                       "/backup-bucket?uploads&prefix=assets%2F&delimiter=%2F&max-uploads=2"
                       "&key-marker=" + quote(root.findtext(NS+"NextKeyMarker"), safe="")
                       + "&upload-id-marker=")
    assert next_page.status_code == 200, next_page.text
    follow = ET.fromstring(next_page.content)
    assert [node.findtext(NS+"Key") for node in follow.findall(NS+"Upload")] == ["assets/single"]
    assert follow.find(NS+"CommonPrefixes") is None
    assert follow.findtext(NS+"IsTruncated") == "false"

    utf8 = signed(service, "GET",
                  "/backup-bucket?uploads&prefix=misc%2F&encoding-type=url")
    assert utf8.status_code == 200, utf8.text
    encoded = ET.fromstring(utf8.content)
    assert encoded.findtext(NS+"EncodingType") == "url"
    assert encoded.findtext(NS+"Upload/"+NS+"Key") == "misc/%C3%BC%281%29.bin"

    same_key = begin(service, key="repeat")
    same_key2 = begin(service, key="repeat")
    first = signed(service, "GET", "/backup-bucket?uploads&prefix=repeat&max-uploads=1")
    assert first.status_code == 200
    start = ET.fromstring(first.content)
    assert start.findtext(NS+"IsTruncated") == "true"
    last_id = start.findtext(NS+"NextUploadIdMarker")
    last_key = start.findtext(NS+"NextKeyMarker")
    next_result = signed(service, "GET",
        "/backup-bucket?uploads&prefix=repeat&max-uploads=1"
        "&key-marker="+quote(last_key)+"&upload-id-marker="+quote(last_id))
    assert next_result.status_code == 200
    end = ET.fromstring(next_result.content)
    assert len(end.findall(NS+"Upload")) == 1
    assert end.findtext(NS+"Upload/"+NS+"UploadId") in {same_key, same_key2} - {last_id}
    assert end.findtext(NS+"Upload/"+NS+"StorageClass") == "STANDARD"
    assert end.findtext(NS+"IsTruncated") == "false"

    for path in (
        "/backup-bucket?uploads&encoding-type=garbage",
        "/backup-bucket?uploads&max-uploads=0",
        "/backup-bucket?uploads&max-uploads=1001",
        "/backup-bucket?uploads&upload-id-marker=x",
    ):
        failure = signed(service, "GET", path)
        assert failure.status_code == 400, (path, failure.text)
    assert service[2].downloads == before_download
    assert signed(service, "DELETE",
                  "/backup-bucket/assets/a/one?uploadId="+first_a).status_code == 204
    assert signed(service, "DELETE",
                  "/backup-bucket/assets/b/one?uploadId="+first_b).status_code == 204


def test_list_multipart_pages_are_scoped_to_credential_without_telegram_reads(service):
    setup(service)
    uid = begin(service)
    part(service, uid, 1, b"private")
    db = service[3]()
    try:
        guest = S3CredentialStore(db, service[4]).create(
            "listing-only", ["s3:ListBucket", "s3:GetObject"],
        )
    finally:
        db.close()
    before = service[2].downloads
    page = signed(service, "GET", "/backup-bucket?uploads&delimiter=%2F",
                  access=guest.access_key, secret=guest.secret_key)
    assert page.status_code == 200
    assert ET.fromstring(page.content).findall(NS+"Upload") == []
    denied = signed(service, "GET", f"/backup-bucket/asset.bin?uploadId={uid}",
                    access=guest.access_key, secret=guest.secret_key)
    assert denied.status_code == 404 and b"NoSuchUpload" in denied.content
    assert service[2].downloads == before

def test_bucket_delete_rejects_uncommitted_multipart_history(service):
    setup(service)
    uid = begin(service)
    before = signed(service, "DELETE", "/backup-bucket")
    assert before.status_code == 409
    assert xml_text(before, "Code") == "BucketNotEmpty"
    # Upload remains usable after failed bucket deletion.
    assert signed(service, "GET", "/backup-bucket?uploads").status_code == 200
    assert signed(service, "DELETE", f"/backup-bucket/asset.bin?uploadId={uid}").status_code == 204
    # Abort retains orphan-cleanup audit metadata; still not safe to delete.
    after = signed(service, "DELETE", "/backup-bucket")
    assert after.status_code == 409 and xml_text(after, "Code") == "BucketNotEmpty"
