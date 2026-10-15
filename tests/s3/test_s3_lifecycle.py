"""S3 lifecycle aborts only old multipart sessions, retaining remote media."""
from __future__ import annotations
import time
from xml.etree import ElementTree as ET
import pytest
from tests.s3.test_s3_gateway import service, signed
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore
from teledrive.s3.multipart import MultipartStore
from teledrive.s3.lifecycle import BucketLifecycleStore
from teledrive.s3.credentials import S3CredentialStore


def xml(days=7, status="Enabled", prefix="upload/"):
    root = ET.Element("LifecycleConfiguration")
    rule = ET.SubElement(root, "Rule")
    ET.SubElement(rule, "ID").text = "abort-only"
    ET.SubElement(rule, "Status").text = status
    filt = ET.SubElement(rule, "Filter")
    ET.SubElement(filt, "Prefix").text = prefix
    act = ET.SubElement(rule, "AbortIncompleteMultipartUpload")
    ET.SubElement(act, "DaysAfterInitiation").text = str(days)
    return ET.tostring(root)


def test_s3_lifecycle_crud_and_old_mpu_only(service):
    assert signed(service, "PUT", "/lifecycle-ci").status_code == 200
    target = "/lifecycle-ci?lifecycle"
    assert signed(service, "GET", target).status_code == 404
    assert signed(service, "PUT", target, xml()).status_code == 200
    response = signed(service, "GET", target)
    assert response.status_code == 200 and b"DaysAfterInitiation" in response.content
    db = service[3]()
    try:
        cat = BucketCatalog(ManifestStore(db))
        mpu = MultipartStore(cat)
        now = int(time.time())
        aged = mpu.initiate("lifecycle-ci", "upload/old", "owner", now=now-9*86400)
        fresh = mpu.initiate("lifecycle-ci", "upload/new", "owner", now=now)
        other = mpu.initiate("lifecycle-ci", "other/old", "owner", now=now-9*86400)
        lifecycle = BucketLifecycleStore(cat)
        assert lifecycle.apply_due(now=now) == 1
        assert lifecycle.apply_due(now=now) == 0
        active = {r.id for r in mpu.list_uploads("lifecycle-ci", "owner")[0]}
        assert aged not in active and {fresh, other} <= active
    finally:
        db.close()
    assert service[2].uploads == service[2].downloads == 0
    assert signed(service, "DELETE", target).status_code == 204
    assert signed(service, "GET", target).status_code == 404


@pytest.mark.parametrize("bad,expected", [
    (b"<LifecycleConfiguration/>", 400), (xml(days=0), 400),
    (xml(days=3651), 400),
    (xml().replace(b"AbortIncompleteMultipartUpload", b"Expiration"), 501),
    (xml().replace(b"Filter", b"And"), 501),
    (b'<!DOCTYPE x [<!ENTITY a "7">]><LifecycleConfiguration>&a;</LifecycleConfiguration>', 400),
])
def test_unimplemented_actions_never_mutate_rule(service, bad, expected):
    assert signed(service, "PUT", "/lifecycle-ci").status_code == 200
    path = "/lifecycle-ci?lifecycle"
    assert signed(service, "PUT", path, xml()).status_code == 200
    res = signed(service, "PUT", path, bad)
    assert res.status_code == expected
    assert signed(service, "GET", path).status_code == 200


def test_disabled_rule_and_permission_isolation(service):
    assert signed(service, "PUT", "/lifecycle-ci").status_code == 200
    path = "/lifecycle-ci?lifecycle"
    assert signed(service, "PUT", path, xml(status="Disabled", days=1)).status_code == 200
    db = service[3]()
    try:
        cat = BucketCatalog(ManifestStore(db))
        mpu = MultipartStore(cat)
        ident = mpu.initiate("lifecycle-ci", "upload/old", "owner",
                             now=int(time.time()) - 20*86400)
        assert BucketLifecycleStore(cat).apply_due(now=int(time.time())) == 0
        assert ident in {r.id for r in mpu.list_uploads("lifecycle-ci", "owner")[0]}
        auth = S3CredentialStore(db, service[4])
        read = auth.create("lifecycle-read", ["s3:GetLifecycleConfiguration"])
        write = auth.create("lifecycle-write", ["s3:PutLifecycleConfiguration"])
    finally:
        db.close()
    assert signed(service, "PUT", path, xml(), access=read.access_key,
                  secret=read.secret_key).status_code == 403
    assert signed(service, "PUT", path, xml(), access=write.access_key,
                  secret=write.secret_key).status_code == 200
    assert signed(service, "GET", path, access=write.access_key,
                  secret=write.secret_key).status_code == 403
    assert signed(service, "GET", path, access=read.access_key,
                  secret=read.secret_key).status_code == 200


def test_real_boto3_lifecycle_control_api(service):
    import boto3, socket, threading, uvicorn
    from botocore.config import Config
    import web.s3 as gateway
    assert signed(service, "PUT", "/lifecycle-ci").status_code == 200
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(gateway.app, host="127.0.0.1",
                    port=port, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets":[sock]}, daemon=True)
    thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline:
            time.sleep(.02)
        assert server.started
        cred=service[1]
        s3=boto3.client("s3", endpoint_url=f"http://127.0.0.1:{port}",
            aws_access_key_id=cred.access_key, aws_secret_access_key=cred.secret_key,
            region_name="us-east-1", config=Config(signature_version="s3v4",
                                                   s3={"addressing_style":"path"}))
        assert s3.put_bucket_lifecycle_configuration(
            Bucket="lifecycle-ci", LifecycleConfiguration={"Rules":[{
                "ID":"boto3", "Status":"Enabled", "Filter":{"Prefix":"upload/"},
                "AbortIncompleteMultipartUpload":{"DaysAfterInitiation":5},
            }]}
        )["ResponseMetadata"]["HTTPStatusCode"] == 200
        rules=s3.get_bucket_lifecycle_configuration(Bucket="lifecycle-ci")["Rules"]
        assert rules[0]["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 5
        assert s3.delete_bucket_lifecycle(Bucket="lifecycle-ci")["ResponseMetadata"]["HTTPStatusCode"] == 204
    finally:
        server.should_exit=True
        thread.join(timeout=10)


def test_aborted_multipart_retains_original_telegram_part_and_manifest(service):
    """Lifecycle revokes a session but preserves immutable remote references."""
    import hashlib
    from sqlalchemy import func, select
    from teledrive.storage.manifest import ChunkReference, chunks, objects
    from teledrive.s3.multipart import parts as multipart_parts, uploads

    assert signed(service, "PUT", "/lifecycle-ci").status_code == 200
    assert signed(service, "PUT", "/lifecycle-ci?lifecycle",
                  xml(days=1)).status_code == 200
    db = service[3]()
    try:
        cat = BucketCatalog(ManifestStore(db))
        now = int(time.time())
        old = now - 14 * 86400
        payload = b"immutable multipart reference"
        digest = hashlib.sha256(payload).hexdigest()
        identifier = cat.manifests.begin("lifecycle-ci", "upload/staged", len(payload), old)
        cat.manifests.append(identifier, ChunkReference(
            ordinal=0, offset=0, size=len(payload), sha256=digest,
            account="ci-original", chat_id=-100666555444, message_id=901,
        ), old)
        cat.manifests.finish(identifier, digest, old)
        multipart = MultipartStore(cat)
        upload_id = multipart.initiate("lifecycle-ci", "upload/staged", "owner", now=old)
        multipart.record_part(
            upload_id, "lifecycle-ci", "upload/staged", "owner",
            number=1, object_id=identifier,
            etag=hashlib.md5(payload, usedforsecurity=False).hexdigest(),
        )
        lifecycle = BucketLifecycleStore(cat)
        assert lifecycle.apply_due(now=now) == 1
        assert db.connection.execute(select(uploads.c.state).where(
            uploads.c.id == upload_id)).scalar_one() == "aborted"
        assert db.connection.execute(select(func.count()).select_from(
            multipart_parts).where(multipart_parts.c.upload_id == upload_id)).scalar_one() == 1
        assert db.connection.execute(select(func.count()).select_from(
            chunks).where(chunks.c.object_id == identifier)).scalar_one() == 1
        assert db.connection.execute(select(objects.c.status).where(
            objects.c.id == identifier)).scalar_one() == "ready"
        assert cat.manifests.get(identifier).sha256 == digest
        assert cat.manifests.parts(identifier)[0].message_id == 901
        assert lifecycle.apply_due(now=now) == 0
    finally:
        db.close()
    assert service[2].uploads == service[2].downloads == 0


def test_bucket_delete_cleans_lifecycle_config_before_bucket_recreation(service):
    assert signed(service, "PUT", "/empty-lifecycle").status_code == 200
    assert signed(service, "PUT", "/empty-lifecycle?lifecycle", xml()).status_code == 200
    assert signed(service, "DELETE", "/empty-lifecycle").status_code == 204
    assert signed(service, "PUT", "/empty-lifecycle").status_code == 200
    assert signed(service, "GET", "/empty-lifecycle?lifecycle").status_code == 404
