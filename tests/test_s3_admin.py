"""Admin S3 credential issuance contract and immediate revoke with signed S3 calls."""
from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

import web.app as webapp
import web.s3 as gateway
from teledrive.db import connect_database
from teledrive.s3.credentials import S3CredentialStore


@pytest.fixture
def admin(tmp_path, monkeypatch):
    url = str(tmp_path / "metadata.db")
    key = Fernet.generate_key()
    monkeypatch.setattr(webapp, "db_connect", lambda: connect_database(url))
    monkeypatch.setattr(gateway, "db_connect", lambda: connect_database(url))
    monkeypatch.setattr(webapp, "load_master_key", lambda: key)
    monkeypatch.setattr(gateway, "load_master_key", lambda: key)
    monkeypatch.setattr(webapp, "WEB_USER", "testadmin")
    monkeypatch.setattr(webapp, "WEB_PASSWORD", "secure-auth-password")
    webapp.AUTH_FAILURES.clear()
    # No ASGI lifespan: endpoint authorization contract is isolated from
    # Docker startup paths (/config, /data) and Telegram scheduler.
    client = TestClient(webapp.app, base_url="http://localhost")
    try:
        yield client, url, key
    finally:
        client.close()


def test_issuance_requires_basic_auth_csrf_and_one_time_secret(admin):
    client, url, master = admin
    path = "/api/v1/admin/s3-credentials"
    payload = {"name": "SDK-backup", "permissions": ["s3:GetObject", "s3:ListBucket"]}
    assert client.post(path, json=payload).status_code == 403 or client.post(path, json=payload).status_code == 401
    assert client.post(path, auth=("testadmin", "secure-auth-password"),
                       json=payload).status_code == 403
    issued = client.post(path, auth=("testadmin", "secure-auth-password"),
                         headers={"x-teledrive-csrf": "1"}, json=payload)
    assert issued.status_code == 201, issued.text
    result = issued.json()
    assert result["secret_key"]
    assert result["access_key"].startswith("TD")
    listing = client.get(path, auth=("testadmin", "secure-auth-password"))
    assert listing.status_code == 200
    rows = listing.json()["credentials"]
    assert rows[0]["access_key"] == result["access_key"]
    assert result["secret_key"] not in listing.text
    connection = connect_database(url)
    try:
        assert S3CredentialStore(connection, master).resolve(
            result["access_key"], "s3:GetObject"
        ) == result["secret_key"]
    finally:
        connection.close()
    revoke_path = f"{path}/{result['id']}"
    assert client.delete(revoke_path, auth=("testadmin", "secure-auth-password")).status_code == 403
    assert client.delete(revoke_path, auth=("testadmin", "secure-auth-password"),
                         headers={"x-teledrive-csrf": "1"}).status_code == 200
    connection = connect_database(url)
    try:
        assert S3CredentialStore(connection, master).resolve(
            result["access_key"], "s3:GetObject"
        ) is None
    finally:
        connection.close()


def test_scope_validation_and_missing_master_key(admin, monkeypatch):
    client, _, _ = admin
    url = "/api/v1/admin/s3-credentials"
    auth = ("testadmin", "secure-auth-password")
    headers = {"x-teledrive-csrf": "1"}
    invalid = client.post(url, auth=auth, headers=headers,
                          json={"name": "bad", "permissions": ["s3:AdminAll"]})
    assert invalid.status_code == 422
    monkeypatch.setattr(webapp, "load_master_key", lambda: (_ for _ in ()).throw(
        RuntimeError("Missing S3 master key")
    ))
    missing = client.get(url, auth=auth)
    assert missing.status_code == 503
    assert "Missing S3 master key" in missing.text
