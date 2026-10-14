"""P0 SigV4 privilege isolation across real SQL-backed S3 HTTP endpoints."""
from __future__ import annotations

import time

from sqlalchemy import update

from test_s3_gateway import service, signed
from teledrive.s3.credentials import S3CredentialStore, credentials


def test_expired_and_readonly_keys_cannot_mutate_or_list_storage(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    payload = b"keep-permissions-separated"
    assert signed(service, "PUT", "/backup-bucket/original", payload).status_code == 200
    remote = service[2]
    original_uploads = remote.uploads
    now = int(time.time())
    connection = service[3]()
    try:
        store = S3CredentialStore(connection, service[4])
        reader = store.create("p0-read-only", ["s3:GetObject"])
        expired = store.create(
            "p0-expired", ["s3:GetObject"], expires_at=now + 60, now=now
        )
        connection.connection.execute(
            update(credentials)
            .where(credentials.c.id == expired.id)
            .values(expires_at=now - 1)
        )
        connection.commit()
    finally:
        connection.close()

    def request(method, path, body=b"", credential=reader):
        return signed(
            service, method, path, body,
            access=credential.access_key, secret=credential.secret_key,
        )

    assert request("GET", "/backup-bucket/original").content == payload
    denied = [
        request("GET", "/"),
        request("GET", "/backup-bucket?list-type=2"),
        request("PUT", "/backup-bucket/injected", b"not-authorized"),
        request("DELETE", "/backup-bucket/original"),
        request("POST", "/backup-bucket/attempt?uploads"),
        request("PUT", "/backup-bucket/copied", credential=reader),
        request("GET", "/backup-bucket/original", credential=expired),
    ]
    assert all(item.status_code == 403 for item in denied), [
        (item.status_code, item.text[:200]) for item in denied
    ]
    assert all(b"AccessDenied" in item.content for item in denied)
    assert remote.uploads == original_uploads
    assert signed(service, "GET", "/backup-bucket/original").content == payload
    assert signed(service, "GET", "/backup-bucket/injected").status_code == 404
    assert signed(service, "GET", "/backup-bucket/copied").status_code == 404


def test_key_revocation_applies_before_any_mutation(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    connection = service[3]()
    try:
        store = S3CredentialStore(connection, service[4])
        scoped = store.create("p0-revocable", ["s3:PutObject", "s3:GetObject"])
    finally:
        connection.close()
    before = service[2].uploads
    connection = service[3]()
    try:
        assert S3CredentialStore(connection, service[4]).revoke(scoped.id)
    finally:
        connection.close()
    denied = signed(
        service, "PUT", "/backup-bucket/revoked", b"must-not-upload",
        access=scoped.access_key, secret=scoped.secret_key,
    )
    assert denied.status_code == 403
    assert b"AccessDenied" in denied.content
    assert service[2].uploads == before
    assert signed(service, "GET", "/backup-bucket/revoked").status_code == 404
