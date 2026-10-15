"""Public access block maps to the actual enforced private-only contract."""
from __future__ import annotations

import hashlib
from xml.etree import ElementTree as ET

import pytest

from tests.s3.test_s3_gateway import service, signed
from teledrive.s3.credentials import S3CredentialStore
from teledrive.s3.public_access import public_access_xml

ALL_TRUE = public_access_xml()


def test_private_only_bucket_public_access_controls_and_no_payload_calls(service):
    client, _, remote, _, _ = service
    assert signed(service, "PUT", "/private-ci").status_code == 200
    path = "/private-ci?publicAccessBlock"
    initial = signed(service, "GET", path)
    assert initial.status_code == 200, initial.text
    root = ET.fromstring(initial.content)
    assert len(root) == 4
    assert all(child.text == "true" for child in root)
    assert signed(service, "PUT", path, ALL_TRUE).status_code == 200
    assert signed(service, "PUT", path, ALL_TRUE).status_code == 200
    assert signed(service, "DELETE", path).status_code == 501
    assert remote.uploads == 0 and remote.downloads == 0
    assert signed(service, "GET", "/private-ci/file.bin").status_code == 404
    # Anonymous reads remain denied, even after PUT controls.
    assert client.get("/private-ci/file.bin").status_code == 403


@pytest.mark.parametrize("bad,status", [
    (b"<PublicAccessBlockConfiguration></PublicAccessBlockConfiguration>", 400),
    (b'<PublicAccessBlockConfiguration><BlockPublicAcls>false</BlockPublicAcls><IgnorePublicAcls>true</IgnorePublicAcls><BlockPublicPolicy>true</BlockPublicPolicy><RestrictPublicBuckets>true</RestrictPublicBuckets></PublicAccessBlockConfiguration>', 501),
    (b"<!DOCTYPE r [<!ENTITY x 'true'>]><PublicAccessBlockConfiguration><BlockPublicAcls>&x;</BlockPublicAcls></PublicAccessBlockConfiguration>", 400),
    (b"x" * 16385, 400),
    (ALL_TRUE.replace(b"<BlockPublicPolicy>true</BlockPublicPolicy>", b""), 400),
    (ALL_TRUE.replace(b"<RestrictPublicBuckets>true</RestrictPublicBuckets>", b"<RestrictPublicBuckets>TRUE</RestrictPublicBuckets>"), 400),
])
def test_public_access_cannot_be_disabled_or_partially_configured(service, bad, status):
    assert signed(service, "PUT", "/private-ci").status_code == 200
    answer = signed(service, "PUT", "/private-ci?publicAccessBlock", bad)
    assert answer.status_code == status, answer.text
    assert b"true" in signed(service, "GET", "/private-ci?publicAccessBlock").content


def test_private_control_permission_separation(service):
    _, _, _, connect, master = service
    assert signed(service, "PUT", "/private-ci").status_code == 200
    db = connect()
    try:
        store = S3CredentialStore(db, master)
        reader = store.create("read-public-config", ["s3:GetBucketPublicAccessBlock"])
        writer = store.create("write-public-config", ["s3:PutBucketPublicAccessBlock"])
        other = store.create("unrelated", ["s3:GetObject"])
    finally:
        db.close()
    path = "/private-ci?publicAccessBlock"
    assert signed(service, "GET", path, access=reader.access_key, secret=reader.secret_key).status_code == 200
    assert signed(service, "PUT", path, ALL_TRUE, access=reader.access_key, secret=reader.secret_key).status_code == 403
    assert signed(service, "GET", path, access=writer.access_key, secret=writer.secret_key).status_code == 403
    assert signed(service, "PUT", path, ALL_TRUE, access=writer.access_key, secret=writer.secret_key).status_code == 200
    assert signed(service, "GET", path, access=other.access_key, secret=other.secret_key).status_code == 403


def test_missing_bucket_selector_conflicts_and_signed_checksums(service):
    assert signed(service, "GET", "/missing-private?publicAccessBlock").status_code == 404
    assert signed(service, "PUT", "/missing-private?publicAccessBlock", ALL_TRUE).status_code == 404
    assert signed(service, "PUT", "/private-ci").status_code == 200
    for path in ("/private-ci?publicAccessBlock&publicAccessBlock",
                 "/private-ci?publicAccessBlock&tagging"):
        assert signed(service, "GET", path).status_code == 400
    assert signed(service, "GET", "/private-ci/item?publicAccessBlock").status_code == 501
    assert signed(service, "PUT", "/private-ci?publicAccessBlock", ALL_TRUE,
                  extra_headers={"Content-MD5": "AAAA"}).status_code == 400


def test_actual_boto3_public_access_block_client(service):
    import socket
    import threading
    import time
    import uvicorn
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
    import web.s3 as gateway

    _, credential, remote, _, _ = service
    assert signed(service, "PUT", "/private-ci").status_code == 200
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        gateway.app, host="127.0.0.1", port=port, log_level="error", lifespan="off",
    ))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        s3 = boto3.client("s3", endpoint_url=f"http://127.0.0.1:{port}",
                          region_name="us-east-1",
                          aws_access_key_id=credential.access_key,
                          aws_secret_access_key=credential.secret_key,
                          config=Config(signature_version="s3v4",
                                        s3={"addressing_style": "path"}))
        expected = {k: True for k in (
            "BlockPublicAcls", "IgnorePublicAcls",
            "BlockPublicPolicy", "RestrictPublicBuckets",
        )}
        assert s3.get_public_access_block(Bucket="private-ci")["PublicAccessBlockConfiguration"] == expected
        ok = s3.put_public_access_block(Bucket="private-ci", PublicAccessBlockConfiguration=expected)
        assert ok["ResponseMetadata"]["HTTPStatusCode"] == 200
        with pytest.raises(ClientError):
            s3.put_public_access_block(Bucket="private-ci",
                                       PublicAccessBlockConfiguration={
                                           **expected, "BlockPublicAcls": False,
                                       })
    finally:
        server.should_exit = True
        thread.join(timeout=10)
