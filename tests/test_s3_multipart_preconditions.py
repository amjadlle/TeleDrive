"""Conditional CompleteMultipartUpload: exact HEAD CAS, signed headers and retries.

The upload writes to Telegram before Complete, but completing under a stale
If-Match/If-None-Match must not take ownership of a different object's head.
"""
from __future__ import annotations

import hashlib
import time
from xml.etree import ElementTree as ET

from test_s3_gateway import service, signed
from test_s3_multipart import setup, begin, part, xml_text
from teledrive.storage.engine import TelegramObjectStorage
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore

def complete_conditional(svc, upload_id, selected, **headers):
    root = ET.Element("CompleteMultipartUpload")
    for number, etag in selected:
        row = ET.SubElement(root, "Part")
        ET.SubElement(row, "PartNumber").text = str(number)
        ET.SubElement(row, "ETag").text = etag
    return signed(svc, "POST",
        f"/backup-bucket/asset.bin?uploadId={upload_id}",
        ET.tostring(root), extra_headers={
            k.replace("_", "-"): v for k, v in headers.items()
        },
    )


def test_if_none_match_complete_is_one_time_and_keeps_parts_retryable(service):
    setup(service)
    uid = begin(service)
    payload = b"conditioned-telegram-multipart"
    etag = part(service, uid, 1, payload)
    baseline = service[2].uploads
    first = complete_conditional(service, uid, [(1, etag)], If_None_Match="*")
    assert first.status_code == 200, first.text
    assert service[2].uploads == baseline
    assert signed(service, "GET", "/backup-bucket/asset.bin").content == payload
    assert complete_conditional(
        service, uid, [(1, etag)], If_None_Match="*"
    ).status_code == 412

    second_id = begin(service)
    second_etag = part(service, second_id, 1, b"second-staged")
    uploads = service[2].uploads
    conflict = complete_conditional(
        service, second_id, [(1, second_etag)], If_None_Match="*"
    )
    assert conflict.status_code == 412, conflict.text
    assert xml_text(conflict, "Code") == "PreconditionFailed"
    assert signed(service, "GET", "/backup-bucket/asset.bin").content == payload
    assert service[2].uploads == uploads


def test_if_match_complete_fences_inflight_head_change(service, monkeypatch):
    setup(service)
    old = signed(service, "PUT", "/backup-bucket/asset.bin", b"old-head")
    assert old.status_code == 200
    uid = begin(service)
    payload = b"staged-multipart-object"
    etag = part(service, uid, 1, payload)
    prior_uploads = service[2].uploads
    initial_etag = old.headers["etag"]
    original = TelegramObjectStorage.iter_bytes
    intrusions = []

    async def concurrently_publish(self, identifier, **kwargs):
        if not intrusions:
            connection = service[3]()
            try:
                catalog = BucketCatalog(ManifestStore(connection))
                now = int(time.time())
                winner = catalog.manifests.begin(
                    "backup-bucket", "asset.bin", 0, now,
                )
                catalog.manifests.finish(winner, hashlib.sha256(b"").hexdigest(), now)
                catalog.publish(winner, now=now)
                intrusions.append(winner)
            finally:
                connection.close()
        async for data in original(self, identifier, **kwargs):
            yield data

    monkeypatch.setattr(TelegramObjectStorage, "iter_bytes", concurrently_publish)
    stale = complete_conditional(service, uid, [(1, etag)], If_Match=initial_etag)
    assert intrusions
    assert stale.status_code == 412, stale.text
    assert xml_text(stale, "Code") == "PreconditionFailed"
    assert service[2].uploads == prior_uploads
    monkeypatch.setattr(TelegramObjectStorage, "iter_bytes", original)
    assert signed(service, "GET", "/backup-bucket/asset.bin").content == b""

    # Staged media is not thrown away by the failed CAS; a fresh signed
    # condition can safely Complete without a new Telegram upload.
    new_etag = signed(service, "HEAD", "/backup-bucket/asset.bin").headers["etag"]
    done = complete_conditional(service, uid, [(1, etag)], If_Match=new_etag)
    assert done.status_code == 200, done.text
    assert service[2].uploads == prior_uploads
    assert signed(service, "GET", "/backup-bucket/asset.bin").content == payload


def test_unsigned_completion_precondition_is_rejected(service):
    from botocore.auth import S3SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    setup(service)
    uid = begin(service)
    etag = part(service, uid, 1, b"safe")
    request_path = f"/backup-bucket/asset.bin?uploadId={uid}"
    xml = ET.tostring(ET.Element("CompleteMultipartUpload"))
    req = AWSRequest(
        method="POST", url="http://localhost:9000" + request_path,
        data=xml,
        headers={"x-amz-content-sha256": hashlib.sha256(xml).hexdigest()},
    )
    req.context["payload_signing_enabled"] = True
    S3SigV4Auth(
        Credentials(service[1].access_key, service[1].secret_key),
        "s3", "us-east-1",
    ).add_auth(req)
    altered = dict(req.headers)
    altered["If-None-Match"] = "*"
    response = service[0].post(request_path, content=xml, headers=altered)
    assert response.status_code == 403
    assert b"AccessDenied" in response.content
    assert service[2].uploads == 1
