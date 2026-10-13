#!/usr/bin/env python3
"""Disposable native PostgreSQL/MySQL dump/restore acceptance fixture.

CI only: a separate restored database is REQUIRED; no production restore,
mutation of existing data, or Telegram message deletion is performed.
The external pg_dump/pg_restore/mysqldump commands run in the CI workflow.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet
from sqlalchemy import func, select

from teledrive.db import connect_database
from teledrive.s3.credentials import S3CredentialStore
from teledrive.s3.tagging import ObjectTagStore
from teledrive.storage.bot_api_adapter import BotFileIndex, _bot_receipts
from teledrive.storage.catalog import BucketCatalog, removed_versions
from teledrive.storage.jobs import StorageUploadJobs
from teledrive.storage.manifest import ChunkReference, ManifestStore

# Test-only Fernet key, never a production secret.
TEST_MASTER_KEY = base64.urlsafe_b64encode(
    hashlib.sha256(b"TeleDrive disposable DR CI master key only").digest()
)
PAYLOAD = bytes(range(256)) * 32 + b"native-restore-tail"
S3_KEY = "dr/ünicode/multi-part.bin"


def fingerprint(url: str, receipt: dict) -> dict:
    """Read data through the actual production stores in a fresh DB session."""
    db = connect_database(url)
    try:
        catalog = BucketCatalog(ManifestStore(db))
        index = BotFileIndex(db)
        owner = receipt["bucket"]
        head = catalog.get(owner, S3_KEY)
        versions, truncated = catalog.list_bucket_versions(owner, limit=10)
        first = receipt["first"]
        pending = receipt["pending"]
        parts = catalog.manifests.parts(first)
        return {
            "head": head.item.id,
            "generation": head.generation,
            "head_sha": head.item.sha256,
            "versions": [(v["version_id"], v["is_current"], v["is_delete_marker"])
                         for v in versions],
            "truncated": truncated,
            "source_visible": catalog.is_visible_version(owner, S3_KEY, first),
            "deleted_version_hidden": not catalog.is_visible_version(
                owner, S3_KEY, receipt["second"]),
            "deleted_marker_hidden": not catalog.is_visible_version(
                owner, S3_KEY, receipt["marker"]),
            "removed_count": db.connection.execute(
                select(func.count()).select_from(removed_versions)
                .where(removed_versions.c.object_id.in_(
                    [receipt["marker"], receipt["second"]]
                ))
            ).scalar_one(),
            "parts": [(p.ordinal, p.offset, p.size, p.sha256,
                       p.account, p.chat_id, p.message_id) for p in parts],
            "tags": ObjectTagStore(catalog).get(first),
            "pending_ack": index.find_receipt(
                pending, 0, account="ci-dr-bot", chat_id=-100900901,
                checksum=hashlib.sha256(b"Q").hexdigest(), size=1,
            ).message_id,
            "unjournaled": index.unjournaled_count(),
            "job": next(
                {"id": j["id"], "state": j["state"], "attempts": j["attempts"]}
                for j in StorageUploadJobs(catalog).list_jobs(limit=1000)
                if j["id"] == receipt["job"]
            ),
            "s3_credential_decrypts": (
                S3CredentialStore(db, TEST_MASTER_KEY).resolve(
                    receipt["access_key"], "s3:GetObject"
                ) is not None
            ),
        }
    finally:
        db.close()


def seed(url: str, receipt_path: Path, source: Path):
    """Write realistic version history, chunk journal, ACK and queued job."""
    db = connect_database(url)
    bucket = "ci-dr-" + uuid4().hex[:19]
    message = int(uuid4().hex[:8], 16) % 1_800_000_000 + 1000
    now = int(time.time())
    receipt = {"bucket": bucket}
    try:
        catalog = BucketCatalog(ManifestStore(db))
        catalog.create_bucket(bucket, now)
        first = catalog.manifests.begin(bucket, S3_KEY, len(PAYLOAD), now)
        for index, part in enumerate((PAYLOAD[:8192], PAYLOAD[8192:])):
            catalog.manifests.append(
                first, ChunkReference(
                    ordinal=index, offset=index * 8192, size=len(part),
                    sha256=hashlib.sha256(part).hexdigest(),
                    account="ci-dr-bot", chat_id=-100900901,
                    message_id=message + index,
                ), now,
            )
        catalog.manifests.finish(first, hashlib.sha256(PAYLOAD).hexdigest(), now)
        catalog.publish(first, now=now)
        second = catalog.manifests.begin(bucket, S3_KEY, 0, now)
        catalog.manifests.finish(second, hashlib.sha256(b"").hexdigest(), now)
        catalog.publish(second, now=now)
        marker = catalog.delete_marker(bucket, S3_KEY)
        assert catalog.delete_version(bucket, S3_KEY, marker)
        assert catalog.delete_version(bucket, S3_KEY, second)
        ObjectTagStore(catalog).replace_current(
            bucket, S3_KEY, [("dr", "restored"), ("lang", "Türkçe")]
        )
        pending = catalog.manifests.begin(bucket, "ack-orphan", 1, now)
        BotFileIndex(db).put_receipt(
            "ci-dr-bot", -100900901, message + 3, "ci-test-file-id",
            object_id=pending, ordinal=0,
            checksum=hashlib.sha256(b"Q").hexdigest(), size=1,
        )
        source.write_bytes(b"durable-queued-source" * 13)
        job = StorageUploadJobs(catalog).enqueue(
            bucket, "queue/pending.bin", source,
            allowed_root=source.parent,
        )
        credential = S3CredentialStore(db, TEST_MASTER_KEY).create(
            "disposable-dr", ["s3:GetObject"]
        )
        receipt.update({
            "first": first, "second": second, "marker": marker,
            "pending": pending, "job": job, "access_key": credential.access_key,
        })
    finally:
        db.close()
    receipt["fingerprint"] = fingerprint(url, receipt)
    assert receipt["fingerprint"]["head"] == receipt["first"]
    assert receipt["fingerprint"]["head_sha"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert receipt["fingerprint"]["removed_count"] == 2
    assert len(receipt["fingerprint"]["parts"]) == 2
    assert receipt["fingerprint"]["unjournaled"] == 1
    assert receipt["fingerprint"]["s3_credential_decrypts"]
    # Durable test receipt contains only synthetic CI IDs and no secret keys.
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")


def verify(url: str, receipt_path: Path):
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    restored = fingerprint(url, receipt)
    assert restored == receipt["fingerprint"], {
        "expected": receipt["fingerprint"], "restored": restored,
    }
    print(json.dumps({
        "restore_valid": True,
        "backend": url.split(":", 1)[0],
        "chunk_refs": len(restored["parts"]),
        "removed_versions": restored["removed_count"],
        "unjournaled_receipts": restored["unjournaled"],
        "queue_state": restored["job"]["state"],
        "s3_credential_decrypts": restored["s3_credential_decrypts"],
        "physical_telegram_delete": False,
    }, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("seed", "verify"))
    parser.add_argument("--url-env", required=True,
                        choices=("DB_TEST_POSTGRES_URL", "DB_TEST_MYSQL_URL"))
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--restore-database")
    args = parser.parse_args()
    url = os.environ[args.url_env]
    if args.phase == "seed":
        if args.source is None:
            parser.error("seed requires --source")
        seed(url, args.receipt, args.source)
    else:
        if not args.restore_database or args.restore_database == "teledrive":
            parser.error("verify must target a separate, nonproduction database")
        from sqlalchemy.engine.url import make_url
        url = str(make_url(url).set(database=args.restore_database))
        verify(url, args.receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
