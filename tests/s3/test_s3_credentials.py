"""S3 key lifecycle and signature abuse regression tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from teledrive.db import connect_database
from teledrive.s3.credentials import S3CredentialStore, credentials, load_master_key


def test_secret_is_encrypted_and_survives_database_restart(tmp_path):
    path = str(tmp_path / "state.db")
    master = Fernet.generate_key()
    db = connect_database(path)
    try:
        keys = S3CredentialStore(db, master)
        value = keys.create("sync", ["s3:GetObject"], now=1000)
        row = db.connection.execute(select(credentials.c.encrypted_secret)).scalar_one()
        assert value.secret_key not in row
        assert value.access_key not in row
        assert keys.resolve(value.access_key, "s3:GetObject", now=1001) == value.secret_key
        assert keys.resolve(value.access_key, "s3:DeleteObject", now=1001) is None
        assert "secret_key" not in str(keys.list())
        assert keys.revoke(value.id)
        assert not keys.revoke(value.id)
        assert keys.resolve(value.access_key, "s3:GetObject", now=1001) is None
    finally:
        db.close()
    reopened = connect_database(path)
    try:
        keys = S3CredentialStore(reopened, master)
        assert keys.resolve(value.access_key, "s3:GetObject", now=1001) is None
    finally:
        reopened.close()


def test_expiration_and_invalid_scopes(tmp_path):
    db = connect_database(str(tmp_path / "state.db"))
    try:
        keys = S3CredentialStore(db, Fernet.generate_key())
        with pytest.raises(ValueError):
            keys.create("bad", ["admin:*"])
        with pytest.raises(ValueError):
            keys.create("expired", ["s3:GetObject"], expires_at=2, now=2)
        issued = keys.create("temp", ["s3:GetObject"], expires_at=110, now=100)
        assert keys.resolve(issued.access_key, "s3:GetObject", now=109)
        assert keys.resolve(issued.access_key, "s3:GetObject", now=110) is None
    finally:
        db.close()


def test_master_key_file_and_fail_closed(monkeypatch, tmp_path):
    key = Fernet.generate_key()
    keyfile = tmp_path / "master"
    keyfile.write_bytes(key + b"\n")
    monkeypatch.setenv("TELEDRIVE_S3_MASTER_KEY_FILE", str(keyfile))
    assert load_master_key() == key
    keyfile.write_text("bad-key")
    with pytest.raises(RuntimeError):
        load_master_key()
    monkeypatch.delenv("TELEDRIVE_S3_MASTER_KEY_FILE")
    monkeypatch.delenv("TELEDRIVE_S3_MASTER_KEY", raising=False)
    with pytest.raises(RuntimeError):
        load_master_key()
