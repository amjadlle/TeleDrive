"""SigV4 Host-bound S3 virtual-host routing is opt-in and refuses spoofing."""
from __future__ import annotations

import hashlib

from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from test_s3_gateway import service, signed


def _vhost(service, method, path, payload=b"", *, host="backup-bucket.s3.example.test:9000",
           headers=None):
    issued = service[1]
    request_headers = {
        "host": host,
        "x-amz-content-sha256": hashlib.sha256(payload).hexdigest(),
    }
    request_headers.update(headers or {})
    request = AWSRequest(
        method=method, url="http://" + host + path,
        data=payload, headers=request_headers,
    )
    request.context["payload_signing_enabled"] = True
    S3SigV4Auth(
        Credentials(issued.access_key, issued.secret_key), "s3", "us-east-1"
    ).add_auth(request)
    return service[0].request(
        method, path, content=payload, headers=dict(request.headers),
    )


def test_virtual_host_bucket_roundtrip_and_signed_host_is_required(service, monkeypatch):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    monkeypatch.setenv("TELEDRIVE_S3_VHOST_SUFFIX", "s3.example.test")
    body = b"virtual-host-telegram-contents"
    put = _vhost(service, "PUT", "/nested/file.bin", body)
    assert put.status_code == 200, put.text
    assert _vhost(service, "GET", "/nested/file.bin").content == body
    head = _vhost(service, "HEAD", "/nested/file.bin")
    assert head.status_code == 200
    assert head.headers["content-length"] == str(len(body))
    listing = _vhost(service, "GET", "/?list-type=2&prefix=nested")
    assert listing.status_code == 200
    assert b"nested/file.bin" in listing.content

    creds = service[1]
    original = AWSRequest(
        method="GET", url="http://backup-bucket.s3.example.test:9000/nested/file.bin",
        headers={"host": "backup-bucket.s3.example.test:9000",
                 "x-amz-content-sha256": hashlib.sha256(b"").hexdigest()},
    )
    original.context["payload_signing_enabled"] = True
    S3SigV4Auth(Credentials(creds.access_key, creds.secret_key),
                "s3", "us-east-1").add_auth(original)
    tampered = dict(original.headers)
    tampered["host"] = "another-bucket.s3.example.test:9000"
    rejected = service[0].get("/nested/file.bin", headers=tampered)
    assert rejected.status_code == 403
    assert b"SignatureDoesNotMatch" in rejected.content


def test_virtual_host_requires_explicit_suffix_and_no_proxy_header_trust(service, monkeypatch):
    assert signed(service, "PUT", "/backup-bucket").status_code == 200
    monkeypatch.delenv("TELEDRIVE_S3_VHOST_SUFFIX", raising=False)
    response = _vhost(service, "GET", "/object.bin")
    assert response.status_code == 404
    monkeypatch.setenv("TELEDRIVE_S3_VHOST_SUFFIX", "s3.example.test")
    forwarded = signed(
        service, "GET", "/backup-bucket?list-type=2",
        extra_headers={"X-Forwarded-Host": "another-bucket.s3.example.test:9000"},
    )
    assert forwarded.status_code == 200
    assert _vhost(service, "GET", "/", host="bad_name.s3.example.test:9000").status_code == 400
