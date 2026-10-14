"""P0 acceptance: crash, revoked credentials and fail-closed S3 operations.

Uses the production HTTP handler, SigV4, real SQLite and deterministic
Telegram-compatible I/O. No production Telegram messages can be deleted.
"""
from __future__ import annotations

import hashlib
import time

import pytest

from test_s3_gateway import service, signed
from test_s3_multipart import begin, part, complete
from teledrive.s3.credentials import S3CredentialStore
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore


@pytest.mark.parametrize("after_remote_ack", [False, True])
def test_remote_crash_before_or_after_ack_never_overwrites_ready_head(
    service, monkeypatch, after_remote_ack
):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/asset.bin",
                  b"already-safe").status_code == 200
    remote = service[2]
    original_call = remote.call
    old_uploads = remote.uploads

    async def crashed_send(method, fields, source=None):
        if method == "sendDocument":
            if after_remote_ack:
                await original_call(method, fields, source=source)
            raise RuntimeError("simulated crash before SQL journal commit")
        return await original_call(method, fields, source=source)

    monkeypatch.setattr(remote, "call", crashed_send)
    failed = signed(service, "PUT", "/backup-bucket/asset.bin", b"new-unpublished")
    assert failed.status_code == 503, failed.text
    assert remote.uploads == old_uploads + int(after_remote_ack)
    assert signed(service, "GET", "/backup-bucket/asset.bin").content == b"already-safe"

    db = service[3]()
    try:
        store = ManifestStore(db)
        states = store.interrupted()
        assert len(states) == 1
        assert states[0][1] in ("pending", "failed")
        # A failed/unindexed Telegram send must never become visible as a
        # second S3 head, and physical cleanup must remain explicit.
        assert BucketCatalog(store).get("backup-bucket", "asset.bin").item.size == len(b"already-safe")
    finally:
        db.close()


@pytest.mark.parametrize("method,path,content,headers", [
    ("PUT", "/backup-bucket/forbidden.bin", b"unauthorized", {}),
    ("DELETE", "/backup-bucket/asset.bin", b"", {}),
    ("POST", "/backup-bucket/asset.bin?uploads", b"", {}),
    ("PUT", "/backup-bucket/copied.bin", b"", {"x-amz-copy-source": "/backup-bucket/asset.bin"}),
    ("GET", "/backup-bucket/asset.bin?tagging", b"", {}),
    ("GET", "/backup-bucket?uploads", b"", {}),
])
def test_read_only_s3_credential_cannot_mutate_or_enumerate(
    service, method, path, content, headers
):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/asset.bin",
                  b"original").status_code == 200
    db = service[3]()
    try:
        reader = S3CredentialStore(db, service[4]).create(
            "read-only acceptance", ["s3:GetObject"]
        )
    finally:
        db.close()
    assert signed(service, "HEAD", "/backup-bucket/asset.bin",
                  access=reader.access_key, secret=reader.secret_key).status_code == 200
    assert signed(service, "GET", "/backup-bucket/asset.bin",
                  access=reader.access_key, secret=reader.secret_key).content == b"original"
    uploads = service[2].uploads
    rejected = signed(service, method, path, content, extra_headers=headers,
                      access=reader.access_key, secret=reader.secret_key)
    assert rejected.status_code == 403, rejected.text
    assert b"AccessDenied" in rejected.content
    assert service[2].uploads == uploads
    assert signed(service, "GET", "/backup-bucket/asset.bin").content == b"original"


def test_revocation_between_multipart_upload_and_complete_fails_closed(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/asset.bin", b"preserve").status_code == 200
    uid = begin(service)
    etag = part(service, uid, 1, b"staged-before-revocation")
    original_uploads = service[2].uploads
    db = service[3]()
    try:
        credentials = S3CredentialStore(db, service[4])
        reader = credentials.create("fresh-reader", ["s3:GetObject"])
        assert credentials.revoke(service[1].id)
    finally:
        db.close()
    assert signed(service, "PUT",
                  f"/backup-bucket/asset.bin?partNumber=2&uploadId={uid}",
                  b"must-not-upload").status_code == 403
    assert complete(service, uid, [(1, etag)]).status_code == 403
    assert signed(service, "DELETE",
                  f"/backup-bucket/asset.bin?uploadId={uid}").status_code == 403
    assert signed(service, "GET", "/backup-bucket/asset.bin",
                  access=reader.access_key, secret=reader.secret_key).content == b"preserve"
    assert service[2].uploads == original_uploads


def test_expired_credential_denied_by_real_sql_resolver_without_sleep(service, monkeypatch):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    now = int(time.time())
    db = service[3]()
    try:
        expiring = S3CredentialStore(db, service[4]).create(
            "expires-in-one-second", ["s3:PutObject", "s3:GetObject"],
            now=now, expires_at=now + 1,
        )
    finally:
        db.close()
    original_resolve = S3CredentialStore.resolve

    def expired(self, access_key, permission):
        return original_resolve(self, access_key, permission, now=now + 2)

    monkeypatch.setattr(S3CredentialStore, "resolve", expired)
    before = service[2].uploads
    denied = signed(service, "PUT", "/backup-bucket/expired.bin",
                    b"must-never-reach-telegram", access=expiring.access_key,
                    secret=expiring.secret_key)
    assert denied.status_code == 403
    assert b"AccessDenied" in denied.content
    assert service[2].uploads == before
