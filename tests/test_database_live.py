"""Live database integration tests; skipped unless CI supplies an external DB URL."""
from __future__ import annotations

import os
import time

import pytest

from teledrive.db import connect_database


@pytest.mark.parametrize("env_name", ["DB_TEST_POSTGRES_URL", "DB_TEST_MYSQL_URL"])
def test_external_db_schema_queue_lease_and_account_history(env_name):
    url = os.getenv(env_name)
    if not url:
        pytest.skip(f"{env_name} not configured")
    first = connect_database(url)
    second = connect_database(url)
    now = int(time.time())
    token = f"ci-{env_name}-{now}"
    path = f"/uploads/{token}.bin"
    try:
        first.insert_pending(path, 123, 1.0, now)
        first.commit()

        assert first.claim_file(path, token + "-a", now, 3600)
        assert not second.claim_file(path, token + "-b", now, 3600)

        first.mark_uploaded(path, 99, "primary", now)
        first.set_account_state(
            "primary", "ready", last_error=None,
            cooldown_until=None, last_success_ts=now, now=now,
        )
        first.commit()

        history = first.list_files("history", 200)
        uploaded = next(row for row in history if row["path"] == path)
        assert uploaded["tg_account"] == "primary"
        assert uploaded["tg_message_id"] == 99
        assert first.uploaded_counts_by_account()["primary"] >= 1
        assert first.all_account_states()["primary"]["state"] == "ready"

        assert first.acquire_lease(token, "holder-a", now, 30)
        assert not second.acquire_lease(token, "holder-b", now, 30)
        assert first.renew_lease(token, "holder-a", now, 30)
        first.release_lease(token, "holder-a")
        assert second.acquire_lease(token, "holder-b", now, 30)
        second.release_lease(token, "holder-b")
    finally:
        first.close()
        second.close()
