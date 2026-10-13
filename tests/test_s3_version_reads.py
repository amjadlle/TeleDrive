"""Signed S3 VersionId historical GET/HEAD never exposes staging manifests."""
from __future__ import annotations

from test_s3_gateway import service, signed
from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore


def test_history_versionid_read_range_and_unlink_preserve_immutable_media(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    old = b"versioned-old-telegram" * 400
    new = b"versioned-new-telegram" * 31
    first = signed(service, "PUT", "/backup-bucket/history.bin", old)
    assert first.status_code == 200
    db = service[3]()
    try:
        catalog = BucketCatalog(ManifestStore(db))
        old_id = catalog.get("backup-bucket", "history.bin").item.id
    finally:
        db.close()
    assert signed(service, "PUT", "/backup-bucket/history.bin", new).status_code == 200
    current = signed(service, "GET", "/backup-bucket/history.bin")
    assert current.content == new
    prior = signed(service, "GET", f"/backup-bucket/history.bin?versionId={old_id}")
    assert prior.status_code == 200, prior.text
    assert prior.content == old
    assert prior.headers["x-amz-version-id"] == old_id
    head = signed(service, "HEAD", f"/backup-bucket/history.bin?versionId={old_id}")
    assert head.status_code == 200
    assert head.headers["content-length"] == str(len(old))
    partial = signed(service, "GET", f"/backup-bucket/history.bin?versionId={old_id}",
                     extra_headers={"Range": "bytes=31-79"})
    assert partial.status_code == 206
    assert partial.content == old[31:80]
    assert signed(service, "DELETE", "/backup-bucket/history.bin").status_code == 204
    assert signed(service, "GET", "/backup-bucket/history.bin").status_code == 404
    assert signed(service, "GET", f"/backup-bucket/history.bin?versionId={old_id}").content == old


def test_never_serves_ready_unpublished_objects_as_s3_versions(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    db = service[3]()
    try:
        cat = BucketCatalog(ManifestStore(db))
        staged_id = cat.manifests.begin("backup-bucket", "hidden.bin", 0, 123)
        import hashlib
        cat.manifests.finish(staged_id, hashlib.sha256(b"").hexdigest(), 123)
    finally:
        db.close()
    unavailable = signed(service, "GET", f"/backup-bucket/hidden.bin?versionId={staged_id}")
    assert unavailable.status_code == 404
    assert b"NoSuchVersion" in unavailable.content
    cannot_delete = signed(service, "DELETE", f"/backup-bucket/hidden.bin?versionId={staged_id}")
    assert cannot_delete.status_code == 501
    missing = signed(service, "GET", "/backup-bucket/hidden.bin?versionId=unknown")
    assert missing.status_code == 404
