"""Reconstruct ONE provable Telegram object into a NEW isolated SQLite catalog.

No upload or remote deletion. Historical key, bytes, and chunk ownership
must be supplied independently: Telegram captions alone cannot prove a key.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from pathlib import Path
from uuid import UUID

from sqlalchemy import insert

from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.manifest import (
    ChunkReference, ManifestStore, chunks, objects,
)

_SHA = re.compile(r"[0-9a-f]{64}\\Z")


def validate_plan(plan: dict) -> tuple[list[ChunkReference], int]:
    if not isinstance(plan, dict) or set(plan) != {
        "object_id", "bucket", "key", "sha256", "size", "chunks",
    }:
        raise ValueError("Recovery plan must contain exact original identities")
    if str(UUID(plan["object_id"])) != plan["object_id"]:
        raise ValueError("Invalid original object UUID")
    BucketCatalog.validate_bucket(plan["bucket"])
    BucketCatalog._keyhash(plan["key"])
    if not isinstance(plan["sha256"], str) or not _SHA.fullmatch(plan["sha256"]):
        raise ValueError("Expected independent full-file SHA-256 is required")
    size = plan["size"]
    if type(size) is not int or size < 0 or size > 1024**3:
        raise ValueError("Recovery size must be within the one GiB verification cap")
    entries = plan["chunks"]
    if not isinstance(entries, list) or len(entries) > 4096:
        raise ValueError("Invalid chunk inventory")
    if bool(size) != bool(entries):
        raise ValueError("Empty files cannot have remote chunks")
    references = []
    offset = 0
    identities = set()
    for position, item in enumerate(entries):
        if not isinstance(item, dict) or set(item) != {
            "ordinal", "account", "chat_id", "message_id", "size", "sha256",
        }:
            raise ValueError("Incomplete original Telegram chunk identity")
        if (type(item["ordinal"]) is not int or item["ordinal"] != position
                or type(item["chat_id"]) is not int
                or type(item["message_id"]) is not int
                or item["message_id"] <= 0
                or not isinstance(item["account"], str)
                or not item["account"] or len(item["account"]) > 255
                or type(item["size"]) is not int or item["size"] <= 0
                or not isinstance(item["sha256"], str)
                or not _SHA.fullmatch(item["sha256"])):
            raise ValueError("Invalid Telegram chunk reference")
        identity = (item["account"], item["chat_id"], item["message_id"])
        if identity in identities:
            raise ValueError("Duplicate Telegram message identity")
        identities.add(identity)
        references.append(ChunkReference(
            ordinal=position, offset=offset, size=item["size"],
            sha256=item["sha256"], account=item["account"],
            chat_id=item["chat_id"], message_id=item["message_id"],
        ))
        offset += item["size"]
        if offset > size:
            raise ValueError("Manifest chunks exceed verified object size")
    if offset != size:
        raise ValueError("Chunk offsets do not cover the complete original file")
    return references, size


async def restore_isolated_catalog(
    plan: dict, transport, destination: Path,
) -> dict:
    """Verify all Telegram payload bytes BEFORE opening a new SQL catalog."""
    refs, size = validate_plan(plan)
    destination = Path(destination)
    if destination.is_symlink() or destination.exists() or not destination.parent.is_dir():
        raise FileExistsError("Refuse to replace an existing or unsafe database")
    digest = hashlib.sha256()
    count = 0
    with tempfile.TemporaryDirectory(prefix="teledrive-rebuild-") as tmp:
        for ref in refs:
            chunk_path = Path(tmp) / f"chunk-{ref.ordinal}"
            await transport.download(ref, chunk_path)
            if not chunk_path.is_file() or chunk_path.is_symlink():
                raise ValueError("Telegram did not produce a regular file")
            h = hashlib.sha256()
            read = 0
            with chunk_path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    read += len(block)
                    if read > ref.size:
                        raise ValueError("Unexpected extra Telegram bytes")
                    h.update(block)
                    digest.update(block)
            if read != ref.size or h.hexdigest() != ref.sha256:
                raise ValueError("Telegram chunk hash/length mismatch")
            count += read
            chunk_path.unlink()
        if count != size or digest.hexdigest() != plan["sha256"]:
            raise ValueError("Original Telegram payload does not match external SHA")
    # Exclusive create after successful byte-level verification. Keep the
    # database in a new location: never overwrite a production SQL catalog.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    db = None
    try:
        db = connect_database(str(destination))
        manifests = ManifestStore(db)
        catalog = BucketCatalog(manifests)
        now = int(time.time())
        catalog.create_bucket(plan["bucket"], now)
        db.connection.execute(insert(objects).values(
            id=plan["object_id"], bucket=plan["bucket"],
            object_key=plan["key"], status="ready", size=size,
            sha256=plan["sha256"], created_at=now, updated_at=now, error=None,
        ))
        if refs:
            db.connection.execute(insert(chunks), [
                dict(object_id=plan["object_id"], ordinal=r.ordinal,
                     offset=r.offset, size=r.size, sha256=r.sha256,
                     account=r.account, chat_id=r.chat_id,
                     message_id=r.message_id)
                for r in refs
            ])
        db.commit()
        catalog.publish(plan["object_id"], now=now, expected_generation=0)
        assert catalog.get(plan["bucket"], plan["key"]).item.sha256 == plan["sha256"]
    except Exception:
        if db is not None:
            db.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        db.close()
    return {
        "verified": True, "isolated_database_created": True,
        "object_id": plan["object_id"], "size": size,
        "sha256": digest.hexdigest(), "chunks": len(refs),
        "telegram_uploads": 0, "telegram_deletions": 0,
        "production_database_modified": False,
        "historic_versions_reconstructed": False,
    }
