"""S3 private-only GetBucketPolicyStatus protocol and permission coverage."""
from __future__ import annotations

import threading
import time
from xml.etree import ElementTree as ET

import boto3
import pytest
import uvicorn
from botocore.config import Config

from tests.s3.test_s3_gateway import service, signed
from teledrive.s3.credentials import S3CredentialStore
import web.s3 as gateway


def test_policy_status_always_false_and_is_read_only(service):
    assert signed(service, "PUT", "/policy-ci").status_code == 200
    status = signed(service, "GET", "/policy-ci?policyStatus")
    assert status.status_code == 200, status.text
    root = ET.fromstring(status.content)
    assert root.tag.rsplit("}", 1)[-1] == "PolicyStatus"
    assert root.findtext("{http://s3.amazonaws.com/doc/2006-03-01/}IsPublic") == "false"
    assert signed(service, "PUT", "/policy-ci?policyStatus").status_code == 501
    assert signed(service, "DELETE", "/policy-ci?policyStatus").status_code == 501
    assert signed(service, "HEAD", "/policy-ci").status_code == 200
    assert service[2].uploads == 0 and service[2].downloads == 0


def test_policy_status_missing_or_unauthorized_bucket(service):
    assert signed(service, "GET", "/policy-missing?policyStatus").status_code == 404
    assert signed(service, "PUT", "/policy-ci").status_code == 200
    assert service[0].get("/policy-ci?policyStatus").status_code == 403
    db = service[3]()
    try:
        credentials = S3CredentialStore(db, service[4])
        policy_reader = credentials.create(
            "get-private-policy-status", ["s3:GetBucketPolicyStatus"],
        )
        object_reader = credentials.create("object-reader", ["s3:GetObject"])
    finally:
        db.close()
    result = signed(service, "GET", "/policy-ci?policyStatus",
                    access=policy_reader.access_key, secret=policy_reader.secret_key)
    assert result.status_code == 200, result.text
    assert signed(service, "GET", "/policy-ci?policyStatus",
                  access=object_reader.access_key, secret=object_reader.secret_key).status_code == 403
    assert signed(service, "GET", "/policy-ci",
                  access=policy_reader.access_key, secret=policy_reader.secret_key).status_code == 403


@pytest.mark.parametrize("method,path", [
    ("GET", "/policy-ci?policyStatus=true"),
    ("GET", "/policy-ci?policyStatus&policyStatus"),
    ("GET", "/policy-ci?policyStatus&tagging"),
    ("GET", "/policy-ci/item?policyStatus"),
    ("PUT", "/policy-ci?policyStatus"),
    ("DELETE", "/policy-ci?policyStatus"),
])
def test_policy_status_cannot_override_or_mutate_routes(service, method, path):
    assert signed(service, "PUT", "/policy-ci").status_code == 200
    response = signed(service, method, path)
    assert response.status_code in (400, 501), response.text
    assert signed(service, "HEAD", "/policy-ci").status_code == 200
    assert service[2].uploads == 0


def test_real_boto3_get_bucket_policy_status_private_only(service):
    from socket import socket
    assert signed(service, "PUT", "/policy-ci").status_code == 200
    sock = socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        gateway.app, host="127.0.0.1", port=port, log_level="error",
        lifespan="off",
    ))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                              daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.02)
        assert server.started
        credential = service[1]
        client = boto3.client(
            "s3", endpoint_url=f"http://127.0.0.1:{port}", region_name="us-east-1",
            aws_access_key_id=credential.access_key,
            aws_secret_access_key=credential.secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        actual = client.get_bucket_policy_status(Bucket="policy-ci")
        assert actual["PolicyStatus"] == {"IsPublic": False}
        # No public bucket-policy engine can be enabled via the same API.
        with pytest.raises(client.exceptions.ClientError):
            client.put_bucket_policy(
                Bucket="policy-ci",
                Policy='{"Version":"2012-10-17","Statement":[]}',
            )
    finally:
        server.should_exit = True
        worker.join(timeout=10)
