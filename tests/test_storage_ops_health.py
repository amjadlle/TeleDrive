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
    assert data["stale_pending_sample_count"] == 0
    assert data["telegram_messages_retained"] == 0
    assert data["physical_deletion_enabled"] is False
    assert "lease_token" not in response.text
    assert "secret_key" not in response.text
    assert identifier not in response.text
