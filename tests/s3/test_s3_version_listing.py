"""S3 signed ListObjectVersions: published-only scope and stable pagination."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from tests.s3.test_s3_gateway import service, signed
from teledrive.s3.credentials import S3CredentialStore
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore


NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def test_signed_version_listing_excludes_staged_and_enforces_scoped_key(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    for key, payload in (
        ("history.bin", b"first"), ("history.bin", b"second"),
        ("other.bin", b"third"),
    ):
        assert signed(service, "PUT", "/backup-bucket/" + key, payload).status_code == 200
    db = service[3]()
    try:
        catalog = BucketCatalog(ManifestStore(db))
        import hashlib
        unpublished = catalog.manifests.begin("backup-bucket", "history.bin", 0, 123)
        catalog.manifests.finish(unpublished, hashlib.sha256(b"").hexdigest(), 123)
        key = S3CredentialStore(db, service[4]).create(
            "versions-only", ["s3:ListBucketVersions"]
        )
    finally:
        db.close()
    listed = signed(service, "GET", "/backup-bucket?versions",
                    access=key.access_key, secret=key.secret_key)
    assert listed.status_code == 200, listed.text
    root = ET.fromstring(listed.content)
    entries = root.findall(NS + "Version")
    assert len(entries) == 3
    assert unpublished not in listed.text
    assert [node.findtext(NS + "Key") for node in entries] == [
        "history.bin", "history.bin", "other.bin",
    ]
    assert [node.findtext(NS + "IsLatest") for node in entries] == [
        "true", "false", "true",
    ]
    assert signed(service, "GET", "/backup-bucket?list-type=2",
                  access=key.access_key, secret=key.secret_key).status_code == 403
    assert signed(service, "GET", "/backup-bucket/history.bin",
                  access=key.access_key, secret=key.secret_key).status_code == 403


def test_version_listing_cursor_prefix_and_invalid_marker(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    for key, data in (
        ("a.bin", b"v1"), ("a.bin", b"v2"),
        ("a.bin", b"v3"), ("b.bin", b"last"),
    ):
        assert signed(service, "PUT", "/backup-bucket/" + key, data).status_code == 200
    got = []
    marker = ""
    for _ in range(4):
        page = signed(service, "GET", "/backup-bucket?versions&max-keys=1" + marker)
        assert page.status_code == 200, page.text
        root = ET.fromstring(page.content)
        versions = root.findall(NS + "Version")
        assert len(versions) == 1
        got.append(versions[0].findtext(NS + "VersionId"))
        if root.findtext(NS + "IsTruncated") == "false":
            break
        marker = (
            "&key-marker=" + root.findtext(NS + "NextKeyMarker")
            + "&version-id-marker=" + root.findtext(NS + "NextVersionIdMarker")
        )
    assert len(set(got)) == 4
    filtered = signed(service, "GET", "/backup-bucket?versions&prefix=b.")
    assert filtered.status_code == 200
    assert [node.findtext(NS + "Key") for node in ET.fromstring(
        filtered.content
    ).findall(NS + "Version")] == ["b.bin"]
    for query in (
        "?versions&max-keys=0", "?versions&version-id-marker=missing",
        "?versions&key-marker=a.bin&version-id-marker=missing",
        "?versions&versions", "?versions&delimiter=/",
    ):
        denied = signed(service, "GET", "/backup-bucket" + query)
        assert denied.status_code in (400, 501), denied.text


def test_version_listing_revocation_and_unlink_preserve_remote_bytes(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/archived.bin", b"retained").status_code == 200
    db = service[3]()
    try:
        reader = S3CredentialStore(db, service[4]).create(
            "versions-revocable", ["s3:ListBucketVersions"]
        )
    finally:
        db.close()
    before = service[2].uploads
    assert signed(service, "DELETE", "/backup-bucket/archived.bin").status_code == 204
    listing = signed(service, "GET", "/backup-bucket?versions",
                     access=reader.access_key, secret=reader.secret_key)
    assert listing.status_code == 200
    assert b"archived.bin" in listing.content
    db = service[3]()
    try:
        assert S3CredentialStore(db, service[4]).revoke(reader.id)
    finally:
        db.close()
    assert signed(service, "GET", "/backup-bucket?versions",
                  access=reader.access_key, secret=reader.secret_key).status_code == 403
    assert service[2].uploads == before
