"""S3 versioned tombstones: publication fencing and logical VersionId deletion.

All writes use real signed TestClient HTTP and SQLite transactions. Telegram
fake tracks uploads; physical deletion is categorically forbidden.
"""
from __future__ import annotations

import hashlib
from xml.etree import ElementTree as ET

import pytest

from test_s3_gateway import service, signed
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore

NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _current_id(service, key="file.bin"):
    conn = service[3]()
    try:
        return BucketCatalog(ManifestStore(conn)).get("backup-bucket", key).item.id
    finally:
        conn.close()


def _versions(service, *, suffix=""):
    result = signed(service, "GET", "/backup-bucket?versions" + suffix)
    assert result.status_code == 200, result.text
    root = ET.fromstring(result.content)
    rows = []
    for node in root:
        if node.tag in (NS + "Version", NS + "DeleteMarker"):
            rows.append((
                node.findtext(NS + "Key"),
                node.findtext(NS + "VersionId"),
                node.tag == NS + "DeleteMarker",
                node.findtext(NS + "IsLatest") == "true",
            ))
    return rows, root


def test_delete_marker_hides_latest_but_preserves_historical_bytes(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/file.bin", b"first").status_code == 200
    previous = _current_id(service)
    before = service[2].uploads
    deletion = signed(service, "DELETE", "/backup-bucket/file.bin")
    assert deletion.status_code == 204, deletion.text
    assert deletion.headers["x-amz-delete-marker"] == "true"
    marker = deletion.headers["x-amz-version-id"]
    assert marker and marker != previous
    latest = signed(service, "GET", "/backup-bucket/file.bin")
    assert latest.status_code == 404
    assert latest.headers["x-amz-delete-marker"] == "true"
    assert latest.headers["x-amz-version-id"] == marker
    assert signed(service, "HEAD", "/backup-bucket/file.bin").status_code == 404
    historical = signed(service, "GET", f"/backup-bucket/file.bin?versionId={previous}")
    assert historical.status_code == 200 and historical.content == b"first"
    selected = signed(service, "GET", f"/backup-bucket/file.bin?versionId={marker}")
    assert selected.status_code == 405
    assert selected.headers["x-amz-delete-marker"] == "true"
    assert selected.headers["x-amz-version-id"] == marker
    rows, _ = _versions(service)
    assert rows == [("file.bin", marker, True, True),
                    ("file.bin", previous, False, False)]
    assert service[2].uploads == before


def test_delete_marker_version_removal_restores_prior_object_without_reupload(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/file.bin", b"old").status_code == 200
    old = _current_id(service)
    assert signed(service, "PUT", "/backup-bucket/file.bin", b"latest").status_code == 200
    current = _current_id(service)
    marker_response = signed(service, "DELETE", "/backup-bucket/file.bin")
    marker = marker_response.headers["x-amz-version-id"]
    before = service[2].uploads
    unlinked = signed(service, "DELETE", f"/backup-bucket/file.bin?versionId={marker}")
    assert unlinked.status_code == 204
    assert unlinked.headers["x-amz-delete-marker"] == "true"
    assert signed(service, "GET", "/backup-bucket/file.bin").content == b"latest"
    assert _current_id(service) == current
    assert signed(service, "GET", f"/backup-bucket/file.bin?versionId={marker}").status_code == 404
    remove_current = signed(service, "DELETE", f"/backup-bucket/file.bin?versionId={current}")
    assert remove_current.status_code == 204
    assert _current_id(service) == old
    assert signed(service, "GET", "/backup-bucket/file.bin").content == b"old"
    assert signed(service, "GET", f"/backup-bucket/file.bin?versionId={current}").status_code == 404
    assert signed(service, "GET", f"/backup-bucket/file.bin?versionId={old}").content == b"old"
    rows, _ = _versions(service)
    assert rows == [("file.bin", old, False, True)]
    assert service[2].uploads == before


def test_multiple_markers_are_immutable_and_current_last_marker_can_be_removed(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/file.bin", b"payload").status_code == 200
    first = signed(service, "DELETE", "/backup-bucket/file.bin").headers["x-amz-version-id"]
    second = signed(service, "DELETE", "/backup-bucket/file.bin").headers["x-amz-version-id"]
    assert first != second
    assert signed(service, "DELETE", f"/backup-bucket/file.bin?versionId={second}").status_code == 204
    last = signed(service, "GET", "/backup-bucket/file.bin")
    assert last.status_code == 404 and last.headers["x-amz-version-id"] == first
    assert signed(service, "DELETE", f"/backup-bucket/file.bin?versionId={first}").status_code == 204
    assert signed(service, "GET", "/backup-bucket/file.bin").content == b"payload"


def test_missing_foreign_staged_and_deleted_version_ids_never_leak(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/one.bin", b"one").status_code == 200
    actual = _current_id(service, "one.bin")
    db = service[3]()
    try:
        cat = BucketCatalog(ManifestStore(db))
        staged = cat.manifests.begin("backup-bucket", "one.bin", 0, 123)
        cat.manifests.finish(staged, hashlib.sha256(b"").hexdigest(), 123)
    finally:
        db.close()
    before = service[2].uploads
    for key, version in (
        ("one.bin", "nonexistent"),
        ("other.bin", actual),
        ("one.bin", staged),
    ):
        response = signed(service, "DELETE", f"/backup-bucket/{key}?versionId={version}")
        assert response.status_code == 404
    for path in (
        "/backup-bucket/one.bin?versionId=",
        "/backup-bucket/one.bin?versionId=abc&versionId=xyz",
        "/backup-bucket/one.bin?versionId=abc&tagging",
    ):
        assert signed(service, "DELETE", path).status_code == 400
    assert signed(service, "GET", "/backup-bucket/one.bin").content == b"one"
    assert service[2].uploads == before


def test_latest_marker_can_be_replaced_using_signed_if_none_match_and_copy(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/source.bin", b"copy-source").status_code == 200
    marker = signed(service, "DELETE", "/backup-bucket/file.bin").headers["x-amz-version-id"]
    before = service[2].uploads
    copied = signed(service, "PUT", "/backup-bucket/file.bin", b"",
                    extra_headers={"x-amz-copy-source": "/backup-bucket/source.bin"})
    assert copied.status_code == 200, copied.text
    assert signed(service, "GET", "/backup-bucket/file.bin").content == b"copy-source"
    assert service[2].uploads == before
    assert signed(service, "DELETE", "/backup-bucket/file.bin").status_code == 204
    written = signed(service, "PUT", "/backup-bucket/file.bin", b"new",
                     extra_headers={"If-None-Match": "*"})
    assert written.status_code == 200, written.text
    assert signed(service, "GET", "/backup-bucket/file.bin").content == b"new"
    assert signed(service, "GET", f"/backup-bucket/file.bin?versionId={marker}").status_code == 405


def test_removed_version_is_inaccessible_through_copy_and_tagging(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/source.bin", b"private-version").status_code == 200
    source_id = _current_id(service, "source.bin")
    assert signed(service, "DELETE", f"/backup-bucket/source.bin?versionId={source_id}").status_code == 204
    before = service[2].uploads
    failed_copy = signed(service, "PUT", "/backup-bucket/after.bin", b"",
                         extra_headers={"x-amz-copy-source":
                                        f"/backup-bucket/source.bin?versionId={source_id}"})
    assert failed_copy.status_code == 404
    failed_tags = signed(service, "GET", f"/backup-bucket/source.bin?tagging&versionId={source_id}")
    assert failed_tags.status_code == 404
    assert service[2].uploads == before


def test_delete_marker_listing_pagination_across_versions(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    for key, value in (("a.bin", b"one"), ("a.bin", b"two"), ("b.bin", b"three")):
        assert signed(service, "PUT", f"/backup-bucket/{key}", value).status_code == 200
    marker = signed(service, "DELETE", "/backup-bucket/a.bin").headers["x-amz-version-id"]
    collected = []
    suffix = "&max-keys=1"
    for _ in range(6):
        page, root = _versions(service, suffix=suffix)
        assert len(page) == 1
        collected.extend(page)
        if root.findtext(NS + "IsTruncated") == "false":
            break
        suffix = ("&max-keys=1&key-marker=" + root.findtext(NS + "NextKeyMarker")
                  + "&version-id-marker=" + root.findtext(NS + "NextVersionIdMarker"))
    assert len(collected) == 4
    assert collected[0] == ("a.bin", marker, True, True)
    assert len({row[1] for row in collected}) == 4
    assert collected[-1][0] == "b.bin"


def test_delete_last_version_leaves_missing_head_and_keeps_remote(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/file.bin", b"keep-on-telegram").status_code == 200
    only = _current_id(service)
    before = service[2].uploads
    assert signed(service, "DELETE", f"/backup-bucket/file.bin?versionId={only}").status_code == 204
    assert signed(service, "GET", "/backup-bucket/file.bin").status_code == 404
    rows, _ = _versions(service)
    assert rows == []
    assert service[2].uploads == before
