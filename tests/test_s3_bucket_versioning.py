"""S3 bucket versioning: honest always-on immutable version semantics."""
from __future__ import annotations

import hashlib
import socket
import threading

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import pytest

from test_s3_gateway import service, signed
from teledrive.db import connect_database
from teledrive.s3.credentials import S3CredentialStore
from teledrive.s3.versioning import parse_versioning_xml, VersioningError

VALID = (
    b'<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    b'<Status>Enabled</Status></VersioningConfiguration>'
)


def test_versioning_get_put_idempotent_and_real_object_history(service):
    client, _, remote, _, _ = service
    assert signed(service, "PUT", "/versioning-ci").status_code == 200
    path = "/versioning-ci?versioning"
    response = signed(service, "GET", path)
    assert response.status_code == 200, response.text
    assert b"<Status>Enabled</Status>" in response.content
    assert signed(service, "PUT", path, VALID).status_code == 200
    assert signed(service, "PUT", path, VALID).status_code == 200
    first = signed(service, "PUT", "/versioning-ci/data.bin", b"one")
    second = signed(service, "PUT", "/versioning-ci/data.bin", b"two")
    assert first.status_code == second.status_code == 200
    older = first.headers["x-amz-version-id"]
    newest = second.headers["x-amz-version-id"]
    assert older != newest
    assert signed(service, "GET",
                  "/versioning-ci/data.bin?versionId=" + older).content == b"one"
    assert signed(service, "GET", "/versioning-ci/data.bin").content == b"two"
    assert remote.uploads == 2


@pytest.mark.parametrize("bad,code", [
    (b"<VersioningConfiguration><Status>Suspended</Status></VersioningConfiguration>", 501),
    (b"<VersioningConfiguration><Status>Disabled</Status></VersioningConfiguration>", 400),
    (b"<VersioningConfiguration></VersioningConfiguration>", 400),
    (b"<VersioningConfiguration><MFADelete>Disabled</MFADelete><Status>Enabled</Status></VersioningConfiguration>", 501),
    (b"<VersioningConfiguration><Status>Enabled</Status><Status>Enabled</Status></VersioningConfiguration>", 400),
    (b'<!DOCTYPE x [<!ENTITY a "Enabled">]><VersioningConfiguration><Status>&a;</Status></VersioningConfiguration>', 400),
    (b"x" * 16385, 400),
])
def test_unsafe_and_malformed_versioning_requests_do_not_change_history(service, bad, code):
    client, _, remote, _, _ = service
    assert signed(service, "PUT", "/versioning-ci").status_code == 200
    before = remote.uploads
    response = signed(service, "PUT", "/versioning-ci?versioning", bad)
    assert response.status_code == code, response.text
    assert remote.uploads == before
    assert b"Enabled" in signed(service, "GET", "/versioning-ci?versioning").content


def test_versioning_control_permissions_are_separate_from_putobject(service):
    client, _, remote, connect, master = service
    assert signed(service, "PUT", "/versioning-ci").status_code == 200
    db = connect()
    try:
        credentials = S3CredentialStore(db, master)
        reader = credentials.create("versioning-read", ["s3:GetBucketVersioning"])
        writer = credentials.create("versioning-write", ["s3:PutBucketVersioning"])
        object_key = credentials.create("versioning-objects", ["s3:PutObject"])
    finally:
        db.close()
    path = "/versioning-ci?versioning"
    assert signed(service, "GET", path, access=reader.access_key, secret=reader.secret_key).status_code == 200
    assert signed(service, "PUT", path, VALID, access=reader.access_key, secret=reader.secret_key).status_code == 403
    assert signed(service, "GET", path, access=writer.access_key, secret=writer.secret_key).status_code == 403
    assert signed(service, "PUT", path, VALID, access=writer.access_key, secret=writer.secret_key).status_code == 200
    assert signed(service, "GET", path, access=object_key.access_key, secret=object_key.secret_key).status_code == 403
    assert signed(service, "PUT", path, VALID, access=object_key.access_key, secret=object_key.secret_key).status_code == 403
    assert remote.uploads == 0


def test_versioning_checksums_are_validated_even_with_signed_correct_sha256(service):
    import base64
    import zlib

    assert signed(service, "PUT", "/versioning-ci").status_code == 200
    correct_crc32 = base64.b64encode(
        (zlib.crc32(VALID) & 0xffffffff).to_bytes(4, "big"),
    ).decode("ascii")
    verified = signed(service, "PUT", "/versioning-ci?versioning", VALID,
                     extra_headers={
                         "x-amz-checksum-crc32": correct_crc32,
                         "x-amz-sdk-checksum-algorithm": "CRC32",
                     })
    assert verified.status_code == 200, verified.text
    bad_crc = signed(service, "PUT", "/versioning-ci?versioning", VALID,
                     extra_headers={
                         "x-amz-checksum-crc32": base64.b64encode(
                             b"nope",
                         ).decode("ascii"),
                         "x-amz-sdk-checksum-algorithm": "CRC32",
                     })
    assert bad_crc.status_code == 400, bad_crc.text
    bad_md5 = signed(service, "PUT", "/versioning-ci?versioning", VALID,
                     extra_headers={
                         "Content-MD5": base64.b64encode(b"\x00" * 16).decode(),
                     })
    assert bad_md5.status_code == 400, bad_md5.text


def test_missing_bucket_and_ambiguous_selectors_fail_closed(service):
    assert signed(service, "GET", "/missing-versioning?versioning").status_code == 404
    assert signed(service, "PUT", "/missing-versioning?versioning", VALID).status_code == 404
    assert signed(service, "PUT", "/versioning-ci").status_code == 200
    for path in ("/versioning-ci?versioning&versioning",
                 "/versioning-ci?versioning&policy"):
        assert signed(service, "GET", path).status_code in (400, 501)
    assert signed(service, "DELETE", "/versioning-ci?versioning").status_code == 501
    assert signed(service, "GET", "/versioning-ci/data.bin?versioning").status_code == 501
    assert signed(service, "DELETE", "/versioning-ci/data.bin?versioning").status_code == 501


def test_real_boto3_get_put_bucket_versioning(service):
    import uvicorn
    import web.s3 as gateway

    _, creds, _, _, _ = service
    assert signed(service, "PUT", "/versioning-ci").status_code == 200
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        gateway.app, host="127.0.0.1", port=port, log_level="error",
        lifespan="off",
    ))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                              daemon=True)
    thread.start()
    try:
        import time
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        client = boto3.client("s3", endpoint_url=f"http://127.0.0.1:{port}",
                              region_name="us-east-1",
                              aws_access_key_id=creds.access_key,
                              aws_secret_access_key=creds.secret_key,
                              config=Config(signature_version="s3v4",
                                            s3={"addressing_style": "path"}))
        assert client.get_bucket_versioning(Bucket="versioning-ci")["Status"] == "Enabled"
        reply = client.put_bucket_versioning(
            Bucket="versioning-ci",
            VersioningConfiguration={"Status": "Enabled"},
        )
        assert reply["ResponseMetadata"]["HTTPStatusCode"] == 200
        with pytest.raises(ClientError):
            client.put_bucket_versioning(
                Bucket="versioning-ci",
                VersioningConfiguration={"Status": "Suspended"},
            )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
