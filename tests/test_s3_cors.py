"""Bucket CORS S3 compatibility: boto3 XML contract and browser preflights.

Real SQLite and AWS SigV4; fake Telegram transport is used only to measure
the absence of payload calls. CORS never bypasses signed S3 permissions.
"""
from __future__ import annotations

import hashlib
import os
import socket
import threading
import time
from xml.etree import ElementTree as ET

import boto3
from botocore.config import Config

from test_s3_gateway import service, signed
from teledrive.s3.credentials import S3CredentialStore
from teledrive.s3.cors import parse_cors_xml, CORSError

NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def config_xml(origins=None, methods=None, headers=None, expose=None, max_age=180):
    node = ET.Element("CORSConfiguration")
    rule = ET.SubElement(node, "CORSRule")
    for origin in origins or ["https://app.example.com"]:
        ET.SubElement(rule, "AllowedOrigin").text = origin
    for method in methods or ["GET", "PUT"]:
        ET.SubElement(rule, "AllowedMethod").text = method
    for value in headers or ["authorization", "content-type", "x-amz-*"]:
        ET.SubElement(rule, "AllowedHeader").text = value
    for value in expose or ["ETag"]:
        ET.SubElement(rule, "ExposeHeader").text = value
    ET.SubElement(rule, "MaxAgeSeconds").text = str(max_age)
    return ET.tostring(node)


def create_bucket(svc, name="cors-bucket"):
    assert signed(svc, "PUT", "/" + name).status_code == 200


def test_signed_bucket_cors_round_trip_preflight_and_regular_get(service):
    client, _, fake, _, _ = service
    create_bucket(service)
    url = "/cors-bucket?cors"
    baseline = signed(service, "GET", url)
    assert baseline.status_code == 404 and b"NoSuchCORSConfiguration" in baseline.content
    prior = (fake.uploads, fake.downloads)
    assert client.options("/cors-bucket/file.bin", headers={
        "Origin": "https://app.example.com",
        "Access-Control-Request-Method": "GET"
    }).status_code == 403
    created = signed(service, "PUT", url, config_xml())
    assert created.status_code == 200, created.text
    config = signed(service, "GET", url)
    assert config.status_code == 200
    parsed = ET.fromstring(config.content)
    assert parsed.findtext(NS+"CORSRule/"+NS+"MaxAgeSeconds") == "180"
    assert parsed.findtext(NS+"CORSRule/"+NS+"AllowedOrigin") == "https://app.example.com"
    preflight = client.options("/cors-bucket/file.bin", headers={
        "Origin": "https://app.example.com",
        "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "Content-Type,X-Amz-Meta-Custom",
    })
    assert preflight.status_code == 200, preflight.text
    assert preflight.headers["access-control-allow-origin"] == "https://app.example.com"
    assert "PUT" in preflight.headers["access-control-allow-methods"]
    assert preflight.headers["access-control-max-age"] == "180"
    assert "content-type" in preflight.headers["access-control-allow-headers"].lower()
    assert "authorization" not in preflight.headers
    assert (fake.uploads, fake.downloads) == prior
    # CORS is not authorization: unsiged GET remains 403 even for allowed origins.
    unauth = client.get("/cors-bucket/private.bin", headers={"Origin": "https://app.example.com"})
    assert unauth.status_code == 403
    assert unauth.headers["access-control-allow-origin"] == "https://app.example.com"
    wrong_origin = client.options("/cors-bucket/private.bin", headers={
        "Origin": "https://evil.example.net",
        "Access-Control-Request-Method": "GET",
    })
    assert wrong_origin.status_code == 403
    wrong_header = client.options("/cors-bucket/private.bin", headers={
        "Origin": "https://app.example.com",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "X-Untrusted",
    })
    assert wrong_header.status_code == 403
    removed = signed(service, "DELETE", url)
    assert removed.status_code == 204
    assert signed(service, "GET", url).status_code == 404
    assert client.options("/cors-bucket/file.bin", headers={
        "Origin": "https://app.example.com",
        "Access-Control-Request-Method": "GET",
    }).status_code == 403


def test_cors_does_not_leak_across_buckets_and_is_durable(service):
    create_bucket(service)
    assert signed(service, "PUT", "/empty-bucket").status_code == 200
    assert signed(service, "PUT", "/cors-bucket?cors", config_xml()).status_code == 200
    client, _, _, connect, _ = service
    assert client.options("/empty-bucket/x", headers={
        "Origin": "https://app.example.com",
        "Access-Control-Request-Method": "GET",
    }).status_code == 403
    db = connect()
    try:
        from teledrive.s3.cors import BucketCORSStore
        from teledrive.storage.catalog import BucketCatalog
        from teledrive.storage.manifest import ManifestStore
        assert BucketCORSStore(BucketCatalog(ManifestStore(db))).get("cors-bucket")[0]["max_age"] == 180
    finally:
        db.close()
    assert signed(service, "DELETE", "/cors-bucket").status_code == 204
    assert signed(service, "PUT", "/cors-bucket").status_code == 200
    assert signed(service, "GET", "/cors-bucket?cors").status_code == 404


def test_scoped_get_put_delete_cors_independent_of_object_permissions(service):
    create_bucket(service)
    db = service[3]()
    try:
        keys = S3CredentialStore(db, service[4])
        obj = keys.create("object-reader", ["s3:GetObject"])
        reader = keys.create("cors-reader", ["s3:GetBucketCORS"])
        writer = keys.create("cors-writer", ["s3:PutBucketCORS"])
    finally:
        db.close()
    path = "/cors-bucket?cors"
    unauthorized = signed(service, "PUT", path, config_xml(),
                          access=obj.access_key, secret=obj.secret_key)
    assert unauthorized.status_code == 403
    allowed = signed(service, "PUT", path, config_xml(),
                     access=writer.access_key, secret=writer.secret_key)
    assert allowed.status_code == 200, allowed.text
    denied = signed(service, "GET", path,
                    access=writer.access_key, secret=writer.secret_key)
    assert denied.status_code == 403
    visible = signed(service, "GET", path,
                     access=reader.access_key, secret=reader.secret_key)
    assert visible.status_code == 200
    cannot_delete = signed(service, "DELETE", path,
                           access=reader.access_key, secret=reader.secret_key)
    assert cannot_delete.status_code == 403
    can_delete = signed(service, "DELETE", path,
                        access=writer.access_key, secret=writer.secret_key)
    assert can_delete.status_code == 204


def test_malformed_xml_entity_attack_bad_digest_preserve_previous_config(service):
    create_bucket(service)
    path = "/cors-bucket?cors"
    original = config_xml()
    assert signed(service, "PUT", path, original).status_code == 200
    for malformed in [
        b'<!DOCTYPE x [<!ENTITY bomb "xx">]><CORSConfiguration><CORSRule><AllowedOrigin>&bomb;</AllowedOrigin></CORSRule></CORSConfiguration>',
        b"<CORSConfiguration><CORSRule><AllowedMethod>GET",
        config_xml(origins=["https://a.example.com/path"]),
        config_xml(methods=["TRACE"]),
        config_xml(origins=["https://*.example.com.evil.test:bad"]),
        config_xml(max_age=100000),
    ]:
        rejected = signed(service, "PUT", path, malformed)
        assert rejected.status_code == 400, (malformed[:100], rejected.text)
        assert signed(service, "GET", path).status_code == 200
    # Original rule remains even after malformed and hostile policy writes.
    assert b"https://app.example.com" in signed(service, "GET", path).content
    assert signed(service, "PUT", path, b"x" * (64 * 1024 + 1)).status_code == 400


def test_real_boto3_bucket_cors_over_http_socket(service):
    import uvicorn
    import web.s3 as gateway

    client, creds, remote, _, _ = service
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(16)
    server = uvicorn.Server(uvicorn.Config(
        gateway.app, host="127.0.0.1", port=port, log_level="error", lifespan="off"
    ))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    worker.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        sdk = boto3.client("s3", endpoint_url=f"http://127.0.0.1:{port}",
            aws_access_key_id=creds.access_key, aws_secret_access_key=creds.secret_key,
            region_name="us-east-1",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path",
                         "payload_signing_enabled": True},
                         request_checksum_calculation="when_required",
                         response_checksum_validation="when_required",
                         retries={"max_attempts":0}),
        )
        sdk.create_bucket(Bucket="cors-sdk")
        before=(remote.uploads, remote.downloads)
        result = sdk.put_bucket_cors(Bucket="cors-sdk", CORSConfiguration={"CORSRules":[{
            "AllowedOrigins":["https://console.example.com"], "AllowedMethods":["GET", "HEAD"],
            "AllowedHeaders":["x-amz-*"], "ExposeHeaders":["ETag"], "MaxAgeSeconds":600,
        }]})
        assert result["ResponseMetadata"]["HTTPStatusCode"] == 200
        info = sdk.get_bucket_cors(Bucket="cors-sdk")
        assert info["CORSRules"][0]["AllowedOrigins"] == ["https://console.example.com"]
        assert info["CORSRules"][0]["MaxAgeSeconds"] == 600
        sdk.delete_bucket_cors(Bucket="cors-sdk")
        import botocore.exceptions
        try:
            sdk.get_bucket_cors(Bucket="cors-sdk")
            assert False, "Deleted CORS must return NoSuchCORSConfiguration"
        except botocore.exceptions.ClientError as exc:
            # Botocore sometimes reports an empty error Code for 404 bucket
            # subresource responses; require the actual HTTP status here.
            assert exc.response["ResponseMetadata"]["HTTPStatusCode"] == 404
        # Separately verify TeleDrive's exact S3 XML error contract, rather
        # than relying on botocore's lossy HTTP error parser.
        raw = signed(service, "GET", "/cors-sdk?cors")
        assert raw.status_code == 404
        assert ET.fromstring(raw.content).findtext(
            "{http://s3.amazonaws.com/doc/2006-03-01/}Code"
        ) == "NoSuchCORSConfiguration"
        assert (remote.uploads, remote.downloads) == before
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        sock.close()
        assert not worker.is_alive()


def test_wildcard_subdomain_cors_does_not_match_lookalikes(service):
    create_bucket(service)
    body = config_xml(
        origins=["https://*.example.com"],
        methods=["GET", "POST"],
        headers=["x-amz-*"],
        expose=["ETag"],
    )
    assert signed(service, "PUT", "/cors-bucket?cors", body).status_code == 200
    client = service[0]
    allowed = client.options("/cors-bucket/one.txt", headers={
        "Origin": "https://console.example.com",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "X-Amz-Date,X-Amz-Meta-Project",
    })
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://console.example.com"
    for origin in (
        "https://example.com",
        "https://console.example.com.evil.invalid",
        "http://console.example.com",
        "https://sub.deeper.example.com",
        "null",
    ):
        denied = client.options("/cors-bucket/one.txt", headers={
            "Origin": origin, "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Amz-Date",
        })
        assert denied.status_code == 403, origin


def test_preflight_rejects_header_abuse_and_does_not_authenticate_uploads(service):
    create_bucket(service)
    assert signed(service, "PUT", "/cors-bucket?cors", config_xml(
        origins=["*"], methods=["GET", "PUT"], headers=["*"]
    )).status_code == 200
    client, _, transport, _, _ = service
    huge = client.options("/cors-bucket/item", headers={
        "Origin": "https://client.example.net",
        "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": ",".join(f"X-Foo-{x}" for x in range(33)),
    })
    assert huge.status_code == 403
    before = transport.uploads
    # Origin and successful OPTIONS do not authorize a subsequent anonymous PUT.
    assert client.options("/cors-bucket/item", headers={
        "Origin": "https://client.example.net",
        "Access-Control-Request-Method": "PUT",
    }).status_code == 200
    direct = client.put("/cors-bucket/item", content=b"must-not-upload",
                        headers={"Origin": "https://client.example.net"})
    assert direct.status_code == 403
    assert before == transport.uploads
