"""Operator health and durable worker schedule require explicit opt-in."""
from __future__ import annotations

import time

from test_s3_admin import admin
from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.jobs import StorageUploadJobs
from teledrive.storage.manifest import ManifestStore


def test_operator_storage_health_is_authenticated_and_fail_safe(admin, tmp_path):
    client, path, _ = admin
    root = tmp_path / "allowed"
    root.mkdir()
    source = root / "file.bin"
    source.write_bytes(b"operator-health")
    db = connect_database(path)
    try:
        cat = BucketCatalog(ManifestStore(db))
        cat.create_bucket("health-ci", int(time.time()))
        queue = StorageUploadJobs(cat)
        identifier = queue.enqueue(
            "health-ci", "file.bin", source, allowed_root=root,
        )
    finally:
        db.close()

    endpoint = "/api/v1/admin/storage/health"
    assert client.get(endpoint).status_code in (401, 403)
    response = client.get(endpoint, auth=("testadmin", "secure-auth-password"))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["job_states"]["queued"] >= 1
    assert data["unresolved_alert_count"] == 0
    assert data["alerts"] == []
    assert data["stale_pending_sample_count"] == 0
    assert data["telegram_messages_retained"] == 0
    assert data["physical_deletion_enabled"] is False
    assert "lease_token" not in response.text
    assert "secret_key" not in response.text
    assert identifier not in response.text


def test_operator_health_alerts_failed_jobs_and_stale_pending_without_secrets(admin, tmp_path):
    client, path, _ = admin
    now = int(time.time())
    allowed = tmp_path / "alerts"
    allowed.mkdir()
    source = allowed / "payload.bin"
    source.write_bytes(b"metadata-only")
    connection = connect_database(path)
    try:
        catalog = BucketCatalog(ManifestStore(connection))
        catalog.create_bucket("alert-ci", now)
        queue = StorageUploadJobs(catalog)
        ident = queue.enqueue("alert-ci", "failure.bin", source, allowed_root=allowed, now=now)
        worker = queue.claim_next(now=now)
        assert worker and worker["id"] == ident
        assert queue.fail(ident, worker["lease_token"], "unrecoverable", now=now)
        stale = catalog.manifests.begin("alert-ci", "pending.bin", 0, now - 7300)
        catalog.manifests.start_writer(stale, "private-writer-token", now-7300, ttl=120)
    finally:
        connection.close()
    denied = client.get("/api/v1/admin/storage/health")
    assert denied.status_code in (401, 403)
    response = client.get("/api/v1/admin/storage/health",
                          auth=("testadmin", "secure-auth-password"))
    assert response.status_code == 200, response.text
    payload = response.json()
    by_code = {entry["code"]: entry for entry in payload["alerts"]}
    assert by_code["failed_upload_jobs"]["count"] == 1
    assert by_code["stale_pending_uploads"]["count"] == 1
    assert payload["unresolved_alert_count"] == len(by_code)
    assert "private-writer-token" not in response.text
    assert ident not in response.text
    assert stale not in response.text
    assert payload["physical_deletion_enabled"] is False
