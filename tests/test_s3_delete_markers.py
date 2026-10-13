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
    ):
        assert signed(service, "DELETE", path).status_code == 400
    # Tagging is an independent versioned operation. Unknown IDs return
    # NoSuchVersion, not a malformed delete-version selector.
    assert signed(service, "DELETE",
                  "/backup-bucket/one.bin?tagging&versionId=abc").status_code == 404
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


def test_version_tagging_selector_is_not_shadowed_by_version_delete(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/file.bin", b"kept").status_code == 200
    version = _current_id(service)
    deleted_tags = signed(service, "DELETE",
                          f"/backup-bucket/file.bin?tagging&versionId={version}")
    assert deleted_tags.status_code == 204, deleted_tags.text
    assert signed(service, "GET", f"/backup-bucket/file.bin?versionId={version}").content == b"kept"
    # An operation selector collision is rejected instead of deleting bytes.
    invalid = signed(service, "DELETE",
                     f"/backup-bucket/file.bin?tagging&versionId={version}&bogus=1")
    assert invalid.status_code in (400, 501)
    assert signed(service, "GET", "/backup-bucket/file.bin").content == b"kept"


def test_delete_version_requires_delete_scope_and_signed_query(service):
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
    from teledrive.s3.credentials import S3CredentialStore

    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/file.bin", b"protect").status_code == 200
    version = _current_id(service)
    db = service[3]()
    try:
        read_only = S3CredentialStore(db, service[4]).create(
            "read-only-version", ["s3:GetObject", "s3:ListBucketVersions"],
        )
    finally:
        db.close()
    denied = signed(service, "DELETE",
                    f"/backup-bucket/file.bin?versionId={version}",
                    access=read_only.access_key, secret=read_only.secret_key)
    assert denied.status_code == 403
    path = f"/backup-bucket/file.bin?versionId={version}"
    req = AWSRequest(method="DELETE", url="http://localhost:9000" + path,
                     data=b"", headers={
                         "x-amz-content-sha256": hashlib.sha256(b"").hexdigest(),
                     })
    S3SigV4Auth(Credentials(service[1].access_key, service[1].secret_key),
                "s3", "us-east-1").add_auth(req)
    # Keep the authenticated header signature but change the query selector.
    changed = service[0].delete(
        "/backup-bucket/file.bin?versionId=forged", headers=dict(req.headers)
    )
    assert changed.status_code == 403
    assert signed(service, "GET", f"/backup-bucket/file.bin?versionId={version}").content == b"protect"


def test_marker_is_retained_by_online_metadata_snapshot(tmp_path, service):
    from scripts.backup_metadata import backup_sqlite
    from scripts.verify_metadata_backup import verify_snapshot

    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/file.bin", b"backup-me").status_code == 200
    marker = signed(service, "DELETE", "/backup-bucket/file.bin").headers["x-amz-version-id"]
    database = tmp_path / "state.db"
    snapshot = tmp_path / "snapshot.db"
    checksum = backup_sqlite(database, snapshot)
    result = verify_snapshot(snapshot, checksum)
    assert result["valid"] is True
    assert result["versions"] == 2
    assert result["heads"] == 1
    assert result["telegram_chunk_refs"] == 1
    assert signed(service, "GET", f"/backup-bucket/file.bin?versionId={marker}").status_code == 405


def test_batch_delete_produces_visible_markers_and_never_deletes_telegram(service):
    import base64
    from test_s3_batch_delete import delete_xml

    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/file.bin", b"batch-data").status_code == 200
    data = delete_xml(["file.bin", "unlisted.bin"])
    before = dict(service[2].parts)
    digest = base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()
    response = signed(service, "POST", "/backup-bucket?delete", data,
                      extra_headers={"Content-MD5": digest})
    assert response.status_code == 200, response.text
    entries, _ = _versions(service)
    assert sum(is_marker for _, _, is_marker, _ in entries) == 2
    assert signed(service, "GET", "/backup-bucket/file.bin").status_code == 404
    assert service[2].parts == before



def test_removed_version_is_invisible_to_all_tagging_methods_and_direct_store(service):
    """S3 gateway and SQL service share the same irreversible removal fence."""
    from teledrive.s3.tagging import ObjectTagStore, TaggingError
    from sqlalchemy import select
    from teledrive.s3.tagging import object_tags

    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    data = b"revoked-version-must-stay-hidden" * 4097
    assert signed(service, "PUT", "/backup-bucket/secure.bin", data).status_code == 200
    version = _current_id(service, "secure.bin")
    assert signed(service, "PUT",
                  f"/backup-bucket/secure.bin?tagging&versionId={version}",
                  b"<Tagging><TagSet><Tag><Key>safe</Key><Value>original</Value></Tag></TagSet></Tagging>").status_code == 200
    before = service[2].uploads
    removed = signed(service, "DELETE",
                     f"/backup-bucket/secure.bin?versionId={version}")
    assert removed.status_code == 204
    for method in ("GET", "PUT", "DELETE"):
        path = f"/backup-bucket/secure.bin?tagging&versionId={version}"
        body = (b"<Tagging><TagSet><Tag><Key>safe</Key>"
                b"<Value>attack</Value></Tag></TagSet></Tagging>") if method == "PUT" else b""
        res = signed(service, method, path, body)
        assert res.status_code == 404, (method, res.text)
        assert b"NoSuchVersion" in res.content

    db = service[3]()
    try:
        cat = BucketCatalog(ManifestStore(db))
        tags = ObjectTagStore(cat)
        assert not cat.is_visible_version("backup-bucket", "secure.bin", version)
        with pytest.raises(TaggingError) as denied:
            tags.replace_published("backup-bucket", "secure.bin",
                                   version, [("safe", "attack")])
        assert denied.value.code == "NoSuchVersion"
        original = db.connection.execute(select(
            object_tags.c.tag_key, object_tags.c.tag_value
        ).where(object_tags.c.object_id == version)).all()
        assert [tuple(x) for x in original] == [("safe", "original")]
    finally:
        db.close()
    assert service[2].uploads == before
    assert signed(service, "GET", "/backup-bucket/secure.bin").status_code == 404


def test_published_version_visibility_requires_untampered_sql_manifest_identity(service):
    """A corrupted published index may not make another object's bytes visible."""
    from sqlalchemy import update
    from teledrive.storage.manifest import objects

    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/other.bin", b"private" * 791).status_code == 200
    version = _current_id(service, "other.bin")
    db = service[3]()
    try:
        catalog = BucketCatalog(ManifestStore(db))
        assert catalog.is_visible_version("backup-bucket", "other.bin", version)
        db.connection.execute(update(objects).where(
            objects.c.id == version
        ).values(object_key="wrong-key.bin"))
        db.commit()
        assert not catalog.is_visible_version("backup-bucket", "other.bin", version)
    finally:
        db.close()
    res = signed(service, "GET",
                 f"/backup-bucket/other.bin?versionId={version}")
    assert res.status_code in (404, 409), res.text
