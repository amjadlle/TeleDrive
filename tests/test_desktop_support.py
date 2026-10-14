"""Desktop regression tests for account isolation, auth handoff and vendor-neutral DB."""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

from teledrive import uploader
from teledrive import db
from teledrive.desktop_support import (
    accounts_from_config, parse_auth_prompt, validate_account_rows, write_auth_response,
)


@pytest.mark.parametrize("line,expected", [
    ("Please enter the code for account 'primary':", ("primary", "code")),
    ("Please enter the password for account 'backup-2':", ("backup-2", "password")),
    ("unrelated uploader output", None),
    ("Please enter the code for account 'bad\\nname':", None),
])
def test_prompt_parser(line, expected):
    assert parse_auth_prompt(line) == expected


def test_auth_handoff_is_scoped_and_private(tmp_path):
    auth = tmp_path / "auth"
    first = write_auth_response(auth, "primary", "code", "12345")
    second = write_auth_response(auth, "backup", "password", "a very secret password")
    assert first.name == "primary_code.txt"
    assert second.name == "backup_password.txt"
    assert first.read_text() == "12345"
    assert second.read_text() == "a very secret password"
    assert uploader.wait_for_auth_value(str(auth), "code", timeout=1, account_name="primary") == "12345"
    assert not first.exists()
    assert second.exists()
    if os.name != "nt":
        assert stat.S_IMODE(second.stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        write_auth_response(auth, "../hijack", "code", "12345")
    with pytest.raises(ValueError):
        write_auth_response(auth, "primary", "evil", "12345")


def test_legacy_account_is_migrated():
    account = accounts_from_config({
        "api_id": 123, "api_hash": "hash", "phone": "+90000",
        "target": "me", "session_path": "data/original",
    })
    assert len(account) == 1
    assert account[0]["name"] == "default"
    assert account[0]["phone"] == "+90000"
    assert account[0]["session_path"] == "data/original"


def test_accounts_require_unique_names_and_sessions():
    rows = [
        {"name": "main", "enabled": True, "session_path": "data/main"},
        {"name": "backup", "enabled": True, "session_path": "data/backup"},
    ]
    validate_account_rows(rows, 123, "hash")
    with pytest.raises(ValueError, match="Duplicate account"):
        validate_account_rows([rows[0], {**rows[1], "name": "main"}], 123, "hash")
    with pytest.raises(ValueError, match="unique session"):
        validate_account_rows([rows[0], {**rows[1], "session_path": "data/main"}], 123, "hash")
    with pytest.raises(ValueError, match="Enable"):
        validate_account_rows([{**rows[0], "enabled": False}], 123, "hash")
    with pytest.raises(ValueError, match="API"):
        validate_account_rows(rows, 0, "")


def test_sqlalchemy_queue_history_and_account_states(tmp_path):
    path = tmp_path / "state.sqlite"
    connection = db.connect_database(str(path))
    try:
        connection.insert_pending(str(tmp_path / "a.txt"), 30, 1.0, 100)
        connection.insert_pending(str(tmp_path / "b.txt"), 40, 1.0, 100)
        connection.commit()
        connection.mark_uploaded(str(tmp_path / "a.txt"), 42, "backup", 200)
        connection.set_account_state(
            "backup", "ready", last_error=None, cooldown_until=None,
            last_success_ts=200, now=200,
        )
        connection.commit()
        assert connection.dashboard_counts() == {"pending": 1, "uploaded": 1}
        assert connection.list_files("queue", 10)[0]["status"] == "pending"
        assert connection.list_files("history", 10)[0]["tg_account"] == "backup"
        assert connection.uploaded_counts_by_account()["backup"] == 1
        assert connection.all_account_states()["backup"]["state"] == "ready"
    finally:
        connection.close()


def test_external_sqlalchemy_url_is_not_replaced_by_sqlite(tmp_path):
    url = "postgresql+psycopg://user:secret@db-host/teledrive"
    assert db.normalize_database_url(url, config_dir=tmp_path) == url
