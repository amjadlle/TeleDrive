"""Checksum-bound presigned multipart Complete never permits mutable XML bodies."""
from __future__ import annotations

import hashlib
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from botocore.auth import S3SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from test_s3_gateway import service, signed
from test_s3_multipart import begin, part
from teledrive.s3.credentials import S3CredentialStore


def _url(service, upload_id, payload, *, checksum=True, extra=""):
    issued = service[1]
    headers = {}
    if checksum:
        headers["x-amz-content-sha256"] = hashlib.sha256(payload).hexdigest()
    request = AWSRequest(
        method="POST",
        url="http://localhost:9000/backup-bucket/asset.bin?uploadId=" + upload_id + extra,
        # Query presigning must not parse XML as botocore JSON/form data.
        # SigV4 authenticates method, path/query and the signed XML SHA-256.
        data=b"",
        headers=headers,
    )
    request.context["payload_signing_enabled"] = False
    S3SigV4QueryAuth(
        Credentials(issued.access_key, issued.secret_key),
        "s3", "us-east-1", expires=120,
    ).add_auth(request)
    parsed = urlsplit(request.url)
    return parsed.path + "?" + parsed.query, headers


def _body(number, etag):
    root = ET.Element("CompleteMultipartUpload")
    item = ET.SubElement(root, "Part")
    ET.SubElement(item, "PartNumber").text = str(number)
    ET.SubElement(item, "ETag").text = etag
    return ET.tostring(root)


def test_presigned_complete_binds_xml_and_does_not_reupload(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    uid = begin(service)
    etag = part(service, uid, 1, b"bound-multipart-part")
    xml = _body(1, etag)
    url, headers = _url(service, uid, xml)
    before = service[2].uploads
    mutated = service[0].post(url, content=xml + b" ",
                              headers=headers)
    assert mutated.status_code == 400, mutated.text
    assert service[2].uploads == before
    done = service[0].post(url, content=xml, headers=headers)
    assert done.status_code == 200, done.text
    assert service[2].uploads == before
    assert signed(service, "GET", "/backup-bucket/asset.bin").content == b"bound-multipart-part"
    again = service[0].post(url, content=xml, headers=headers)
    assert again.status_code == 200
    assert service[2].uploads == before


def test_presigned_complete_requires_signed_checksum_and_existing_upload(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    uid = begin(service)
    etag = part(service, uid, 1, b"keep")
    xml = _body(1, etag)
    unchecked, headers = _url(service, uid, xml, checksum=False)
    before = service[2].uploads
    denied = service[0].post(unchecked, content=xml, headers=headers)
    assert denied.status_code == 403, denied.text
    assert b"AccessDenied" in denied.content
    assert service[2].uploads == before
    # Other POST query selectors cannot borrow the protected Complete flow.
    unsupported, bound_headers = _url(service, uid, xml, extra="&uploads")
    bad = service[0].post(unsupported, content=xml, headers=bound_headers)
    assert bad.status_code == 501
    assert service[2].uploads == before


def test_revoked_presigned_complete_does_not_publish(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    uid = begin(service)
    etag = part(service, uid, 1, b"staged")
    xml = _body(1, etag)
    url, headers = _url(service, uid, xml)
    db = service[3]()
    try:
        assert S3CredentialStore(db, service[4]).revoke(service[1].id)
    finally:
        db.close()
    before = service[2].uploads
    denied = service[0].post(url, content=xml, headers=headers)
    assert denied.status_code == 403
    assert b"AccessDenied" in denied.content
    assert service[2].uploads == before


def _presigned_initiate(service, *, checksum=True, extra=""):
    issued = service[1]
    headers = {}
    if checksum:
        headers["x-amz-content-sha256"] = hashlib.sha256(b"").hexdigest()
    request = AWSRequest(
        method="POST",
        url="http://localhost:9000/backup-bucket/asset.bin?uploads" + extra,
        data=b"", headers=headers,
    )
    request.context["payload_signing_enabled"] = False
    S3SigV4QueryAuth(
        Credentials(issued.access_key, issued.secret_key),
        "s3", "us-east-1", expires=120,
    ).add_auth(request)
    parsed = urlsplit(request.url)
    return parsed.path + "?" + parsed.query, headers


def _multipart_session_count(service):
    from sqlalchemy import func, select
    from teledrive.s3.multipart import MultipartStore, uploads
    from teledrive.storage.catalog import BucketCatalog
    from teledrive.storage.manifest import ManifestStore
    db = service[3]()
    try:
        # The first test may inspect sessions before any multipart endpoint
        # has initialized the schema.
        MultipartStore(BucketCatalog(ManifestStore(db)))
        return db.connection.execute(
            select(func.count()).select_from(uploads)
        ).scalar_one()
    finally:
        db.close()


def test_presigned_initiate_accepts_exact_empty_body_and_existing_permissions(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    url, headers = _presigned_initiate(service)
    before = _multipart_session_count(service)
    created = service[0].post(url, content=b"", headers=headers)
    assert created.status_code == 200, created.text
    assert b"<UploadId>" in created.content
    assert _multipart_session_count(service) == before + 1
    assert service[2].uploads == 0


def test_presigned_initiate_rejects_tampered_body_without_sql_session(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    url, headers = _presigned_initiate(service)
    before = _multipart_session_count(service)
    denied = service[0].post(url, content=b"unexpected-body", headers=headers)
    assert denied.status_code == 400 and b"InvalidRequest" in denied.content
    assert _multipart_session_count(service) == before
    assert service[2].uploads == 0


def test_presigned_initiate_requires_signed_checksum_and_exact_selector(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    before = _multipart_session_count(service)
    unsigned_url, missing_headers = _presigned_initiate(service, checksum=False)
    missing = service[0].post(unsigned_url, content=b"", headers=missing_headers)
    assert missing.status_code == 403 and b"AccessDenied" in missing.content
    mixed_url, bound_headers = _presigned_initiate(service, extra="&uploadId=other")
    mixed = service[0].post(mixed_url, content=b"", headers=bound_headers)
    assert mixed.status_code == 501 and b"NotImplemented" in mixed.content
    url, headers = _presigned_initiate(service)
    wrong_key = service[0].post(url.replace("asset.bin", "other.bin"),
                                content=b"", headers=headers)
    assert wrong_key.status_code == 403
    assert _multipart_session_count(service) == before


def test_revoked_presigned_initiate_never_allocates_session(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    url, headers = _presigned_initiate(service)
    before = _multipart_session_count(service)
    db = service[3]()
    try:
        assert S3CredentialStore(db, service[4]).revoke(service[1].id)
    finally:
        db.close()
    response = service[0].post(url, content=b"", headers=headers)
    assert response.status_code == 403 and b"AccessDenied" in response.content
    assert _multipart_session_count(service) == before
