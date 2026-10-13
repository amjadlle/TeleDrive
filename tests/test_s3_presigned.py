"""Real boto3-generated short-lived S3 presign regression tests.

No in-memory signature stubs: botocore produces the wire URL and TestClient
executes it against the production SigV4 verifier and real SQL credential store.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import boto3
import pytest
from botocore.config import Config
from starlette.requests import Request

from test_s3_gateway import service, signed
from teledrive.s3.credentials import S3CredentialStore
from teledrive.s3.sigv4 import SigV4Error, verify_header_signature


def sdk(service, access=None, secret=None):
    credential = service[1]
    return boto3.client(
        "s3", endpoint_url="http://localhost:9000",
        aws_access_key_id=access or credential.access_key,
        aws_secret_access_key=secret or credential.secret_key,
        region_name="us-east-1",
        config=Config(signature_version="s3v4",
                      s3={"addressing_style": "path"},
                      request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"),
    )


def request_url(client, method, presigned_url, **kwargs):
    uri = urlsplit(presigned_url)
    assert uri.netloc == "localhost:9000"
    return client.request(method, uri.path + "?" + uri.query, **kwargs)


def test_boto3_presigned_get_and_head_obey_scopes(service):
    client, _, remote, _, _ = service
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    payload = b"telegram-presigned-read" * 120
    assert signed(service, "PUT", "/backup-bucket/signed.bin", payload).status_code == 200
    s3 = sdk(service)
    get_url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": "backup-bucket", "Key": "signed.bin"}, ExpiresIn=120,
    )
    result = request_url(client, "GET", get_url)
    assert result.status_code == 200, result.text
    assert result.content == payload
    head_url = s3.generate_presigned_url(
        "head_object", Params={"Bucket": "backup-bucket", "Key": "signed.bin"}, ExpiresIn=120,
    )
    before = remote.downloads
    head = request_url(client, "HEAD", head_url)
    assert head.status_code == 200
    assert head.headers["content-length"] == str(len(payload))
    assert remote.downloads == before


def test_checksum_bound_presigned_put_prevents_unapproved_bytes(service):
    client, _, remote, _, _ = service
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    body = b"presigned-telegram-write" * 38
    checksum = base64.b64encode(hashlib.sha256(body).digest()).decode()
    url = sdk(service).generate_presigned_url(
        "put_object",
        Params={"Bucket": "backup-bucket", "Key": "signed-put.bin",
                "ChecksumSHA256": checksum},
        ExpiresIn=120,
        HttpMethod="PUT",
    )
    before = remote.uploads
    corrupt = request_url(client, "PUT", url, content=b"modified-payload",
                          headers={"x-amz-checksum-sha256": checksum})
    assert corrupt.status_code == 400
    assert remote.uploads == before
    assert signed(service, "GET", "/backup-bucket/signed-put.bin").status_code == 404
    authorized = request_url(client, "PUT", url, content=body,
                             headers={"x-amz-checksum-sha256": checksum})
    assert authorized.status_code == 200, authorized.text
    assert remote.uploads == before + 1
    assert signed(service, "GET", "/backup-bucket/signed-put.bin").content == body


def test_bare_presigned_put_is_refused_not_accepted_unsigned(service):
    client, _, remote, _, _ = service
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    url = sdk(service).generate_presigned_url(
        "put_object", Params={"Bucket": "backup-bucket", "Key": "unsigned.bin"},
        ExpiresIn=120, HttpMethod="PUT",
    )
    before = remote.uploads
    denied = request_url(client, "PUT", url, content=b"unbound")
    assert denied.status_code == 403
    assert b"AccessDenied" in denied.content
    assert remote.uploads == before


def test_presign_query_path_tampering_duplicate_params_and_revocation(service):
    client, credential, _, connect, master = service
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/unchanged.bin", b"correct").status_code == 200
    url = sdk(service).generate_presigned_url(
        "get_object", Params={"Bucket": "backup-bucket", "Key": "unchanged.bin"},
        ExpiresIn=120,
    )
    parsed = urlsplit(url)
    corrupted_path = parsed.path.replace("unchanged.bin", "spoofed.bin")
    bad_path = client.get(corrupted_path + "?" + parsed.query)
    assert bad_path.status_code == 403
    assert b"SignatureDoesNotMatch" in bad_path.content
    replay_extra = client.get(parsed.path + "?" + parsed.query + "&X-Amz-Expires=120")
    assert replay_extra.status_code == 403
    connection = connect()
    try:
        assert S3CredentialStore(connection, master).revoke(credential.id)
    finally:
        connection.close()
    denied = request_url(client, "GET", url)
    assert denied.status_code == 403
    assert b"AccessDenied" in denied.content


def test_presigned_expiration_is_enforced_without_sleep(service):
    _, credential, _, connect, master = service
    url = sdk(service).generate_presigned_url(
        "list_buckets", Params={}, ExpiresIn=3,
    )
    parsed = urlsplit(url)
    scope = {
        "type": "http", "method": "GET", "path": parsed.path,
        "raw_path": parsed.path.encode("ascii"),
        "query_string": parsed.query.encode("ascii"),
        "headers": [(b"host", b"localhost:9000")],
    }
    request = Request(scope)
    db = connect()
    try:
        store = S3CredentialStore(db, master)
        assert verify_header_signature(
            request, store.resolve, "s3:ListAllMyBuckets",
            now=datetime.now(timezone.utc)
        ) == credential.access_key
        with pytest.raises(SigV4Error, match="expired") as caught:
            verify_header_signature(
                request, store.resolve, "s3:ListAllMyBuckets",
                now=datetime.now(timezone.utc) + timedelta(seconds=10)
            )
        assert caught.value.code == "AccessDenied"
    finally:
        db.close()
