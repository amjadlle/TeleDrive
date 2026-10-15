"""Version-specific S3 tagging modifies only published version SQL metadata."""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET

from tests.s3.test_s3_gateway import service, signed
from teledrive.s3.credentials import S3CredentialStore
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore

NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def _tag_xml(key: str, value: str) -> bytes:
    root = ET.Element("Tagging")
    tags = ET.SubElement(root, "TagSet")
    item = ET.SubElement(tags, "Tag")
    ET.SubElement(item, "Key").text = key
    ET.SubElement(item, "Value").text = value
    return ET.tostring(root)


def _tags(response):
    root = ET.fromstring(response.content)
    return [(x.findtext(NS + "Key"), x.findtext(NS + "Value"))
            for x in root.findall(NS + "TagSet/" + NS + "Tag")]


def test_version_specific_tag_mutations_are_independent_and_retain_telegram(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    path = "/backup-bucket/history.bin"
    assert signed(service, "PUT", path, b"old").status_code == 200
    db = service[3]()
    try:
        old_id = BucketCatalog(ManifestStore(db)).get("backup-bucket", "history.bin").item.id
    finally:
        db.close()
    assert signed(service, "PUT", path, b"new").status_code == 200
    before = service[2].uploads
    current = signed(service, "PUT", path + "?tagging", _tag_xml("new", "yes"))
    assert current.status_code in (200, 204)
    changed = signed(service, "PUT", path + "?tagging&versionId=" + old_id,
                     _tag_xml("old", "only"))
    assert changed.status_code == 200, changed.text
    historical = signed(service, "GET", path + "?tagging&versionId=" + old_id)
    assert historical.status_code == 200 and _tags(historical) == [("old", "only")]
    latest = signed(service, "GET", path + "?tagging")
    assert latest.status_code == 200 and _tags(latest) == [("new", "yes")]
    deleted = signed(service, "DELETE", path + "?tagging&versionId=" + old_id)
    assert deleted.status_code == 204
    assert _tags(signed(service, "GET", path + "?tagging&versionId=" + old_id)) == []
    assert _tags(signed(service, "GET", path + "?tagging")) == [("new", "yes")]
    assert signed(service, "GET", path + "?versionId=" + old_id).content == b"old"
    assert service[2].uploads == before


def test_unpublished_and_foreign_versions_cannot_be_tagged(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    db = service[3]()
    try:
        catalog = BucketCatalog(ManifestStore(db))
        staged = catalog.manifests.begin("backup-bucket", "never.bin", 0, 123)
        catalog.manifests.finish(staged, hashlib.sha256(b"").hexdigest(), 123)
    finally:
        db.close()
    for method in ("GET", "DELETE", "PUT"):
        path = "/backup-bucket/never.bin?tagging&versionId=" + staged
        result = signed(service, method, path,
                        _tag_xml("bad", "data") if method == "PUT" else b"")
        assert result.status_code == 404
        assert b"NoSuchVersion" in result.content
    assert signed(service, "GET", "/backup-bucket/never.bin?tagging&versionId=bad&versionId=other").status_code == 400


def test_version_tagging_uses_separate_scope_and_revocation(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/file.bin", b"data").status_code == 200
    db = service[3]()
    try:
        ver = BucketCatalog(ManifestStore(db)).get("backup-bucket", "file.bin").item.id
        reader = S3CredentialStore(db, service[4]).create(
            "version-tag-reader", ["s3:GetObjectTagging"]
        )
    finally:
        db.close()
    endpoint = "/backup-bucket/file.bin?tagging&versionId=" + ver
    assert signed(service, "GET", endpoint,
                  access=reader.access_key, secret=reader.secret_key).status_code == 200
    assert signed(service, "PUT", endpoint, _tag_xml("no", "write"),
                  access=reader.access_key, secret=reader.secret_key).status_code == 403
    db = service[3]()
    try:
        assert S3CredentialStore(db, service[4]).revoke(reader.id)
    finally:
        db.close()
    assert signed(service, "GET", endpoint,
                  access=reader.access_key, secret=reader.secret_key).status_code == 403
