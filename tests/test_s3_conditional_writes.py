"""Conditional S3 writes: actual signed HTTP and real SQL concurrency boundaries.

An ETag check without an atomic publish would be a time-of-check/time-of-use
vulnerability. These tests trigger concurrent head mutations during Telegram
sendDocument and verify that the conditional writer CANNOT replace the winner.
"""
from __future__ import annotations

import asyncio
import hashlib
import time

from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from test_s3_gateway import service, signed
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore


def put(svc, body: bytes, key: str = "race.bin", **headers):
    return signed(svc, "PUT", f"/conditional/{key}", body,
                  extra_headers={name.replace("_", "-"): value
                                 for name, value in headers.items()})


def read(svc, key: str = "race.bin"):
    return signed(svc, "GET", f"/conditional/{key}")


def test_create_if_absent_and_etag_cas_prevent_stale_writes(service):
    assert signed(service, "PUT", "/conditional").status_code == 200
    first = put(service, b"first", If_None_Match="*")
    assert first.status_code == 200, first.text
    uploads = service[2].uploads
    conflict = put(service, b"must-not-write", If_None_Match="*")
    assert conflict.status_code == 412
    assert service[2].uploads == uploads
    current_tag = first.headers["etag"]
    denied = put(service, b"wrong", If_Match='"not-the-etag"')
    assert denied.status_code == 412
    assert service[2].uploads == uploads
    matching = put(service, b"second", If_Match=current_tag)
    assert matching.status_code == 200, matching.text
    stale = put(service, b"third", If_Match=current_tag)
    assert stale.status_code == 412
    assert read(service).content == b"second"


def test_signed_conditional_delete_is_atomic_and_missing_match_is_412(service):
    assert signed(service, "PUT", "/conditional").status_code == 200
    missing = signed(service, "DELETE", "/conditional/race.bin",
                     extra_headers={"If-Match": "*"})
    assert missing.status_code == 412
    first = put(service, b"important")
    etag = first.headers["etag"]
    denied = signed(service, "DELETE", "/conditional/race.bin",
                    extra_headers={"If-Match": '"bogus"'})
    assert denied.status_code == 412
    assert read(service).content == b"important"
    done = signed(service, "DELETE", "/conditional/race.bin",
                  extra_headers={"If-Match": etag})
    assert done.status_code == 204
    assert read(service).status_code == 404
    again = signed(service, "DELETE", "/conditional/race.bin",
                   extra_headers={"If-Match": etag})
    assert again.status_code == 412


def test_unsigned_write_precondition_cannot_be_added_to_signed_request(service):
    assert signed(service, "PUT", "/conditional").status_code == 200
    existing = put(service, b"keep")
    assert existing.status_code == 200
    raw = b"overwrite"
    req = AWSRequest(method="PUT", url="http://localhost:9000/conditional/race.bin",
                     data=raw, headers={
                         "x-amz-content-sha256": hashlib.sha256(raw).hexdigest(),
                     })
    req.context["payload_signing_enabled"] = True
    S3SigV4Auth(
        Credentials(service[1].access_key, service[1].secret_key),
        "s3", "us-east-1",
    ).add_auth(req)
    forged = dict(req.headers)
    forged["If-Match"] = existing.headers["etag"]
    before = service[2].uploads
    denied = service[0].put("/conditional/race.bin", content=raw, headers=forged)
    assert denied.status_code == 403
    assert b"AccessDenied" in denied.content
    assert service[2].uploads == before
    assert read(service).content == b"keep"


def test_competing_writer_during_telegram_upload_cannot_steal_conditional_head(
        service, monkeypatch):
    assert signed(service, "PUT", "/conditional").status_code == 200
    first = put(service, b"old")
    assert first.status_code == 200
    remote = service[2]
    original = remote.call
    intrusions = []

    async def concurrent_winner(method, fields, source=None):
        if method == "sendDocument" and not intrusions:
            # Separate transaction commits while first writer is awaiting Telegram.
            connection = service[3]()
            try:
                catalog = BucketCatalog(ManifestStore(connection))
                moment = int(time.time())
                winner_id = catalog.manifests.begin("conditional", "race.bin", 0, moment)
                catalog.manifests.finish(winner_id, hashlib.sha256(b"").hexdigest(), moment)
                catalog.publish(winner_id, now=moment)
                intrusions.append(winner_id)
            finally:
                connection.close()
        return await original(method, fields, source=source)

    monkeypatch.setattr(remote, "call", concurrent_winner)
    stale = put(service, b"stale-write-must-not-win", If_Match=first.headers["etag"])
    assert intrusions
    assert stale.status_code == 412, stale.text
    assert read(service).status_code == 200
    assert read(service).content == b""


def test_competing_create_during_upload_rejects_if_none_match(service, monkeypatch):
    assert signed(service, "PUT", "/conditional").status_code == 200
    remote = service[2]
    original = remote.call
    injected = []

    async def concurrent_create(method, fields, source=None):
        if method == "sendDocument" and not injected:
            db = service[3]()
            try:
                catalog = BucketCatalog(ManifestStore(db))
                moment = int(time.time())
                winner = catalog.manifests.begin("conditional", "race.bin", 0, moment)
                catalog.manifests.finish(winner, hashlib.sha256(b"").hexdigest(), moment)
                catalog.publish(winner, now=moment)
                injected.append(winner)
            finally:
                db.close()
        return await original(method, fields, source=source)

    monkeypatch.setattr(remote, "call", concurrent_create)
    result = put(service, b"should-not-win", If_None_Match="*")
    assert injected
    assert result.status_code == 412, result.text
    assert read(service).content == b""


def test_delete_recreate_same_generation_is_not_equal_object_identity(service):
    """Object IDs fence ABA even when generation resets after unlink/recreate."""
    assert signed(service, "PUT", "/conditional").status_code == 200
    original = put(service, b"A")
    assert original.status_code == 200
    db = service[3]()
    try:
        catalog = BucketCatalog(ManifestStore(db))
        observed = catalog.get("conditional", "race.bin")
        assert observed.generation == 1
        assert catalog.remove_current("conditional", "race.bin",
                                      expected_generation=1,
                                      expected_object_id=observed.item.id)
        now = int(time.time())
        replacement_id = catalog.manifests.begin("conditional", "race.bin", 0, now)
        catalog.manifests.finish(replacement_id, hashlib.sha256(b"").hexdigest(), now)
        catalog.publish(replacement_id, now=now)
        assert catalog.get("conditional", "race.bin").generation == 1
        try:
            catalog.remove_current("conditional", "race.bin",
                                   expected_generation=1,
                                   expected_object_id=observed.item.id)
        except ValueError:
            pass
        else:
            raise AssertionError("ABA guard allowed old object ID to unlink a new version")
        staged = catalog.manifests.begin("conditional", "race.bin", 0, now)
        catalog.manifests.finish(staged, hashlib.sha256(b"").hexdigest(), now)
        try:
            catalog.publish(staged, now=now, expected_generation=1,
                            expected_object_id=observed.item.id)
        except ValueError:
            pass
        else:
            raise AssertionError("ABA guard allowed stale writer to overwrite a new version")
        assert catalog.get("conditional", "race.bin").item.id == replacement_id
    finally:
        db.close()
