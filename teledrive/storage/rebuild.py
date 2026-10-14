"""Recover independently proven Telegram objects into one isolated SQL catalog.

Strict P2 recovery: original messages + signed/out-of-band manifest mapping
are required. This module never sends or deletes Telegram media, nor modifies
the original SQL database. A batch becomes visible only after all objects
pass remote identity, per-chunk SHA-256 and whole-object SHA-256 validation.
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
from teledrive.storage.recovery import RemoteChunk
from teledrive.storage.manifest import (
    ChunkReference, ManifestStore, chunks, objects,
)

_SHA = re.compile(r"[0-9a-f]{64}\Z")
MAX_BATCH_OBJECTS = 128
MAX_BATCH_CHUNKS = 4096
MAX_BATCH_BYTES = 1024**3


def validate_plan(plan: dict) -> tuple[list[ChunkReference], int]:
    if not isinstance(plan, dict) or set(plan) != {
        "object_id", "bucket", "key", "sha256", "size", "chunks",
    }:
        raise ValueError("Recovery plan must contain exact original identities")
    if not isinstance(plan["object_id"], str) or str(UUID(plan["object_id"])) != plan["object_id"]:
        raise ValueError("Invalid original object UUID")
    BucketCatalog.validate_bucket(plan["bucket"])
    BucketCatalog._keyhash(plan["key"])
    if not isinstance(plan["sha256"], str) or not _SHA.fullmatch(plan["sha256"]):
        raise ValueError("Expected independent full-file SHA-256 is required")
    size = plan["size"]
    if type(size) is not int or size < 0 or size > MAX_BATCH_BYTES:
        raise ValueError("Recovery size must be within the one GiB verification cap")
    entries = plan["chunks"]
    if not isinstance(entries, list) or len(entries) > MAX_BATCH_CHUNKS:
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


def validate_batch(plans: list[dict]) -> list[tuple[dict, list[ChunkReference]]]:
    """Reject ambiguous cross-object identities before any remote IO."""
    if not isinstance(plans, list) or not 1 <= len(plans) <= MAX_BATCH_OBJECTS:
        raise ValueError("Recovery batch must contain 1..128 original objects")
    seen_ids = set()
    seen_heads = set()
    seen_messages = set()
    total_bytes = 0
    total_chunks = 0
    validated = []
    for plan in plans:
        refs, size = validate_plan(plan)
        object_id = plan["object_id"]
        head = (plan["bucket"], plan["key"])
        if object_id in seen_ids or head in seen_heads:
            raise ValueError("Duplicate object ID or published bucket/key")
        seen_ids.add(object_id)
        seen_heads.add(head)
        total_bytes += size
        total_chunks += len(refs)
        if total_bytes > MAX_BATCH_BYTES or total_chunks > MAX_BATCH_CHUNKS:
            raise ValueError("Recovery batch exceeds verification resource bounds")
        for ref in refs:
            identity = (ref.account, ref.chat_id, ref.message_id)
            if identity in seen_messages:
                raise ValueError("Telegram message referenced by multiple objects")
            seen_messages.add(identity)
        validated.append((plan, refs))
    return validated


async def _verify_remote_object(plan: dict, refs: list[ChunkReference],
                                transport, scratch: Path) -> None:
    if not callable(getattr(transport, "probe", None)):
        raise ValueError("Original Telegram ownership probe is required")
    digest = hashlib.sha256()
    count = 0
    for ref in refs:
        proof = await transport.probe(RemoteChunk(
            ref.account, ref.chat_id, ref.message_id, ref.ordinal,
            ref.sha256[:16],
        ), object_id=plan["object_id"])
        if proof != "verified":
            raise ValueError("Original Telegram message ownership unproven")
        target = scratch / f"part-{ref.ordinal}"
        await transport.download(ref, target)
        if not target.is_file() or target.is_symlink():
            raise ValueError("Telegram did not produce a regular file")
        part_digest = hashlib.sha256()
        read = 0
        with target.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                read += len(block)
                if read > ref.size:
                    raise ValueError("Unexpected extra Telegram bytes")
                part_digest.update(block)
                digest.update(block)
        if read != ref.size or part_digest.hexdigest() != ref.sha256:
            raise ValueError("Telegram chunk hash/length mismatch")
        count += read
        target.unlink()
    if count != plan["size"] or digest.hexdigest() != plan["sha256"]:
        raise ValueError("Original Telegram payload does not match external SHA")


async def restore_isolated_catalog_batch(plans: list[dict], transport,
                                         destination: Path) -> dict:
    """Verify every remote object, then atomically publish a new SQLite file.

    An O_EXCL-style hard link makes the result appear all-or-nothing and never
    overwrites an existing path. SQLite is first closed and verified while
    still private. The temporary DB is on the same filesystem as destination.
    """
    validated = validate_batch(plans)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
        raise FileExistsError("Refuse to replace an existing or unsafe database")
    with tempfile.TemporaryDirectory(
        prefix=".teledrive-rebuild-", dir=destination.parent,
    ) as temp:
        scratch = Path(temp)
        for plan, refs in validated:
            await _verify_remote_object(plan, refs, transport, scratch)
        staging = scratch / "recovered.sqlite"
        db = connect_database(str(staging))
        try:
            manifests = ManifestStore(db)
            catalog = BucketCatalog(manifests)
            now = int(time.time())
            for bucket in sorted({p["bucket"] for p, _ in validated}):
                catalog.create_bucket(bucket, now)
            for plan, refs in validated:
                db.connection.execute(insert(objects).values(
                    id=plan["object_id"], bucket=plan["bucket"],
                    object_key=plan["key"], status="ready",
                    size=plan["size"], sha256=plan["sha256"],
                    created_at=now, updated_at=now, error=None,
                ))
                if refs:
                    db.connection.execute(insert(chunks), [
                        dict(object_id=plan["object_id"], ordinal=ref.ordinal,
                             offset=ref.offset, size=ref.size, sha256=ref.sha256,
                             account=ref.account, chat_id=ref.chat_id,
                             message_id=ref.message_id)
                        for ref in refs
                    ])
                db.commit()
                catalog.publish(plan["object_id"], now=now, expected_generation=0)
            # Re-check the staged SQL structure and all restored current heads
            # before the file can be opened by an operator.
            for plan, refs in validated:
                item = catalog.get(plan["bucket"], plan["key"]).item
                if (item.id != plan["object_id"]
                        or item.size != plan["size"]
                        or item.sha256 != plan["sha256"]
                        or catalog.manifests.parts(item.id) != refs):
                    raise ValueError("Staged recovery manifest differs from source proof")
        finally:
            db.close()
        # Verify the SQLite file itself before publishing a pathname.
        import sqlite3
        with sqlite3.connect(f"file:{staging}?mode=ro", uri=True) as audit:
            if audit.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Staging SQLite integrity check failed")
        try:
            os.link(staging, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise FileExistsError("An existing database must not be overwritten") from exc
        os.chmod(destination, 0o600)
    return {
        "verified": True, "isolated_database_created": True,
        "object_count": len(validated),
        "object_ids": [plan["object_id"] for plan, _ in validated],
        "verified_bytes": sum(plan["size"] for plan, _ in validated),
        "verified_chunks": sum(len(refs) for _, refs in validated),
        "telegram_uploads": 0, "telegram_deletions": 0,
        "production_database_modified": False,
        "historic_versions_reconstructed": False,
        "complete_unmapped_history_discovered": False,
    }


async def restore_isolated_catalog(plan: dict, transport, destination: Path) -> dict:
    """Backward-compatible single-object adapter for the original DR CLI."""
    result = await restore_isolated_catalog_batch([plan], transport, destination)
    return {
        **result,
        "object_id": plan["object_id"],
        "size": plan["size"],
        "sha256": plan["sha256"],
        "chunks": len(plan["chunks"]),
    }
