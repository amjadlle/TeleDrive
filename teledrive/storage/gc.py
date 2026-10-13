"""Read-only Telegram chunk reachability audit for eventual mark-and-sweep GC.

This module NEVER deletes remote messages or local metadata. It deliberately
treats all historical manifests as retained until a separate versioning /
retention policy and verified Telegram ownership scans are implemented.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict

from sqlalchemy import inspect, select, func, or_

from teledrive.storage.catalog import BucketCatalog, heads
from teledrive.storage.manifest import ManifestStore, object_aliases, objects, chunks


@dataclass(frozen=True)
class ChunkAudit:
    owner_object_id: str
    message_count: int
    reference_count: int
    live_heads: int
    copied_aliases: int
    multipart_parts: int
    retained_history: bool


@dataclass(frozen=True)
class GCInventory:
    total_manifests: int
    total_telegram_messages: int
    live_object_heads: int
    copy_aliases: int
    multipart_parts: int
    protected_chunks: tuple[ChunkAudit, ...]
    eligible_for_deletion: int


class StorageGCInventory:
    """Conservative inventory: an audit does not authorize physical deletion."""

    def __init__(self, manifests: ManifestStore):
        self.manifests = manifests
        self.db = manifests.db

    def preview_failed_retention(self, *, now: int,
                                 min_age_seconds: int = 7 * 86400,
                                 limit: int = 100) -> list[dict]:
        """Inspect aged FAILED journals without authorizing Telegram deletion."""
        if min_age_seconds < 86400 or not 1 <= limit <= 1000:
            raise ValueError("Unsafe GC retention parameters")
        from teledrive.storage.catalog import published_versions
        from teledrive.s3.multipart import parts
        db = self.db.connection
        found = db.execute(
            select(objects.c.id, objects.c.bucket, objects.c.object_key,
                   objects.c.updated_at)
            .where(objects.c.status == "failed",
                   objects.c.updated_at <= now - min_age_seconds)
            .order_by(objects.c.updated_at, objects.c.id).limit(limit)
        ).all()
        has_parts = inspect(self.db.engine).has_table("storage_multipart_parts")
        result = []
        for ident, bucket, key, updated_at in found:
            count = db.execute(select(func.count()).select_from(chunks).where(
                chunks.c.object_id == ident
            )).scalar_one()
            head_refs = db.execute(select(func.count()).select_from(heads).where(
                heads.c.object_id == ident
            )).scalar_one()
            alias_refs = db.execute(
                select(func.count()).select_from(object_aliases).where(
                    or_(object_aliases.c.object_id == ident,
                        object_aliases.c.source_object_id == ident)
                )
            ).scalar_one()
            history_refs = db.execute(
                select(func.count()).select_from(published_versions).where(
                    published_versions.c.object_id == ident
                )
            ).scalar_one()
            part_refs = db.execute(
                select(func.count()).select_from(parts).where(parts.c.object_id == ident)
            ).scalar_one() if has_parts else 0
            result.append({
                "object_id": ident, "bucket": bucket, "key": key,
                "updated_at": int(updated_at), "journaled_chunks": int(count),
                "reference_blocked": bool(
                    head_refs or alias_refs or history_refs or part_refs
                ),
                "remote_history_scan_required": True,
                "approved_for_deletion": False, "dry_run": True,
            })
        return result

    def inspect(self) -> GCInventory:
        connection = self.db.connection

        # Current head pointers are only one reference source. Deleted or
        # overwritten objects are still historical versions, not safe garbage.
        # S3 delete markers are SQL HEAD tombstones, not live payload
        # objects. They must not inflate the live-object count or the
        # retained Telegram reference inventory.
        head_ids = list(connection.execute(
            select(heads.c.object_id).select_from(heads.join(
                objects, heads.c.object_id == objects.c.id,
            )).where(objects.c.status == "ready")
        ).scalars())
        aliases = list(connection.execute(select(
            object_aliases.c.object_id, object_aliases.c.source_object_id
        )).all())
        all_objects = list(connection.execute(select(
            objects.c.id, objects.c.status
        )).all())
        all_chunks = list(connection.execute(select(
            chunks.c.object_id, chunks.c.account,
            chunks.c.chat_id, chunks.c.message_id
        )).all())

        multipart_objects: list[str] = []
        if inspect(self.db.engine).has_table("storage_multipart_parts"):
            from teledrive.s3.multipart import parts
            multipart_objects = list(connection.execute(
                select(parts.c.object_id)
            ).scalars())

        # Detect invalid/cyclic metadata and refuse to claim a complete audit.
        source_of = dict(aliases)
        known_ids = {identifier for identifier, _ in all_objects}
        def root(identifier: str) -> str:
            visited = set()
            current = identifier
            while current in source_of:
                if current in visited or current not in known_ids:
                    raise RuntimeError("Copy alias graph is corrupt")
                visited.add(current)
                current = source_of[current]
            if current not in known_ids:
                raise RuntimeError("Copy source manifest missing")
            return current

        head_counts = defaultdict(int)
        alias_counts = defaultdict(int)
        part_counts = defaultdict(int)
        for identifier in head_ids:
            head_counts[root(identifier)] += 1
        for alias_id, source_id in aliases:
            # Historical aliases protect their source media too, until
            # immutable-history retention and physical versioning exist.
            if alias_id not in known_ids:
                raise RuntimeError("Copy target manifest missing")
            alias_counts[root(source_id)] += 1
        for identifier in multipart_objects:
            part_counts[root(identifier)] += 1

        chunk_counts = defaultdict(int)
        message_identities = set()
        for identifier, account, chat_id, message_id in all_chunks:
            if identifier not in known_ids:
                raise RuntimeError("Telegram chunk references deleted manifest")
            identity = (account, chat_id, message_id)
            if identity in message_identities:
                raise RuntimeError("Duplicate Telegram message ownership")
            message_identities.add(identity)
            chunk_counts[identifier] += 1

        # Roots with no current refs can still be historical versions; do not
        # interpret this report as an approved deletion plan.
        audits = tuple(
            ChunkAudit(
                owner_object_id=identifier,
                message_count=chunk_counts[identifier],
                reference_count=head_counts[identifier]+alias_counts[identifier]+part_counts[identifier],
                live_heads=head_counts[identifier],
                copied_aliases=alias_counts[identifier],
                multipart_parts=part_counts[identifier],
                retained_history=True,
            )
            for identifier, _ in all_objects if chunk_counts[identifier]
        )
        return GCInventory(
            total_manifests=len(all_objects),
            total_telegram_messages=len(message_identities),
            live_object_heads=len(head_ids),
            copy_aliases=len(aliases),
            multipart_parts=len(multipart_objects),
            protected_chunks=audits,
            eligible_for_deletion=0,
        )
