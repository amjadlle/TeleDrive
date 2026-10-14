"""One running TeleDrive instance: REST, SigV4 S3, and local jobs share bytes.

All writes target the same actual SQLite catalog and fake Telegram Bot
transport. Socket-port dispatch preserves unmodified S3 signed paths.
"""
from __future__ import annotations

import hashlib
import time

import pytest
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import web.app as web
import web.s3 as s3
from web.unified import UnifiedApplication, app
from test_rest_transfer import FakeTelegramBot
from teledrive.db import connect_database
from teledrive.s3.credentials import S3CredentialStore, DEFAULT_PERMISSIONS
from teledrive.storage.bot_api_adapter import BotApiChunkTransport, BotFileIndex
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import ManifestStore


@pytest.fixture
def unified(tmp_path, monkeypatch):
    path=tmp_path/"unified.sqlite"
    telegram=FakeTelegramBot()
    secret=Fernet.generate_key()
    connect=lambda: connect_database(str(path))
    transport=lambda db: BotApiChunkTransport(
        telegram, BotFileIndex(db), chat_id=-1001234567890,
        account="api-bot", min_upload_interval=0,
    )
    for module in (web,s3):
        monkeypatch.setattr(module,"db_connect",connect)
        monkeypatch.setattr(module,"storage_bot_transport",transport)
    monkeypatch.setattr(s3,"load_master_key",lambda: secret)
    monkeypatch.setattr(web,"WEB_USER","testadmin")
    monkeypatch.setattr(web,"WEB_PASSWORD","long-secure-test-password")
    web.AUTH_FAILURES.clear()
    monkeypatch.setenv("TELEDRIVE_STORAGE_SCRATCH_DIR",str(tmp_path/"scratch"))
    monkeypatch.setenv("TELEDRIVE_REST_MAX_UPLOAD_BYTES",str(4*1024*1024))
    root=tmp_path/"uploads"
    root.mkdir()
    monkeypatch.setenv("TELEDRIVE_JOB_SOURCE_ROOT",str(root))
    db=connect()
    try:
        creds=S3CredentialStore(db,secret).create("unified",list(DEFAULT_PERMISSIONS))
    finally:
        db.close()
    web_client=TestClient(app,base_url="http://localhost:8080")
    s3_client=TestClient(app,base_url="http://localhost:9000")
    try:
        result=web_client.post("/api/v1/admin/keys",
            auth=("testadmin","long-secure-test-password"),
            headers={"X-TeleDrive-CSRF":"1"},
            json={"name":"unified-rest","scopes":[
                "buckets:read","buckets:manage","objects:read",
                "objects:write","objects:delete",
            ]})
        assert result.status_code==201,result.text
        auth={"Authorization":"Bearer "+result.json()["token"]}
        bucket=web_client.post("/api/v1/buckets",
                               headers=auth,json={"name":"unified-bucket"})
        assert bucket.status_code==201,bucket.text
        yield web_client,s3_client,auth,creds,telegram,root,connect
    finally:
        web_client.close()
        s3_client.close()


def signed(s3_client,creds,method,path,content=b""):
    sha=hashlib.sha256(content).hexdigest()
    headers={"x-amz-content-sha256":sha}
    aws=AWSRequest(method=method,url="http://localhost:9000"+path,
                   data=content,headers=headers)
    S3SigV4Auth(Credentials(creds.access_key,creds.secret_key),
                "s3","us-east-1").add_auth(aws)
    return s3_client.request(method,path,content=content,headers=dict(aws.headers))


def test_single_process_listener_separation_and_health(unified):
    ui,gateway,*_=unified
    assert ui.get("/api/health").json()=={"ok":True}
    assert gateway.get("/__health").json()=={"ok":True}
    assert ui.get("/__health").status_code==404
    assert gateway.get("/api/health").status_code!=200
    assert ui.get("/api/config").status_code==401
    assert gateway.get("/").status_code in (400,403)
    with pytest.raises(ValueError):
        UnifiedApplication(web_port=8080,s3_port=8080)


def test_rest_s3_and_local_file_publish_one_catalog_and_shared_generations(unified):
    ui,gateway,auth,creds,bot,root,connect=unified
    rest_data=b"written-via-REST-"*17
    rest=ui.put("/api/v1/buckets/unified-bucket/content",
                params={"key":"rest.bin"},headers={**auth,"Content-Type":"application/octet-stream"},
                content=rest_data)
    assert rest.status_code==201,rest.text
    s3_read=signed(gateway,creds,"GET","/unified-bucket/rest.bin")
    assert s3_read.status_code==200,s3_read.text
    assert s3_read.content==rest_data

    s3_data=b"written-via-S3-"*39
    remote=signed(gateway,creds,"PUT","/unified-bucket/s3.bin",s3_data)
    assert remote.status_code==200,remote.text
    local=ui.get("/api/v1/buckets/unified-bucket/content",
                 params={"key":"s3.bin"},headers=auth)
    assert local.status_code==200,local.text
    assert local.content==s3_data

    original=root/"folder-upload.bin"
    folder_data=b"local-queue-same-storage"*19
    original.write_bytes(folder_data)
    enq=ui.post("/api/v1/admin/storage/jobs",
                auth=("testadmin","long-secure-test-password"),
                headers={"X-TeleDrive-CSRF":"1"},
                json={"bucket":"unified-bucket","key":"folder/file.bin",
                      "source_path":str(original)})
    assert enq.status_code==201,enq.text
    processed=ui.post("/api/v1/admin/storage/jobs/run-next",
                auth=("testadmin","long-secure-test-password"),
                headers={"X-TeleDrive-CSRF":"1"})
    assert processed.status_code==200,processed.text
    assert processed.json()["job"]["state"]=="done"
    folder_read=signed(gateway,creds,"GET","/unified-bucket/folder/file.bin")
    assert folder_read.status_code==200,folder_read.text
    assert folder_read.content==folder_data

    # The next S3 write updates the EXACT same REST head/generation.
    replacement=b"s3-overwriting-rest"
    overwrite=signed(gateway,creds,"PUT","/unified-bucket/rest.bin",replacement)
    assert overwrite.status_code==200,overwrite.text
    after=ui.get("/api/v1/buckets/unified-bucket/content",
                 params={"key":"rest.bin"},headers=auth)
    assert after.content==replacement
    assert after.headers["etag"]=='"'+hashlib.sha256(replacement).hexdigest()+'"'
    db=connect()
    try:
        catalog=BucketCatalog(ManifestStore(db))
        assert catalog.get("unified-bucket","rest.bin").generation==2
        assert catalog.get("unified-bucket","s3.bin").item.sha256==hashlib.sha256(s3_data).hexdigest()
        assert catalog.get("unified-bucket","folder/file.bin").item.sha256==hashlib.sha256(folder_data).hexdigest()
        assert len(catalog.list_buckets())==1
    finally:
        db.close()
    assert bot.counter>=4
