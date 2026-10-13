#!/usr/bin/env python3
"""Read-only check of SQLite metadata snapshots before a staging restore."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote


class InvalidSnapshot(RuntimeError):
    pass


REQUIRED = ("storage_objects", "storage_chunks", "storage_object_heads",
            "storage_object_aliases", "storage_published_versions")


def verify_snapshot(path: Path, expected_sha256: str | None = None) -> dict:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise InvalidSnapshot("Snapshot must be an existing regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    sha = digest.hexdigest()
    if expected_sha256 is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise InvalidSnapshot("Expected SHA-256 must be 64 hex digits")
        if not hmac.compare_digest(sha, expected_sha256.lower()):
            raise InvalidSnapshot("Snapshot SHA-256 receipt mismatch")
    try:
        uri = "file:" + quote(str(path.resolve(strict=True)), safe="/") + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=30) as db:
            if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise InvalidSnapshot("SQLite integrity_check failed")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise InvalidSnapshot("Snapshot has dangling foreign keys")
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if set(REQUIRED) - tables:
                raise InvalidSnapshot("TeleDrive storage schema is incomplete")
            for key_hash, key, bucket, original_bucket, status in db.execute(
                "SELECT h.key_hash,o.object_key,h.bucket,o.bucket,o.status "
                "FROM storage_object_heads h LEFT JOIN storage_objects o "
                "ON o.id=h.object_id"
            ):
                if (status not in ("ready", "delete-marker") or original_bucket != bucket
                        or hashlib.sha256(key.encode()).hexdigest() != key_hash):
                    raise InvalidSnapshot("Current HEAD manifest is inconsistent")
            for key_hash, key, bucket, original_bucket, status in db.execute(
                "SELECT v.key_hash,o.object_key,v.bucket,o.bucket,o.status "
                "FROM storage_published_versions v LEFT JOIN storage_objects o "
                "ON o.id=v.object_id"
            ):
                if (status not in ("ready", "delete-marker") or original_bucket != bucket
                        or hashlib.sha256(key.encode()).hexdigest() != key_hash):
                    raise InvalidSnapshot("Published version metadata is inconsistent")
            def count(table):
                return int(db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0])
            summary = {
                "valid": True, "sha256": sha,
                "objects": count("storage_objects"),
                "heads": count("storage_object_heads"),
                "versions": count("storage_published_versions"),
                "telegram_chunk_refs": count("storage_chunks"),
                "physical_deletion_performed": False,
            }
            return summary
    except sqlite3.DatabaseError as exc:
        raise InvalidSnapshot("Unreadable or corrupt SQLite image") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a staging SQLite snapshot")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--sha256", help="Expected SHA-256 from backup receipt")
    args = parser.parse_args()
    try:
        print(json.dumps(verify_snapshot(args.database, args.sha256), sort_keys=True))
    except (InvalidSnapshot, OSError) as exc:
        parser.exit(1, "Invalid snapshot: " + str(exc) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
