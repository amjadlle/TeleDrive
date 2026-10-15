"""Release-blocking P0 failure matrix over production S3 HTTP and SQLite.

Run with deterministic Telegram-compatible fake to prove crash atomicity,
authentication revocation, and multi-chunk range behavior without touching
any user or production Telegram messages.
"""
from __future__ import annotations

import base64
import hashlib
import os
import time

import boto3
import pytest
from botocore.config import Config
from sqlalchemy import select

from tests.s3.test_s3_gateway import service, signed
from tests.s3.test_s3_multipart import begin, complete, part
from tests.s3.test_s3_presigned import request_url
from teledrive.db import Database
from teledrive.s3.credentials import S3CredentialStore
from teledrive.s3.multipart import MultipartStore, parts, uploads
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore
import web.s3 as gateway


def _mpu(service):
    db = service[3]()
    return db, MultipartStore(BucketCatalog(ManifestStore(db)))


def test_failed_sql_commit_during_complete_rolls_back_and_retry_preserves_bytes(
    service, monkeypatch
):
    """Crash after chunk reparenting but before final SQL commit is all-or-nothing."""
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    assert signed(service, "PUT", "/backup-bucket/asset.bin", b"old-head").status_code == 200
    uid = begin(service)
    payload = b"remote-message-owned-by-staged-part" * 120
    etag = part(service, uid, 1, payload)
    original_uploads = service[2].uploads

    db, manager = _mpu(service)
    try:
        staging_id = db.connection.execute(
            select(parts.c.object_id).where(
                parts.c.upload_id == uid, parts.c.number == 1
            )
        ).scalar_one()
        before_refs = manager.manifests.parts(staging_id)
        assert len(before_refs) == 1
    finally:
        db.close()

    original_commit = Database.commit
    faulted = []

    def fail_at_final_commit(self):
        # Stage complete has an in-flight "completed" row only during the
        # publication transaction; the earlier lease claim is "completing".
        state = self.connection.execute(
            select(uploads.c.state).where(uploads.c.id == uid)
        ).scalar_one_or_none()
        if state == "completed" and not faulted:
            faulted.append(True)
            raise RuntimeError("injected SQL commit outage")
        return original_commit(self)

    monkeypatch.setattr(Database, "commit", fail_at_final_commit)
    interrupted = complete(service, uid, [(1, etag)])
    assert interrupted.status_code == 503, interrupted.text
    assert faulted, "Fault injection must reach the final publish transaction"
    assert service[2].uploads == original_uploads
    monkeypatch.setattr(Database, "commit", original_commit)

    # The previously published HEAD and staged message IDs survive across
    # fresh SQL sessions. There must be no half-visible final object.
    assert signed(service, "GET", "/backup-bucket/asset.bin").content == b"old-head"
    db, manager = _mpu(service)
    try:
        assert manager.require(uid, "backup-bucket", "asset.bin",
                               service[1].access_key)["state"] == "active"
        assert manager.manifests.parts(staging_id) == before_refs
        assert manager.catalog.get("backup-bucket", "asset.bin").item.size == len(b"old-head")
    finally:
        db.close()

    done = complete(service, uid, [(1, etag)])
    assert done.status_code == 200, done.text
    assert signed(service, "GET", "/backup-bucket/asset.bin").content == payload
    assert service[2].uploads == original_uploads
    retry = complete(service, uid, [(1, etag)])
    assert retry.status_code == 200, retry.text
    assert service[2].uploads == original_uploads


def test_telegram_read_failure_during_complete_leaves_staging_retryable(
    service, monkeypatch
):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    uid = begin(service)
    payload = b"recoverable-multipart-part" * 100
    etag = part(service, uid, 1, payload)
    original_download = service[2].download
    count = []

    async def broken_download(reference, destination):
        count.append(reference.message_id)
        raise RuntimeError("injected Telegram retrieval outage")

    monkeypatch.setattr(service[2], "download", broken_download)
    interrupted = complete(service, uid, [(1, etag)])
    assert count
    assert interrupted.status_code == 503, interrupted.text
    monkeypatch.setattr(service[2], "download", original_download)
    assert signed(service, "GET", "/backup-bucket/asset.bin").status_code == 404
    db, manager = _mpu(service)
    try:
        assert manager.require(uid, "backup-bucket", "asset.bin",
                               service[1].access_key)["state"] == "active"
        assert len(manager.list_parts(uid, "backup-bucket", "asset.bin",
                                      service[1].access_key)) == 1
    finally:
        db.close()
    before = service[2].uploads
    assert complete(service, uid, [(1, etag)]).status_code == 200
    assert signed(service, "GET", "/backup-bucket/asset.bin").content == payload
    assert service[2].uploads == before


def test_expired_and_revoked_presigned_get_put_never_touch_telegram(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    body = b"keep-immutable"
    assert signed(service, "PUT", "/backup-bucket/asset.bin", body).status_code == 200
    connection = service[3]()
    try:
        key = S3CredentialStore(connection, service[4]).create(
            "presigned-revocable", ["s3:GetObject", "s3:PutObject"]
        )
    finally:
        connection.close()

    s3 = boto3.client("s3", endpoint_url="http://localhost:9000",
        aws_access_key_id=key.access_key,
        aws_secret_access_key=key.secret_key,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                      request_checksum_calculation="when_required",
                      response_checksum_validation="when_required"))
    read = s3.generate_presigned_url(
        "get_object", Params={"Bucket": "backup-bucket", "Key": "asset.bin"},
        ExpiresIn=600,
    )
    assert request_url(service[0], "GET", read).content == body
    unauthorized = b"must-not-upload"
    checksum = base64.b64encode(hashlib.sha256(unauthorized).digest()).decode("ascii")
    write = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": "backup-bucket", "Key": "new.bin",
                "ChecksumSHA256": checksum, "IfNoneMatch": "*"},
        ExpiresIn=600, HttpMethod="PUT",
    )

    db = service[3]()
    try:
        assert S3CredentialStore(db, service[4]).revoke(key.id)
    finally:
        db.close()
    before = service[2].uploads
    for outcome in (
        request_url(service[0], "GET", read),
        request_url(service[0], "PUT", write, content=unauthorized,
                    headers={"x-amz-checksum-sha256": checksum,
                             "If-None-Match": "*"}),
    ):
        assert outcome.status_code == 403
        assert b"AccessDenied" in outcome.content
    assert service[2].uploads == before
    assert signed(service, "GET", "/backup-bucket/asset.bin").content == body
    assert signed(service, "GET", "/backup-bucket/new.bin").status_code == 404


def test_large_multi_chunk_range_and_head_over_fresh_sql_connections(
    service, monkeypatch
):
    """Exercise genuine multi-chunk reads, not just slicing a single blob."""
    monkeypatch.setattr(gateway, "RECOMMENDED_CHUNK_SIZE", 2 * 1024 * 1024)
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    payload = os.urandom(5 * 1024 * 1024 + 137)
    wrote = signed(service, "PUT", "/backup-bucket/chunks.bin", payload)
    assert wrote.status_code == 200, wrote.text
    assert service[2].uploads == 3
    downloads_before_head = service[2].downloads
    head = signed(service, "HEAD", "/backup-bucket/chunks.bin")
    assert head.status_code == 200
    assert int(head.headers["content-length"]) == len(payload)
    assert service[2].downloads == downloads_before_head

    for offset, length in [
        (2 * 1024 * 1024 - 19, 61),
        (4 * 1024 * 1024 - 3, 19),
        (len(payload) - 91, 91),
        (0, len(payload)),
    ]:
        # Every request gets a new independent DB connection from the
        # gateway fixture, including after the writer connection closes.
        result = signed(
            service, "GET", "/backup-bucket/chunks.bin",
            extra_headers={"Range": f"bytes={offset}-{offset+length-1}"},
        )
        assert result.status_code == 206, result.text[:200]
        assert result.content == payload[offset:offset + length]
        assert result.headers["content-range"] == (
            f"bytes {offset}-{offset+length-1}/{len(payload)}"
        )
        assert result.headers["content-length"] == str(length)
    suffix = signed(service, "GET", "/backup-bucket/chunks.bin",
                    extra_headers={"Range": "bytes=-47"})
    assert suffix.status_code == 206 and suffix.content == payload[-47:]
    invalid = signed(service, "GET", "/backup-bucket/chunks.bin",
                     extra_headers={"Range": f"bytes={len(payload)}-{len(payload)+100}"})
    assert invalid.status_code == 416
    assert signed(service, "GET", "/backup-bucket/chunks.bin").content == payload
