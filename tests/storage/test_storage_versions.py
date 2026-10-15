"""Published-version inventory and alias-based restore (no Telegram re-upload)."""
from __future__ import annotations

import hashlib
import time

from tests.s3.test_s3_admin import admin
from tests.s3.test_s3_gateway import service, signed
from tests.s3.test_s3_multipart import begin, part, complete
from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.copy import copy_object
from teledrive.storage.manifest import ManifestStore


def test_catalog_history_records_only_published_versions_and_restore_alias(admin):
    client, path, _ = admin
    now = int(time.time())
    conn = connect_database(path)
    try:
        cat = BucketCatalog(ManifestStore(conn))
        cat.create_bucket("published", now)
        def empty(name):
            ident = cat.manifests.begin("published", name, 0, now)
            cat.manifests.finish(ident, hashlib.sha256(b"").hexdigest(), now)
            return ident

        one = empty("file.bin")
        # Non-published READY manifests must not be advertised as versions.
        uncommitted = empty("file.bin")
        assert cat.list_versions("published", "file.bin") == []
        cat.publish(one, now=now)
        assert [v["version_id"] for v in cat.list_versions(
            "published", "file.bin"
        )] == [one]
        two = empty("file.bin")
        cat.publish(two, now=now)
        before = cat.list_versions("published", "file.bin")
        assert {v["version_id"] for v in before} == {one, two}
        assert [v["version_id"] for v in before if v["is_current"]] == [two]
    finally:
        conn.close()

    url = f"/api/v1/admin/storage/versions/{one}/restore"
    auth = ("testadmin", "secure-auth-password")
    payload = {"bucket": "published", "key": "file.bin", "expected_generation": 2}
    assert client.post(url, json=payload).status_code in (401, 403)
    assert client.post(url, auth=auth, json=payload).status_code == 403
    wrong = client.post(url, auth=auth,
                        headers={"x-teledrive-csrf": "1"},
                        json={**payload, "expected_generation": 1})
    assert wrong.status_code == 409
    restored = client.post(url, auth=auth,
                           headers={"x-teledrive-csrf": "1"}, json=payload)
    assert restored.status_code == 200, restored.text
    fresh = restored.json()
    assert fresh["generation"] == 3
    assert fresh["source_version_id"] == one
    assert fresh["id"] not in {one, two}

    conn = connect_database(path)
    try:
        cat = BucketCatalog(ManifestStore(conn))
        versions = cat.list_versions("published", "file.bin")
        assert {v["version_id"] for v in versions} == {one, two, fresh["id"]}
        assert [v["version_id"] for v in versions if v["is_current"]] == [fresh["id"]]
        assert cat.manifests.parts(fresh["id"]) == []
        assert cat.get("published", "file.bin").item.id == fresh["id"]
        assert uncommitted not in {v["version_id"] for v in versions}
        assert cat.remove_current("published", "file.bin")
        assert cat.list_versions("published", "file.bin")
    finally:
        conn.close()

    again = client.post(url, auth=auth,
                        headers={"x-teledrive-csrf": "1"},
                        json={"bucket": "published", "key": "file.bin",
                              "expected_generation": 0})
    assert again.status_code == 200, again.text


def test_copy_and_multipart_publish_snapshot_ids_are_recorded(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/source.bin", b"content").status_code == 200
    db = service[3]()
    try:
        catalog = BucketCatalog(ManifestStore(db))
        initial = catalog.get("backup-bucket", "source.bin").item.id
        result = copy_object(
            catalog, "backup-bucket", "source.bin",
            "backup-bucket", "copy.bin",
        )
        assert {v["version_id"] for v in catalog.list_versions(
            "backup-bucket", "copy.bin"
        )} == {result.object_id}
        assert catalog.list_versions("backup-bucket", "source.bin")[0]["version_id"] == initial
    finally:
        db.close()

    uid = begin(service, key="multipart.bin")
    tag = part(service, uid, 1, b"upload-once", key="multipart.bin")
    committed = complete(service, uid, [(1, tag)], key="multipart.bin")
    assert committed.status_code == 200, committed.text
    db = service[3]()
    try:
        catalog = BucketCatalog(ManifestStore(db))
        latest = catalog.get("backup-bucket", "multipart.bin").item.id
        assert catalog.list_versions("backup-bucket", "multipart.bin")[0]["version_id"] == latest
    finally:
        db.close()


def test_legacy_backfill_only_indexes_live_proven_heads(admin):
    client, path, _ = admin
    now = int(time.time())
    db = connect_database(path)
    try:
        from sqlalchemy import delete
        from teledrive.storage.catalog import published_versions
        cat = BucketCatalog(ManifestStore(db))
        cat.create_bucket("legacy-vers", now)
        prior = cat.manifests.begin("legacy-vers", "current", 0, now)
        cat.manifests.finish(prior, hashlib.sha256(b"").hexdigest(), now)
        cat.publish(prior, now=now)
        never_published = cat.manifests.begin("legacy-vers", "staged", 0, now)
        cat.manifests.finish(never_published, hashlib.sha256(b"").hexdigest(), now)
        db.connection.execute(delete(published_versions).where(
            published_versions.c.object_id == prior
        ))
        db.commit()
        assert cat.list_versions("legacy-vers", "current") == []
    finally:
        db.close()

    endpoint = "/api/v1/admin/storage/versions/index-current"
    auth = ("testadmin", "secure-auth-password")
    headers = {"x-teledrive-csrf": "1"}
    assert client.post(endpoint, auth=auth, json={"execute":True}).status_code == 403
    preview = client.post(endpoint, auth=auth, headers=headers, json={})
    assert preview.status_code == 200
    assert preview.json()["dry_run"] and prior in preview.json()["ids"]
    done = client.post(endpoint, auth=auth, headers=headers,
                       json={"execute":True})
    assert done.status_code == 200
    assert done.json()["count"] == 1
    db = connect_database(path)
    try:
        cat = BucketCatalog(ManifestStore(db))
        assert cat.list_versions("legacy-vers", "current")[0]["version_id"] == prior
        assert cat.list_versions("legacy-vers", "staged") == []
    finally:
        db.close()
