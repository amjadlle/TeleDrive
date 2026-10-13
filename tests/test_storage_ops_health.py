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


def test_operator_health_counts_unjournaled_bot_acks_without_exposing_ids(admin):
    """Only persisted ACKs absent from chunk journals produce a warning."""
    import hashlib
    from teledrive.storage.bot_api_adapter import BotFileIndex
    from teledrive.storage.manifest import ChunkReference

    client, path, _ = admin
    db = connect_database(path)
    try:
        catalog = BucketCatalog(ManifestStore(db))
        catalog.create_bucket("bot-ack-alert", int(time.time()))
        identifier = catalog.manifests.begin(
            "bot-ack-alert", "only-in-receipts.bin", 5, int(time.time()),
        )
        digest = hashlib.sha256(b"hello").hexdigest()
        idx = BotFileIndex(db)
        idx.put_receipt(
            "bot-ops-ci", -100987654321, 101, "private-bot-file-id",
            object_id=identifier, ordinal=0, checksum=digest, size=5,
        )
    finally:
        db.close()

    url = "/api/v1/admin/storage/health"
    response = client.get(url, auth=("testadmin", "secure-auth-password"))
    assert response.status_code == 200, response.text
    assert response.json()["unjournaled_bot_ack_count"] == 1
    alerts = {row["code"]: row for row in response.json()["alerts"]}
    assert alerts["unjournaled_bot_acks"]["count"] == 1
    assert "private-bot-file-id" not in response.text
    assert identifier not in response.text
    assert response.json()["physical_deletion_enabled"] is False

    db = connect_database(path)
    try:
        catalog = BucketCatalog(ManifestStore(db))
        catalog.manifests.append(
            identifier, ChunkReference(
                ordinal=0, offset=0, size=5, sha256=digest,
                account="bot-ops-ci", chat_id=-100987654321, message_id=101,
            ), int(time.time()),
        )
    finally:
        db.close()
    resolved = client.get(url, auth=("testadmin", "secure-auth-password"))
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["unjournaled_bot_ack_count"] == 0
    assert "unjournaled_bot_acks" not in {
        row["code"] for row in resolved.json()["alerts"]
    }

def test_operator_health_warns_on_unresolved_bot_send_without_identifiers(admin):
    """Ambiguous sends must be visible but token/file ID/object ID stay private."""
    import hashlib
    from teledrive.storage.bot_api_adapter import BotFileIndex

    client, path, _ = admin
    db = connect_database(path)
    try:
        idx = BotFileIndex(db)
        payload = b"unknown-channel-acceptance"
        idx.start_send(
            "private-operator-bot", -10098765432, "private-object-identifier",
            3, hashlib.sha256(payload).hexdigest(), len(payload),
        )
    finally:
        db.close()
    url = "/api/v1/admin/storage/health"
    assert client.get(url).status_code in (401, 403)
    response = client.get(url, auth=("testadmin", "secure-auth-password"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unresolved_bot_send_count"] == 1
    alerts = {entry["code"]: entry for entry in body["alerts"]}
    assert alerts["unresolved_bot_sends"]["severity"] == "critical"
    assert alerts["unresolved_bot_sends"]["count"] == 1
    for secret in ("private-operator-bot", "private-object-identifier",
                   "-10098765432", hashlib.sha256(payload).hexdigest()):
        assert secret not in response.text

    # Definitive non-acceptance clears only the corresponding intent.
    db = connect_database(path)
    try:
        BotFileIndex(db).clear_rejected_send(
            "private-operator-bot", -10098765432, "private-object-identifier",
            3, hashlib.sha256(payload).hexdigest(), len(payload),
        )
    finally:
        db.close()
    again = client.get(url, auth=("testadmin", "secure-auth-password"))
    assert again.status_code == 200
    assert again.json()["unresolved_bot_send_count"] == 0
    assert "unresolved_bot_sends" not in {
        entry["code"] for entry in again.json()["alerts"]
    }

def test_operator_unresolved_bot_inventory_requires_auth_and_has_no_side_effects(admin):
    """Read-only triage explicitly exposes checksum/size to authorized admin only."""
    import hashlib
    from teledrive.storage.bot_api_adapter import BotFileIndex

    client, path, _ = admin
    db = connect_database(path)
    try:
        index = BotFileIndex(db)
        index.start_send(
            "bot-a", -100123456, "object-inflight-a", 0,
            hashlib.sha256(b"first").hexdigest(), 5,
        )
        index.start_send(
            "bot-b", -100123457, "object-inflight-b", 1,
            hashlib.sha256(b"second").hexdigest(), 6,
        )
    finally:
        db.close()
    endpoint = "/api/v1/admin/storage/bot-unresolved"
    assert client.get(endpoint).status_code in (401, 403)
    unauthorized = client.get(endpoint, auth=("testadmin", "not-correct"))
    assert unauthorized.status_code in (401, 403)
    for limit in ("0", "1001", "-1", "not-an-integer"):
        assert client.get(endpoint + "?limit=" + limit,
                          auth=("testadmin", "secure-auth-password")).status_code == 422
    response = client.get(endpoint + "?limit=1",
                          auth=("testadmin", "secure-auth-password"))
    assert response.status_code == 200, response.text
    content = response.json()
    assert content["total"] == 2
    assert len(content["intents"]) == 1
    assert content["intents"][0]["account"] == "bot-a"
    assert content["intents"][0]["ordinal"] == 0
    assert content["intents"][0]["size"] == 5
    assert len(content["intents"][0]["sha256"]) == 64
    assert "independently verify" in content["message"].lower()
    for forbidden in ("bot_token", "file_id", "secret_key",
                      "lease_token", "123456:"):
        assert forbidden not in response.text
    full = client.get(endpoint, auth=("testadmin", "secure-auth-password"))
    assert full.status_code == 200
    assert len(full.json()["intents"]) == 2
    assert full.json()["total"] == 2
    # Read-only inventory must not remove/fabricate receipts or send media.
    db = connect_database(path)
    try:
        idx = BotFileIndex(db)
        assert idx.unresolved_count() == 2
        # This minimal fixture deliberately has no storage_chunks table;
        # assert directly that the read-only API fabricated no Bot ACK.
        assert db.connection.exec_driver_sql(
            "SELECT COUNT(*) FROM storage_bot_upload_receipts"
        ).scalar_one() == 0
    finally:
        db.close()
