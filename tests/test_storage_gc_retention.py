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



def test_gc_preview_fences_unresolved_send_and_unjournaled_bot_receipt(admin):
    """Unjournaled real Bot messages must never become apparent GC candidates."""
    from teledrive.storage.bot_api_adapter import BotFileIndex

    _, url, _ = admin
    now = int(time.time())
    old = now - 12 * 86400
    db = connect_database(url)
    try:
        manifest = ManifestStore(db)
        cat = BucketCatalog(manifest)
        cat.create_bucket("bot-gc-receipt", now)
        first = manifest.begin("bot-gc-receipt", "lost-ack", 4, old)
        second = manifest.begin("bot-gc-receipt", "received-ack", 4, old)
        third = manifest.begin("bot-gc-receipt", "pure-failed", 4, old)
        for ident in (first, second, third):
            manifest.fail(ident, "interrupted send", old)

        index = BotFileIndex(db)
        digest = sha256(b"data").hexdigest()
        index.start_send("bot-gc", -100888777666555, first, 0, digest, 4)
        index.start_send("bot-gc", -100888777666555, second, 0, digest, 4)
        index.put_receipt(
            "bot-gc", -100888777666555, 42, "owned-test-file-id",
            object_id=second, ordinal=0, checksum=digest, size=4,
            require_intent=True,
        )
        inventory = StorageGCInventory(manifest).preview_failed_retention(
            now=now,
        )
        by_id = {candidate["object_id"]: candidate for candidate in inventory}
        assert set(by_id) == {first, second, third}
        assert by_id[first]["unresolved_bot_intents"] == 1
        assert by_id[first]["durable_bot_receipts"] == 0
        assert by_id[first]["reference_blocked"] is True
        assert by_id[second]["unresolved_bot_intents"] == 0
        assert by_id[second]["durable_bot_receipts"] == 1
        assert by_id[second]["reference_blocked"] is True
        assert by_id[third]["unresolved_bot_intents"] == 0
        assert by_id[third]["durable_bot_receipts"] == 0
        assert by_id[third]["reference_blocked"] is False
        assert all(not candidate["approved_for_deletion"]
                   for candidate in inventory)
        assert all(candidate["remote_history_scan_required"]
                   for candidate in inventory)
    finally:
        db.close()


def test_gc_preview_fails_closed_on_partially_migrated_bot_receipt_schema(admin):
    """Missing half of a Bot receipt schema cannot be interpreted as no ACK."""
    from teledrive.storage.bot_api_adapter import BotFileIndex

    _, url, _ = admin
    now = int(time.time())
    old = now - 9 * 86400
    db = connect_database(url)
    try:
        manifests = ManifestStore(db)
        cat = BucketCatalog(manifests)
        cat.create_bucket("incomplete-gc-schema", now)
        ident = manifests.begin("incomplete-gc-schema", "failed", 1, old)
        manifests.fail(ident, "old interruption", old)
        BotFileIndex(db)
        db.connection.exec_driver_sql(
            "DROP TABLE storage_bot_upload_receipts"
        )
        db.commit()
        import pytest
        with pytest.raises(RuntimeError, match="Bot reconciliation metadata incomplete"):
            StorageGCInventory(manifests).preview_failed_retention(now=now)
    finally:
        db.close()
