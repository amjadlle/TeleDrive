"""Signed server-side CopySource historical versions: no Telegram reupload."""
from __future__ import annotations

import hashlib

from test_s3_gateway import service, signed
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore


def _version(service, key):
    db = service[3]()
    try:
        return BucketCatalog(ManifestStore(db)).get("backup-bucket", key).item.id
    finally:
        db.close()


def test_copy_old_published_source_after_head_overwrite_and_delete(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/source.bin", b"old-version").status_code == 200
    old_id = _version(service, "source.bin")
    assert signed(service, "PUT", "/backup-bucket/source.bin", b"new-version").status_code == 200
    uploads = service[2].uploads
    result = signed(service, "PUT", "/backup-bucket/copied.bin", b"",
                    extra_headers={"x-amz-copy-source":
                                   "/backup-bucket/source.bin?versionId=" + old_id})
    assert result.status_code == 200, result.text
    assert signed(service, "GET", "/backup-bucket/copied.bin").content == b"old-version"
    assert signed(service, "GET", "/backup-bucket/source.bin").content == b"new-version"
    assert service[2].uploads == uploads
    assert signed(service, "DELETE", "/backup-bucket/source.bin").status_code == 204
    again = signed(service, "PUT", "/backup-bucket/after-delete.bin", b"",
                   extra_headers={"x-amz-copy-source":
                                  "/backup-bucket/source.bin?versionId=" + old_id})
    assert again.status_code == 200, again.text
    assert signed(service, "GET", "/backup-bucket/after-delete.bin").content == b"old-version"
    assert service[2].uploads == uploads


def test_copy_rejects_unknown_or_wrong_source_version_without_upload(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/source.bin", b"current").status_code == 200
    version = _version(service, "source.bin")
    db = service[3]()
    try:
        cat = BucketCatalog(ManifestStore(db))
        unpub = cat.manifests.begin("backup-bucket", "source.bin", 0, 123)
        cat.manifests.finish(unpub, hashlib.sha256(b"").hexdigest(), 123)
    finally:
        db.close()
    uploads = service[2].uploads
    for src, code in (
        ("/backup-bucket/source.bin?versionId=" + unpub, 404),
        ("/backup-bucket/other.bin?versionId=" + version, 404),
        ("/backup-bucket/source.bin?versionId=unknown", 404),
        ("/backup-bucket/source.bin?versionId=" + version + "&versionId=" + version, 400),
        ("/backup-bucket/source.bin?partNumber=1", 400),
        ("/backup-bucket/source.bin?versionId=", 400),
    ):
        response = signed(service, "PUT", "/backup-bucket/fail.bin", b"",
                          extra_headers={"x-amz-copy-source": src})
        assert response.status_code == code, (src, response.text)
        assert signed(service, "GET", "/backup-bucket/fail.bin").status_code == 404
    assert service[2].uploads == uploads
