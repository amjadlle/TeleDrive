"""End-to-end HTTP authorization and catalog tests using a real SQLite file."""
from __future__ import annotations

import hashlib
import time

import pytest
from fastapi.testclient import TestClient

from teledrive.db import connect_database
from teledrive.storage.manifest import ManifestStore
from teledrive.storage.catalog import BucketCatalog
import web.app as webapp


@pytest.fixture()
def api(tmp_path, monkeypatch):
    path = tmp_path / "rest-api.sqlite"
    monkeypatch.setattr(webapp, "db_connect", lambda: connect_database(str(path)))
    monkeypatch.setattr(webapp, "WEB_USER", "ci-admin")
    monkeypatch.setattr(webapp, "WEB_PASSWORD", "ci-local-password-1234")
    webapp.AUTH_FAILURES.clear()
    client = TestClient(webapp.app, headers={"host": "localhost"})
    yield client, path
    client.close()


def admin_headers():
    return {"x-teledrive-csrf": "1"}


def create_key(client, scopes):
    res = client.post("/api/v1/admin/keys",
        auth=("ci-admin", "ci-local-password-1234"),
        headers=admin_headers(), json={"name": "integration", "scopes": scopes})
    assert res.status_code == 201, res.text
    return res.json()


def test_admin_endpoints_require_basic_and_csrf(api):
    client, _ = api
    assert client.get("/api/v1/admin/keys").status_code == 401
    assert client.post("/api/v1/admin/keys",
        auth=("ci-admin", "ci-local-password-1234"),
        json={"name": "x", "scopes": ["objects:read"]}).status_code == 403
    assert client.post("/api/v1/admin/keys",
        auth=("ci-admin", "ci-local-password-1234"),
        headers={"x-teledrive-csrf": "1", "origin": "http://evil.example"},
        json={"name": "x", "scopes": ["objects:read"]}).status_code == 403


def test_key_rotation_revoke_and_metadata_never_discloses_secrets(api):
    client, _ = api
    old = create_key(client, ["buckets:read"])
    response = client.get("/api/v1/admin/keys",
                          auth=("ci-admin", "ci-local-password-1234"))
    assert response.status_code == 200
    assert old["token"] not in response.text
    assert "digest" not in response.text
    assert client.get("/api/v1/buckets",
                      headers={"Authorization": f"Bearer {old['token']}"}).status_code == 200
    rotate = client.post(f"/api/v1/admin/keys/{old['id']}/rotate",
        auth=("ci-admin", "ci-local-password-1234"), headers=admin_headers())
    assert rotate.status_code == 200
    new = rotate.json()
    assert new["token"] != old["token"]
    assert client.get("/api/v1/buckets",
        headers={"Authorization": f"Bearer {old['token']}"}).status_code == 403
    assert client.get("/api/v1/buckets",
        headers={"Authorization": f"Bearer {new['token']}"}).status_code == 200
    revoked = client.delete(f"/api/v1/admin/keys/{new['id']}",
        auth=("ci-admin", "ci-local-password-1234"), headers=admin_headers())
    assert revoked.status_code == 200
    assert client.get("/api/v1/buckets",
        headers={"Authorization": f"Bearer {new['token']}"}).status_code == 403


def test_read_only_bearer_cannot_create_or_delete_bucket(api):
    client, _ = api
    key = create_key(client, ["buckets:read"])
    headers = {"Authorization": f"Bearer {key['token']}"}
    assert client.get("/api/v1/buckets", headers=headers).status_code == 200
    assert client.post("/api/v1/buckets", headers=headers,
                       json={"name": "backups"}).status_code == 403
    assert client.delete("/api/v1/buckets/backups", headers=headers).status_code == 403
    assert client.get("/api/v1/buckets").status_code == 401


def test_bucket_crud_and_object_metadata_scopes(api):
    client, path = api
    key = create_key(client, ["buckets:read", "buckets:manage", "objects:read", "objects:delete"])
    headers = {"Authorization": f"Bearer {key['token']}"}
    assert client.post("/api/v1/buckets", json={"name": "backups"}, headers=headers).status_code == 201
    assert client.get("/api/v1/buckets", headers=headers).json()["buckets"] == ["backups"]
    assert client.post("/api/v1/buckets", json={"name": "backups"}, headers=headers).status_code == 409

    db = connect_database(str(path))
    try:
        manifests = ManifestStore(db)
        identifier = manifests.begin("backups", "📁/archive.bin", 0, int(time.time()))
        manifests.finish(identifier, hashlib.sha256(b"").hexdigest(), int(time.time()))
        cat = BucketCatalog(manifests)
        cat.publish(identifier, now=int(time.time()))
    finally:
        db.close()

    result = client.get("/api/v1/buckets/backups/objects", headers=headers).json()
    assert [x["key"] for x in result["objects"]] == ["📁/archive.bin"]
    assert result["objects"][0]["sha256"] == hashlib.sha256(b"").hexdigest()
    assert client.delete("/api/v1/buckets/backups", headers=headers).status_code == 409
    unlink = client.delete("/api/v1/buckets/backups/objects",
                           params={"key": "📁/archive.bin", "generation": 1}, headers=headers)
    assert unlink.status_code == 200 and unlink.json()["unlinked"]
    assert client.get("/api/v1/buckets/backups/objects", headers=headers).json()["objects"] == []
    # Historical Telegram data is deliberately not destroyed by unlink.
    assert client.delete("/api/v1/buckets/backups", headers=headers).status_code == 409


def test_bearer_token_does_not_unlock_basic_admin(api):
    client, _ = api
    key = create_key(client, ["admin:*"])
    token = {"Authorization": f"Bearer {key['token']}"}
    assert client.get("/api/v1/admin/keys", headers=token).status_code == 401
    assert client.post("/api/v1/admin/keys", headers=token,
                       json={"name": "escalation", "scopes": ["admin:*"]}).status_code == 401


def test_openapi_documents_rest_key_and_bucket_routes(api):
    client, _ = api
    response = client.get("/openapi.json", auth=("ci-admin", "ci-local-password-1234"))
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/admin/keys" in paths
    assert "/api/v1/buckets" in paths
    assert "/api/v1/buckets/{bucket}/objects" in paths
    assert "HTTPBearer" in response.json()["components"]["securitySchemes"]
