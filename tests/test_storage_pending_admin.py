"""P1 operator-safe PENDING recovery: dry-run, per-object SQL fencing and CSRF."""
from __future__ import annotations

import time

from sqlalchemy import select

from test_s3_admin import admin
from teledrive.db import connect_database
from teledrive.storage.manifest import ManifestStore, writer_fences


def create(url, *, age=7200, ttl=120, leased=True):
    now = int(time.time()) - age
    db = connect_database(url)
    try:
        store = ManifestStore(db)
        identifier = store.begin("backups", "object.bin", 0, now)
        if leased:
            store.start_writer(identifier, f"owner-{identifier}", now, ttl=ttl)
        return identifier, f"owner-{identifier}"
    finally:
        db.close()


def test_pending_recovery_is_authenticated_csrf_protected_and_dry_run(admin):
    client, url, _ = admin
    stale, token = create(url)
    live, live_token = create(url, age=10, ttl=300)
    legacy, _ = create(url, leased=False)
    uri = "/api/v1/admin/storage/pending-stale"
    auth = ("testadmin", "secure-auth-password")
    assert client.get(uri).status_code in (401, 403)
    result = client.get(uri, auth=auth)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["dry_run"] is True
    assert data["physical_deletion_enabled"] is False
    assert [item["id"] for item in data["uploads"]] == [stale]
    assert "token" not in result.text
    assert data["uploads"][0]["journaled_chunks"] == 0
    assert client.get(uri, auth=auth, params={"min_age_seconds": 10}).status_code == 422

    endpoint = f"{uri}/{stale}/abandon"
    assert client.post(endpoint, json={"execute": True}).status_code in (401, 403)
    assert client.post(endpoint, auth=auth, json={"execute": True}).status_code == 403
    headers = {"X-TeleDrive-CSRF": "1"}
    preview = client.post(endpoint, auth=auth, headers=headers, json={})
    assert preview.status_code == 200
    assert preview.json() == {
        "object_id": stale, "eligible": True, "abandoned": False, "dry_run": True,
    }
    db = connect_database(url)
    try:
        store = ManifestStore(db)
        assert dict(store.interrupted())[stale] == "pending"
        assert store.heartbeat_writer(stale, token, int(time.time())) is False
    finally:
        db.close()

    executed = client.post(endpoint, auth=auth, headers=headers,
                           json={"execute": True})
    assert executed.status_code == 200
    assert executed.json() == {
        "object_id": stale, "eligible": True, "abandoned": True, "dry_run": False,
    }
    retry = client.post(endpoint, auth=auth, headers=headers, json={"execute": True})
    assert retry.json()["abandoned"] is False
    db = connect_database(url)
    try:
        store = ManifestStore(db)
        assert dict(store.interrupted()) == {
            stale: "failed", live: "pending", legacy: "pending"
        }
        assert store.heartbeat_writer(live, live_token, int(time.time()))
        assert store.heartbeat_writer(stale, token, int(time.time())) is False
        assert len(store.stale_pending(now=int(time.time()), limit=100)) == 0
    finally:
        db.close()


def test_competing_heartbeat_survives_and_second_worker_cannot_reanimate(admin):
    client, url, _ = admin
    target, old = create(url)
    db = connect_database(url)
    try:
        store = ManifestStore(db)
        now = int(time.time())
        # Simulate a second worker renewing a stale lease before admin execute.
        db.connection.execute(
            writer_fences.update().where(writer_fences.c.object_id == target)
            .values(lease_until=now + 200, heartbeat_at=now)
        )
        db.commit()
    finally:
        db.close()

    endpoint = f"/api/v1/admin/storage/pending-stale/{target}/abandon"
    out = client.post(endpoint, json={"execute": True},
                      auth=("testadmin", "secure-auth-password"),
                      headers={"X-TeleDrive-CSRF": "1"})
    assert out.status_code == 200
    assert out.json()["abandoned"] is False
    db = connect_database(url)
    try:
        store = ManifestStore(db)
        assert dict(store.interrupted())[target] == "pending"
        assert store.heartbeat_writer(target, old, int(time.time()))
    finally:
        db.close()


def test_stale_pending_inventory_persists_across_connections(tmp_path):
    url = str(tmp_path / "durable.sqlite")
    identifier, old = create(url)
    first = connect_database(url)
    second = connect_database(url)
    try:
        left, right = ManifestStore(first), ManifestStore(second)
        now = int(time.time())
        assert [r["id"] for r in left.stale_pending(now=now)] == [identifier]
        first.commit()
        assert right.abandon_stale_pending(now=now, object_id=identifier) == [identifier]
        assert left.stale_pending(now=now) == []
        assert not left.heartbeat_writer(identifier, old, now)
    finally:
        first.close()
        second.close()
