"""Retention-aware GC preview never physically deletes Telegram media."""
from __future__ import annotations
import time
from hashlib import sha256
from test_s3_admin import admin
from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.gc import StorageGCInventory
from teledrive.storage.manifest import ManifestStore


def test_failed_age_plan_protects_live_pending_and_ready(admin):
    client, url, _ = admin
    now = int(time.time())
    old = now - 9 * 86400
    db = connect_database(url)
    try:
        cat = BucketCatalog(ManifestStore(db))
        cat.create_bucket("gc-retained", now)
        failed = cat.manifests.begin("gc-retained", "failed", 0, old)
        cat.manifests.fail(failed, "interrupted Telegram send", old)
        pending = cat.manifests.begin("gc-retained", "pending", 0, old)
        ready = cat.manifests.begin("gc-retained", "ready", 0, old)
        cat.manifests.finish(ready, sha256(b"").hexdigest(), old)
        cat.publish(ready, now=now)
        result = StorageGCInventory(cat.manifests).preview_failed_retention(now=now)
        assert [x["object_id"] for x in result] == [failed]
        assert not result[0]["approved_for_deletion"]
        assert result[0]["remote_history_scan_required"]
        assert not {pending, ready}.intersection(x["object_id"] for x in result)
    finally:
        db.close()
    endpoint = "/api/v1/admin/storage/gc-candidates"
    assert client.get(endpoint).status_code in (401, 403)
    assert client.get(endpoint, auth=("testadmin", "secure-auth-password"),
                      params={"min_age_seconds": 60}).status_code == 422
    response = client.get(endpoint, auth=("testadmin", "secure-auth-password"))
    assert response.status_code == 200, response.text
    assert response.json()["dry_run"] is True
    assert response.json()["physical_deletion_enabled"] is False
    assert response.json()["candidates"][0]["object_id"] == failed
