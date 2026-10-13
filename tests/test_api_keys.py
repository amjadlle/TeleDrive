"""API key integration tests against persisted SQLite; no bearer secrets leaked."""
from __future__ import annotations

import time

import pytest

from teledrive.api_keys import ApiKeyStore
from teledrive.db import connect_database


@pytest.fixture
def keys(tmp_path):
    connection = connect_database(str(tmp_path / "auth.db"))
    try:
        yield ApiKeyStore(connection)
    finally:
        connection.close()


def test_credentials_are_hashed_never_listed_and_persisted(keys):
    created = keys.create("Backup client", ["objects:write", "objects:read"], now=100)
    assert created.value.startswith("td_live_")
    assert len(created.value) >= 40
    row = keys.list()[0]
    assert row["name"] == "Backup client"
    assert row["prefix"] == created.value[:16]
    assert "value" not in row and "digest" not in row
    assert created.value not in str(keys.list())
    assert keys.authorize(created.value, "objects:read", now=101)
    assert keys.authorize(created.value, "objects:write", now=102)
    assert not keys.authorize(created.value, "objects:delete", now=103)
    assert keys.list()[0]["last_used_at"] == 102


def test_read_only_and_wrong_key_do_not_escalate(keys):
    key = keys.create("read", ["objects:read"], now=100)
    assert not keys.authorize(key.value, "objects:write", now=101)
    assert not keys.authorize(key.value + "x", "objects:read", now=101)
    assert not keys.authorize("not-a-token", "objects:read", now=101)
    assert keys.authorize(key.value, "objects:read", now=101)


def test_expiry_and_revoke_fence_future_requests(keys):
    token = keys.create("short", ["objects:read"], now=100, expires_at=150)
    assert keys.authorize(token.value, "objects:read", now=149)
    assert not keys.authorize(token.value, "objects:read", now=150)
    assert keys.revoke(token.id)
    assert not keys.revoke(token.id)
    assert not keys.authorize(token.value, "objects:read", now=101)


def test_rotation_invalidates_old_token_and_preserves_policy(keys):
    previous = keys.create("bot", ["buckets:manage"], now=100, expires_at=500)
    rotated = keys.rotate(previous.id, now=200)
    assert rotated.id != previous.id and rotated.value != previous.value
    assert keys.list()[0]["name"] == "bot"
    assert not keys.authorize(previous.value, "buckets:manage", now=201)
    assert keys.authorize(rotated.value, "buckets:manage", now=201)
    assert not keys.authorize(rotated.value, "objects:read", now=201)
    with pytest.raises(FileNotFoundError):
        keys.rotate(previous.id, now=202)


def test_input_validation_rejects_bad_scopes_and_lifetime(keys):
    with pytest.raises(ValueError):
        keys.create("empty", [], now=100)
    with pytest.raises(ValueError):
        keys.create("invalid", ["root"], now=100)
    with pytest.raises(ValueError):
        keys.create("mixed", ["admin:*", "objects:read"], now=100)
    with pytest.raises(ValueError):
        keys.create("", ["objects:read"], now=100)
    with pytest.raises(ValueError):
        keys.create("expired", ["objects:read"], now=100, expires_at=100)
    with pytest.raises(ValueError):
        keys.create("too long", ["objects:read"], now=100, expires_at=100 + 11 * 365 * 86400)
    assert keys.list() == []


def test_admin_wildcard_and_scope_distinctions(keys):
    key = keys.create("admin service", ["admin:*"], now=100)
    for scope in ["buckets:manage", "objects:delete", "keys:manage"]:
        assert keys.authorize(key.value, scope, now=110)
    assert not keys.authorize("", "objects:read", now=110)
