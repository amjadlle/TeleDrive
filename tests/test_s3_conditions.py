"""Conditional S3 reads must save Telegram bandwidth and preserve ETag semantics."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from test_s3_gateway import service, signed


def setup_object(service, body=b"cached-object-bytes"):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    put = signed(service, "PUT", "/backup-bucket/cache.bin", body)
    assert put.status_code == 200, put.text
    return put.headers["etag"]


def get(service, **headers):
    return signed(service, "GET", "/backup-bucket/cache.bin",
                  extra_headers={key.replace("_", "-"): value
                                 for key, value in headers.items()})


def head(service, **headers):
    return signed(service, "HEAD", "/backup-bucket/cache.bin",
                  extra_headers={key.replace("_", "-"): value
                                 for key, value in headers.items()})


def test_if_none_match_returns_304_without_telegram_io(service):
    tag = setup_object(service)
    remote = service[2]
    before = remote.downloads
    response = get(service, If_None_Match=tag)
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == tag
    assert remote.downloads == before

    wildcard = head(service, If_None_Match="*")
    assert wildcard.status_code == 304
    assert remote.downloads == before


def test_if_match_blocks_bad_etag_without_downloading_chunks(service):
    tag = setup_object(service)
    remote = service[2]
    before = remote.downloads
    blocked = get(service, If_Match='"unexpected"')
    assert blocked.status_code == 412
    assert b"PreconditionFailed" in blocked.content
    assert remote.downloads == before
    success = get(service, If_Match=tag)
    assert success.status_code == 200
    assert success.content == b"cached-object-bytes"
    assert remote.downloads > before


def test_if_modified_and_unmodified_since_use_stable_manifest_timestamp(service):
    setup_object(service)
    head_response = head(service)
    assert head_response.status_code == 200
    modified = head_response.headers["last-modified"]
    before = service[2].downloads
    unchanged = get(service, If_Modified_Since=modified)
    assert unchanged.status_code == 304
    assert service[2].downloads == before

    yesterday = format_datetime(datetime.now(timezone.utc) - timedelta(days=1), usegmt=True)
    prohibited = get(service, If_Unmodified_Since=yesterday)
    assert prohibited.status_code == 412
    assert service[2].downloads == before
    latest = get(service, If_Modified_Since=yesterday)
    assert latest.status_code == 200
    assert latest.content == b"cached-object-bytes"

    # Correct If-Match suppresses an old If-Unmodified-Since per RFC semantics.
    tag = head_response.headers["etag"]
    matching = get(service, If_Match=tag, If_Unmodified_Since=yesterday)
    assert matching.status_code == 200


def test_failed_write_conditions_reject_before_telegram_upload(service):
    tag = setup_object(service)
    before = service[2].uploads
    write = signed(service, "PUT", "/backup-bucket/cache.bin", b"unsafe-overwrite",
                   extra_headers={"If-None-Match": "*"})
    assert write.status_code == 412
    assert b"PreconditionFailed" in write.content
    deletion = signed(service, "DELETE", "/backup-bucket/cache.bin",
                      extra_headers={"If-Match": '"different"'})
    assert deletion.status_code == 412
    assert service[2].uploads == before
    assert get(service).content == b"cached-object-bytes"
    # A correct conditional DELETE must atomically unlink the current head.
    permitted = signed(service, "DELETE", "/backup-bucket/cache.bin",
                       extra_headers={"If-Match": tag})
    assert permitted.status_code == 204
    assert get(service).status_code == 404
