"""Signed checksums are validated independently before Telegram uploads."""
from __future__ import annotations

import base64
import hashlib
import zlib

import pytest

from test_s3_gateway import service, signed
from test_s3_multipart import begin


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def test_valid_sha1_crc32_and_sha256_headers_bind_object_bytes(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    data = b"sha1-crc32-integrity" * 19
    headers = {
        "x-amz-checksum-sha1": _b64(hashlib.sha1(data, usedforsecurity=False).digest()),
        "x-amz-checksum-sha256": _b64(hashlib.sha256(data).digest()),
        "x-amz-checksum-crc32": _b64((zlib.crc32(data) & 0xffffffff).to_bytes(4, "big")),
    }
    result = signed(service, "PUT", "/backup-bucket/checked.bin", data,
                    extra_headers=headers)
    assert result.status_code == 200, result.text
    assert signed(service, "GET", "/backup-bucket/checked.bin").content == data


@pytest.mark.parametrize("bad_header", [
    {"x-amz-checksum-sha1": _b64(b"0" * 20)},
    {"x-amz-checksum-sha256": _b64(b"0" * 32)},
    {"content-md5": _b64(b"0" * 16)},
    {"x-amz-sdk-checksum-algorithm": "SHA1"},
])
def test_bad_extra_checksums_fail_even_when_signed_body_sha256_is_correct(
    service, bad_header
):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    before = service[2].uploads
    denied = signed(service, "PUT", "/backup-bucket/never.bin", b"valid-sha256",
                    extra_headers=bad_header)
    assert denied.status_code == 400, denied.text
    assert b"BadDigest" in denied.content
    assert service[2].uploads == before


@pytest.mark.parametrize("header", [
    "x-amz-checksum-crc32c", "x-amz-checksum-crc64nvme",
])
def test_unsupported_checksum_variants_do_not_silently_succeed(service, header):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    before = service[2].uploads
    result = signed(service, "PUT", "/backup-bucket/no-false-ack", b"content",
                    extra_headers={header: _b64(b"some-digest")})
    assert result.status_code == 501, result.text
    assert b"NotImplemented" in result.content
    assert service[2].uploads == before


def test_bad_multipart_checksum_does_not_contact_telegram(service):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    uid = begin(service)
    before = service[2].uploads
    response = signed(service, "PUT",
                      f"/backup-bucket/large.bin?uploadId={uid}&partNumber=1",
                      b"valid", extra_headers={
                          "x-amz-checksum-sha1": _b64(b"0" * 20),
                      })
    # This uploadId targets a different key from the default begin() key;
    # validate on the matching path in the next request instead.
    assert response.status_code == 404
    response = signed(service, "PUT",
                      f"/backup-bucket/asset.bin?uploadId={uid}&partNumber=1",
                      b"valid", extra_headers={
                          "x-amz-checksum-sha1": _b64(b"0" * 20),
                      })
    assert response.status_code == 400
    assert service[2].uploads == before
