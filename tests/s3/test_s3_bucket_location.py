"""S3 bucket location contract, policy isolation and real boto3 discovery."""
from __future__ import annotations

import socket
import threading
import time
from xml.etree import ElementTree as ET

import boto3
import uvicorn
from botocore.config import Config

from tests.s3.test_s3_gateway import service, signed
from teledrive.s3.credentials import S3CredentialStore


def test_get_bucket_location_exact_s3_xml_and_missing_bucket(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    location = signed(service, "GET", "/backup-bucket?location")
    assert location.status_code == 200, location.text
    root = ET.fromstring(location.content)
    assert root.tag == "{http://s3.amazonaws.com/doc/2006-03-01/}LocationConstraint"
    assert root.text is None
    missing = signed(service, "GET", "/missing-bucket?location")
    assert missing.status_code == 404
    assert b"NoSuchBucket" in missing.content


def test_bucket_location_requires_independent_s3_permission(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    db = service[3]()
    try:
        store = S3CredentialStore(db, service[4])
        reader = store.create("bucket-locator", ["s3:GetBucketLocation"])
        listing_only = store.create("bucket-list", ["s3:ListBucket"])
    finally:
        db.close()
    assert signed(service, "GET", "/backup-bucket?location",
                  access=reader.access_key, secret=reader.secret_key).status_code == 200
    assert signed(service, "GET", "/backup-bucket?list-type=2",
                  access=reader.access_key, secret=reader.secret_key).status_code == 403
    assert signed(service, "GET", "/backup-bucket?location",
                  access=listing_only.access_key, secret=listing_only.secret_key).status_code == 403


def test_real_boto3_get_bucket_location_http(service):
    import web.s3 as gateway
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(16)
    server = uvicorn.Server(uvicorn.Config(
        gateway.app, host="127.0.0.1", port=port, lifespan="off", log_level="error"))
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
            aws_access_key_id=service[1].access_key,
            aws_secret_access_key=service[1].secret_key,
            region_name="us-east-1",
            config=Config(signature_version="s3v4",
                          s3={"addressing_style": "path", "payload_signing_enabled": True},
                          retries={"max_attempts": 0}),
        )
        client.create_bucket(Bucket="sdk-location")
        assert client.get_bucket_location(Bucket="sdk-location")["LocationConstraint"] is None
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()
