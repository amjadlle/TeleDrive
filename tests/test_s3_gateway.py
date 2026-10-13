"""S3 SigV4 and Telegram-backed HTTP behavior tests.

Botocore signs real HTTP wire requests; Telegram transport holds real bytes
in a deterministic fake while SQLite stores only metadata and encrypted keys.
"""
from __future__ import annotations

import hashlib
import os
from urllib.parse import quote

import pytest
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import web.app as admin
import web.s3 as gateway
from teledrive.db import connect_database
from teledrive.s3.credentials import S3CredentialStore, DEFAULT_PERMISSIONS
from teledrive.storage.bot_api_adapter import BotApiChunkTransport, BotFileIndex


class FakeTelegram:
    def __init__(self):
        self.parts = {}
        self.uploads = 0
        self.downloads = 0

    async def call(self, method, fields, source=None):
        if method == "getMe":
            return {"id": 5}
        if method == "getChat":
            return {"id": -1001234567890, "type": "channel"}
        if method == "getChatMember":
            return {"status": "administrator", "can_post_messages": True}
        if method == "sendDocument":
            self.uploads += 1
            fid = f"file-{self.uploads}"
            self.parts[fid] = source.read_bytes()
            return {"message_id": self.uploads, "chat": {"id": -1001234567890},
                    "document": {"file_id": fid}}
        if method == "getFile":
            fid = fields["file_id"]
            return {"file_path": fid, "file_size": len(self.parts[fid])}
        raise AssertionError(method)

    async def download_file(self, file_path, destination):
        self.downloads += 1
        destination.write_bytes(self.parts[file_path])


@pytest.fixture
def service(monkeypatch, tmp_path):
    key = Fernet.generate_key()
    database_path = tmp_path / "state.db"
    db = connect_database(str(database_path))
    cred_store = S3CredentialStore(db, key)
    issued = cred_store.create("S3 integration", list(DEFAULT_PERMISSIONS))
    db.close()
    telegram = FakeTelegram()
    connection = lambda: connect_database(str(database_path))
    factory = lambda db: BotApiChunkTransport(
        telegram, BotFileIndex(db), chat_id=-1001234567890,
        account="api-bot", min_upload_interval=0,
    )
    monkeypatch.setattr(gateway, "db_connect", connection)
    monkeypatch.setattr(gateway, "storage_bot_transport", factory)
    monkeypatch.setattr(gateway, "load_master_key", lambda: key)
    monkeypatch.setenv("TELEDRIVE_STORAGE_SCRATCH_DIR", str(tmp_path / "staging"))
    monkeypatch.setenv("TELEDRIVE_REST_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024))
    client = TestClient(gateway.app, base_url="http://localhost:9000")
    try:
        yield client, issued, telegram, connection, key
    finally:
        client.close()


def signed(service, method, path, body=b"", *, extra_headers=None, access=None, secret=None, date=None):
    client, issued, _, _, _ = service
    headers = {"x-amz-content-sha256": hashlib.sha256(body).hexdigest()}
    headers.update(extra_headers or {})
    url = "http://localhost:9000" + path
    request = AWSRequest(method=method, url=url, data=body, headers=headers)
    request.context["payload_signing_enabled"] = headers["x-amz-content-sha256"] != "UNSIGNED-PAYLOAD"
    S3SigV4Auth(Credentials(access or issued.access_key,
                            secret or issued.secret_key), "s3", "us-east-1").add_auth(request)
    if date:
        request.headers["X-Amz-Date"] = date
    return client.request(method, path, content=body, headers=dict(request.headers))


def test_s3_basic_bucket_object_crud_and_range(service):
    client, creds, telegram, _, _ = service
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    buckets = signed(service, "GET", "/")
    assert buckets.status_code == 200 and b"backup-bucket" in buckets.content
    payload = os.urandom(8193)
    uploaded = signed(service, "PUT", "/backup-bucket/file.bin", payload)
    assert uploaded.status_code == 200, uploaded.text
    assert telegram.uploads == 1
    meta = signed(service, "HEAD", "/backup-bucket/file.bin")
    assert meta.status_code == 200
    assert meta.headers["content-length"] == str(len(payload))
    assert telegram.downloads == 0
    response = signed(service, "GET", "/backup-bucket/file.bin")
    assert response.status_code == 200
    assert response.content == payload
    rng = signed(service, "GET", "/backup-bucket/file.bin",
                 extra_headers={"Range": "bytes=8190-8192"})
    assert rng.status_code == 206 and rng.content == payload[-3:]
    listed = signed(service, "GET", "/backup-bucket?list-type=2&prefix=file")
    assert listed.status_code == 200 and b"file.bin" in listed.content
    assert signed(service, "DELETE", "/backup-bucket/file.bin").status_code == 204
    missing = signed(service, "GET", "/backup-bucket/file.bin")
    assert missing.status_code == 404 and b"NoSuchKey" in missing.content
    # Physical Telegram parts are deliberately retained for future GC.
    assert telegram.uploads == 1


def test_signature_required_tampering_rejected_before_upload(service):
    client, issued, bot, _, _ = service
    assert client.get("/").status_code == 403
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    wrong_key = signed(service, "GET", "/", secret="incorrect")
    assert wrong_key.status_code == 403
    assert b"SignatureDoesNotMatch" in wrong_key.content
    good = AWSRequest(method="PUT", url="http://localhost:9000/backup-bucket/tamper",
                      data=b"original",
                      headers={"x-amz-content-sha256": hashlib.sha256(b"original").hexdigest()})
    good.context["payload_signing_enabled"] = True
    S3SigV4Auth(Credentials(issued.access_key, issued.secret_key),
                "s3", "us-east-1").add_auth(good)
    before = bot.uploads
    modified = client.put("/backup-bucket/tamper", content=b"changed",
                          headers=dict(good.headers))
    assert modified.status_code == 400
    assert bot.uploads == before
    assert b"XAmzContentSHA256Mismatch" in modified.content


def test_revoke_and_scope_are_enforced_independently(service):
    _, issued, _, connection, key = service
    assert signed(service, "GET", "/").status_code == 200
    db = connection()
    try:
        restricted = S3CredentialStore(db, key).create("reader", ["s3:GetObject"])
        assert S3CredentialStore(db, key).revoke(issued.id)
    finally:
        db.close()
    assert signed(service, "GET", "/").status_code == 403
    assert signed(service, "PUT", "/new-bucket", access=restricted.access_key,
                  secret=restricted.secret_key).status_code == 403


def test_unsigned_payload_and_unsupported_operations_fail_closed(service):
    client, _, bot, _, _ = service
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    # Botocore's S3SigV4Auth always replaces UNSIGNED-PAYLOAD with a
    # SHA-256 for PUT. Mutate the actual signed wire request to verify the
    # gateway rejects the unsigned upload before touching Telegram.
    req = AWSRequest(method="PUT", url="http://localhost:9000/backup-bucket/unsafe",
                     data=b"stuff", headers={
                         "x-amz-content-sha256": hashlib.sha256(b"stuff").hexdigest(),
                     })
    req.context["payload_signing_enabled"] = True
    S3SigV4Auth(Credentials(service[1].access_key, service[1].secret_key),
                "s3", "us-east-1").add_auth(req)
    bad_headers = dict(req.headers)
    bad_headers["X-Amz-Content-SHA256"] = "UNSIGNED-PAYLOAD"
    response = client.put("/backup-bucket/unsafe", content=b"stuff", headers=bad_headers)
    assert response.status_code == 400
    assert bot.uploads == 0
    unsupported = signed(service, "POST", "/backup-bucket/object?uploads")
    assert unsupported.status_code == 501
    assert b"NotImplemented" in unsupported.content


def test_catalog_pagination_with_signed_s3_queries(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    for name in ("a.txt", "b.txt", "c.txt"):
        assert signed(service, "PUT", f"/backup-bucket/{name}", name.encode()).status_code == 200
    first = signed(service, "GET", "/backup-bucket?list-type=2&max-keys=2")
    assert first.status_code == 200
    import xml.etree.ElementTree as ET
    root = ET.fromstring(first.content)
    tag = "{http://s3.amazonaws.com/doc/2006-03-01/}"
    assert root.findtext(tag+"IsTruncated") == "true"
    token = root.findtext(tag+"NextContinuationToken")
    second = signed(service, "GET",
                    "/backup-bucket?list-type=2&max-keys=2&continuation-token="+quote(token))
    assert second.status_code == 200
    keys = [item.findtext(tag+"Key") for item in ET.fromstring(second.content).findall(tag+"Contents")]
    assert keys == ["c.txt"]


def test_real_boto3_sdk_over_http_loopback(service):
    """Exercise an actual boto3 S3 client rather than only hand-built requests."""
    import socket
    import threading
    import time
    import boto3
    import uvicorn
    from botocore.config import Config

    _, issued, bot, _, _ = service
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(16)
    settings = uvicorn.Config(gateway.app, host="127.0.0.1", port=port,
                              log_level="error", lifespan="off")
    server = uvicorn.Server(settings)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started, "ASGI S3 server did not start"
        sdk = boto3.client(
            "s3", endpoint_url=f"http://127.0.0.1:{port}",
            aws_access_key_id=issued.access_key, aws_secret_access_key=issued.secret_key,
            region_name="us-east-1",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path", "payload_signing_enabled": True},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"max_attempts": 0},
            ),
        )
        assert sdk.create_bucket(Bucket="sdk-bucket")["ResponseMetadata"]["HTTPStatusCode"] == 200
        assert any(b["Name"] == "sdk-bucket" for b in sdk.list_buckets()["Buckets"])
        body = os.urandom(6003)
        put_result = sdk.put_object(Bucket="sdk-bucket", Key="nested/data.bin", Body=body)
        assert put_result["ResponseMetadata"]["HTTPStatusCode"] == 200
        assert sdk.head_object(Bucket="sdk-bucket", Key="nested/data.bin")["ContentLength"] == len(body)
        read = sdk.get_object(Bucket="sdk-bucket", Key="nested/data.bin")["Body"].read()
        assert read == body
        keys = [item["Key"] for item in sdk.list_objects_v2(
            Bucket="sdk-bucket", Prefix="nested/"
        )["Contents"]]
        assert keys == ["nested/data.bin"]
        sdk.delete_object(Bucket="sdk-bucket", Key="nested/data.bin")
        assert bot.uploads >= 1 and bot.downloads >= 1
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive(), "ASGI S3 server did not stop"
