"""Admin GC audit is read-only and protects copied Telegram data."""
from __future__ import annotations

import hashlib
import time

from test_s3_admin import admin
from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.copy import copy_object
from teledrive.storage.manifest import ManifestStore, ChunkReference, chunks
from sqlalchemy import func, select


def test_admin_gc_audit_requires_auth_and_never_deletes_referenced_media(admin):
    client, url, _ = admin
    now = int(time.time())
    conn = connect_database(url)
    try:
        manifests = ManifestStore(conn)
        catalog = BucketCatalog(manifests)
        catalog.create_bucket("audit-bucket", now)
        identifier = manifests.begin("audit-bucket", "original", 4, now)
        manifests.append(identifier, ChunkReference(
            ordinal=0, offset=0, size=4,
            sha256=hashlib.sha256(b"data").hexdigest(),
            account="bot", chat_id=-100123, message_id=101,
        ), now)
        manifests.finish(identifier, hashlib.sha256(b"data").hexdigest(), now)
        catalog.publish(identifier, now=now)
        alias = copy_object(catalog, "audit-bucket", "original",
                            "audit-bucket", "copy", now=now)
        catalog.remove_current("audit-bucket", "original")
        assert conn.connection.execute(select(func.count())
                                       .select_from(chunks)).scalar_one() == 1
    finally:
        conn.close()

    path = "/api/v1/admin/storage/gc-audit"
    assert client.get(path).status_code in (401, 403)
    result = client.get(path + "?limit=1", auth=("testadmin", "secure-auth-password"))
    assert result.status_code == 200, result.text
    record = result.json()
    assert record["dry_run"] is True
    assert record["physical_deletion_enabled"] is False
    assert record["eligible_for_deletion"] == 0
    assert record["total_manifests"] == 2
    assert record["total_telegram_messages"] == 1
    assert record["copy_aliases"] == 1
    assert record["live_object_heads"] == 1
    assert record["protected_chunks"][0]["copied_aliases"] == 1
    assert record["protected_chunks"][0]["live_heads"] == 1
    conn = connect_database(url)
    try:
        assert conn.connection.execute(select(func.count())
                                       .select_from(chunks)).scalar_one() == 1
        assert BucketCatalog(ManifestStore(conn)).get(
            "audit-bucket", "copy"
        ).item.id == alias.object_id
    finally:
        conn.close()


def test_gc_audit_invalid_limits_blocked_by_validation(admin):
    client, _, _ = admin
    for value in ("0", "1001", "-1", "abc"):
        response = client.get(
            "/api/v1/admin/storage/gc-audit?limit=" + value,
            auth=("testadmin", "secure-auth-password"),
        )
        assert response.status_code == 422


def test_admin_gc_audit_counts_unjournaled_ack_and_ambiguous_bot_send(admin):
    """Bot ACKs are owned remote media even before a chunk journal exists."""
    from teledrive.storage.bot_api_adapter import BotFileIndex

    client, url, _ = admin
    now = int(time.time())
    db = connect_database(url)
    try:
        manifests = ManifestStore(db)
        catalog = BucketCatalog(manifests)
        catalog.create_bucket("gc-ack-proof", now)
        received = manifests.begin("gc-ack-proof", "receipt", 4, now - 86400)
        unknown = manifests.begin("gc-ack-proof", "unknown", 4, now - 86400)
        manifests.fail(received, "post-ACK SQL crash", now)
        manifests.fail(unknown, "lost network response", now)
        index = BotFileIndex(db)
        sha = hashlib.sha256(b"data").hexdigest()
        peer = -100888777666555
        index.start_send("test-ack", peer, received, 0, sha, 4)
        index.put_receipt(
            "test-ack", peer, 2001, "owned-file-id",
            object_id=received, ordinal=0, checksum=sha, size=4,
            require_intent=True,
        )
        index.start_send("test-ack", peer, unknown, 0, sha, 4)
        assert index.unjournaled_count() == 1
        assert index.unresolved_count() == 1
    finally:
        db.close()

    path = "/api/v1/admin/storage/gc-audit"
    reply = client.get(path, auth=("testadmin", "secure-auth-password"))
    assert reply.status_code == 200, reply.text
    inventory = reply.json()
    assert inventory["total_telegram_messages"] == 1
    assert inventory["unjournaled_bot_receipts"] == 1
    assert inventory["unresolved_bot_intents"] == 1
    assert inventory["eligible_for_deletion"] == 0
    assert inventory["dry_run"] is True
    assert inventory["physical_deletion_enabled"] is False
