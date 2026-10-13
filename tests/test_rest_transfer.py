"""HTTP binary transfer contract: actual SQLite metadata and fake Telegram Bot bytes."""
from __future__ import annotations

import hashlib
import os

import pytest
from fastapi.testclient import TestClient

import web.app as webapp
PRODUCTION_BOT_TRANSPORT = webapp.storage_bot_transport
from teledrive.db import connect_database
from teledrive.storage.bot_api_adapter import BotApiChunkTransport, BotFileIndex


class FakeTelegramBot:
    def __init__(self):
        self.parts = {}
        self.counter = 0

    async def call(self, method, fields, source=None):
        if method == "getMe":
            return {"id": 99}
        if method == "getChat":
            return {"id": -1001234567890, "type": "channel"}
        if method == "getChatMember":
            return {"status": "administrator", "can_post_messages": True}
        if method == "sendDocument":
            self.counter += 1
            identifier = f"test-part-{self.counter}"
            self.parts[identifier] = source.read_bytes()
            return {"message_id": self.counter, "chat": {"id": -1001234567890},
                    "document": {"file_id": identifier}}
        if method == "getFile":
            identifier = fields["file_id"]
            return {"file_path": identifier, "file_size": len(self.parts[identifier])}
        raise AssertionError(method)

    async def download_file(self, path, destination):
        destination.write_bytes(self.parts[path])


@pytest.fixture
def service(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    bot = FakeTelegramBot()
    monkeypatch.setattr(webapp, "db_connect", lambda: connect_database(str(path)))
    monkeypatch.setattr(
        webapp, "storage_bot_transport",
        lambda db: BotApiChunkTransport(bot, BotFileIndex(db),
                                         chat_id=-1001234567890, account="api-bot"),
    )
    monkeypatch.setenv("TELEDRIVE_STORAGE_SCRATCH_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("TELEDRIVE_REST_MAX_UPLOAD_BYTES", str(4 * 1024**2))
    monkeypatch.setattr(webapp, "WEB_USER", "admin-test")
    monkeypatch.setattr(webapp, "WEB_PASSWORD", "some-long-password")
    webapp.AUTH_FAILURES.clear()
    client = TestClient(webapp.app, headers={"host": "localhost"})
    try:
        response = client.post("/api/v1/admin/keys",
            auth=("admin-test", "some-long-password"),
            headers={"x-teledrive-csrf": "1"}, json={
                "name": "storage-integration", "scopes": [
                    "buckets:read", "buckets:manage", "objects:read",
                    "objects:write", "objects:delete",
                ],
            })
        assert response.status_code == 201, response.text
        token = response.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        res = client.post("/api/v1/buckets", headers=headers, json={"name": "backup-bucket"})
        assert res.status_code == 201, res.text
        yield client, headers, bot, tmp_path
    finally:
        client.close()


def put(client, headers, payload, key="data.bin"):
    return client.put("/api/v1/buckets/backup-bucket/content",
                      params={"key": key}, headers={
                          **headers, "Content-Type": "application/octet-stream"
                      }, content=payload)


@pytest.mark.parametrize("data", [b"", b"x", os.urandom(4097), os.urandom(1049600)])
def test_binary_round_trip_persists_bytes_not_in_sql(service, data):
    client, headers, bot, root = service
    uploaded = put(client, headers, data)
    assert uploaded.status_code == 201, uploaded.text
    identifier = uploaded.json()["id"]
    assert uploaded.json()["sha256"] == hashlib.sha256(data).hexdigest()
    assert uploaded.json()["size"] == len(data)
    result = client.get("/api/v1/buckets/backup-bucket/content",
                        params={"key": "data.bin"}, headers=headers)
    assert result.status_code == 200, result.text
    assert result.content == data
    assert result.headers["etag"] == '"' + hashlib.sha256(data).hexdigest() + '"'
    assert result.headers["x-teledrive-object-id"] == identifier
    assert not list((root / "scratch").glob("api-upload-*"))
    # SQLite stores manifests/file_ids, never the file bytes.
    if len(data) > 100:
        assert data[:100] not in (root / "state.db").read_bytes()


def test_http_range_suffix_and_unsatisfiable_ranges(service):
    client, headers, _, _ = service
    data = b"0123456789abcdef"
    assert put(client, headers, data).status_code == 201
    for range_header, expected in [
        ("bytes=3-8", b"345678"),
        ("bytes=7-", b"789abcdef"),
        ("bytes=-4", b"cdef"),
    ]:
        result = client.get("/api/v1/buckets/backup-bucket/content",
            params={"key": "data.bin"}, headers={**headers, "Range": range_header})
        assert result.status_code == 206
        assert result.content == expected
        assert result.headers["content-length"] == str(len(expected))
    for invalid in ["bytes=50-60", "bytes=8-3", "bytes=0-1,4-6", "units=0-1"]:
        result = client.get("/api/v1/buckets/backup-bucket/content",
            params={"key": "data.bin"}, headers={**headers, "Range": invalid})
        assert result.status_code == 416


def test_auth_and_upload_policy_enforced_before_telegram_send(service):
    client, headers, bot, _ = service
    args = {"key": "denied"}
    url = "/api/v1/buckets/backup-bucket/content"
    assert client.put(url, params=args, headers={"Content-Type": "application/octet-stream"},
                      content=b"secret").status_code == 401
    assert client.put(url, params=args, headers=headers, content=b"secret").status_code == 415
    assert client.put(url, params=args, headers={
        **headers, "Content-Type": "application/octet-stream",
    }, content=b"z" * (4 * 1024**2 + 1)).status_code == 413
    assert bot.counter == 0


def test_missing_key_is_404_and_overwrite_is_versioned(service):
    client, headers, bot, _ = service
    assert client.get("/api/v1/buckets/backup-bucket/content",
                      params={"key": "absent"}, headers=headers).status_code == 404
    one = put(client, headers, b"one")
    two = put(client, headers, b"two")
    assert one.status_code == 201 and two.status_code == 201
    assert one.json()["id"] != two.json()["id"]
    assert two.json()["generation"] == 2
    result = client.get("/api/v1/buckets/backup-bucket/content",
                        params={"key": "data.bin"}, headers=headers)
    assert result.content == b"two"
    uploads_before_conflict = bot.counter
    conflicting = client.put("/api/v1/buckets/backup-bucket/content",
        params={"key": "data.bin", "generation": 1},
        headers={**headers, "Content-Type": "application/octet-stream"}, content=b"three")
    assert conflicting.status_code == 409
    assert bot.counter == uploads_before_conflict  # zero Telegram writes for a stale generation


def test_missing_bot_credentials_returns_503_before_upload(service, monkeypatch):
    client, headers, bot, _ = service
    # The service fixture temporarily patches the transport. Restore the
    # production factory and remove its required Telegram configuration.
    monkeypatch.setattr(webapp, "storage_bot_transport", PRODUCTION_BOT_TRANSPORT)
    monkeypatch.delenv("TELEDRIVE_STORAGE_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEDRIVE_STORAGE_BOT_CHAT_ID", raising=False)
    result = put(client, headers, b"secret", key="unconfigured")
    assert result.status_code == 503
    assert bot.counter == 0


def test_head_returns_actual_object_metadata_without_telegram_download(service):
    client, headers, bot, _ = service
    body = os.urandom(257)
    uploaded = put(client, headers, body, key="head-check.bin")
    assert uploaded.status_code == 201
    calls_before = bot.counter
    result = client.head(
        "/api/v1/buckets/backup-bucket/content",
        params={"key": "head-check.bin"}, headers=headers,
    )
    assert result.status_code == 200
    assert result.content == b""
    assert result.headers["content-length"] == str(len(body))
    assert result.headers["etag"] == '"' + hashlib.sha256(body).hexdigest() + '"'
    assert result.headers["x-teledrive-object-id"] == uploaded.json()["id"]
    assert bot.counter == calls_before

    absent = client.head(
        "/api/v1/buckets/backup-bucket/content",
        params={"key": "does-not-exist"}, headers=headers,
    )
    assert absent.status_code == 404

    forbidden = client.head(
        "/api/v1/buckets/backup-bucket/content",
        params={"key": "head-check.bin"},
    )
    assert forbidden.status_code == 401
