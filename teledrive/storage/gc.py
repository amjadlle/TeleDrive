"""Read-only Telegram chunk reachability audit for eventual mark-and-sweep GC.

This module NEVER deletes remote messages or local metadata. It deliberately
treats all historical manifests as retained until a separate versioning /
retention policy and verified Telegram ownership scans are implemented.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict

from sqlalchemy import and_, inspect, select, func, or_

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
    # Confirmed Telegram Bot ACKs without manifest chunks still own media.
    unjournaled_bot_receipts: int = 0
    # A pending pre-send intent MAY correspond to a Telegram message.
    unresolved_bot_intents: int = 0
    # File IDs not backed by a chunk journal or ACK receipt remain unknown media.
    unattributed_bot_file_ids: int = 0


class StorageGCInventory:
    """Conservative inventory: an audit does not authorize physical deletion."""

    def __init__(self, manifests: ManifestStore):
        self.manifests = manifests
        self.db = manifests.db

    def preview_failed_retention(self, *, now: int,
                                 min_age_seconds: int = 7 * 86400,
                                 limit: int = 100) -> list[dict]:
        """Read-only mark-phase report, never a Telegram deletion authorization."""
        if min_age_seconds < 86400 or not 1 <= limit <= 1000:
            raise ValueError("Unsafe GC retention parameters")
        from .gc_safety import GCReferenceGuard
        guard = GCReferenceGuard(self.manifests)
        guard.validate_schema()
        db = self.db.connection
        found = db.execute(
            select(objects.c.id, objects.c.bucket, objects.c.object_key,
                   objects.c.updated_at)
            .where(objects.c.status == "failed",
                   objects.c.updated_at <= now - min_age_seconds)
            .order_by(objects.c.updated_at, objects.c.id).limit(limit)
        ).all()
        result = []
        for ident, bucket, key, updated_at in found:
            count = db.execute(select(func.count()).select_from(chunks).where(
                chunks.c.object_id == ident
            )).scalar_one()
            result.append({
                "object_id": ident, "bucket": bucket, "key": key,
                "updated_at": int(updated_at), "journaled_chunks": int(count),
                **guard.evidence(ident),
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

        # A durable Bot ACK may precede the manifest chunk transaction or
        # survive a crash. Count that acknowledged remote message in the
        # inventory even if no immutable chunk journal exists yet.
        inspector = inspect(self.db.engine)
        has_intents = inspector.has_table("storage_bot_send_intents")
        has_receipts = inspector.has_table("storage_bot_upload_receipts")
        if has_intents != has_receipts:
            raise RuntimeError("Bot reconciliation metadata incomplete")
        unresolved_bot_intents = 0
        unjournaled_bot_receipts = 0
        unattributed_bot_file_ids = 0
        has_files = inspector.has_table("storage_bot_file_ids")
        if has_intents and not has_files:
            raise RuntimeError("Bot file index schema incomplete")
        if has_intents:
            from teledrive.storage.bot_api_adapter import (
                _bot_send_intents, _bot_receipts, _bot_files,
            )
            unresolved_bot_intents = int(connection.execute(
                select(func.count()).select_from(_bot_send_intents)
            ).scalar_one())
            unjournaled_bot_receipts = int(connection.execute(
                select(func.count()).select_from(
                    _bot_receipts.outerjoin(chunks, and_(
                        chunks.c.account == _bot_receipts.c.account,
                        chunks.c.chat_id == _bot_receipts.c.chat_id,
                        chunks.c.message_id == _bot_receipts.c.message_id,
                    ))
                ).where(chunks.c.object_id.is_(None))
            ).scalar_one())
            # File IDs can survive old or partially migrated Bot operations.
            # A file_id with neither immutable journal nor durable receipt
            # is still a known remote asset, never automatically collectible.
            unclaimed = _bot_files.outerjoin(chunks, and_(
                _bot_files.c.account == chunks.c.account,
                _bot_files.c.chat_id == chunks.c.chat_id,
                _bot_files.c.message_id == chunks.c.message_id,
            )).outerjoin(_bot_receipts, and_(
                _bot_files.c.account == _bot_receipts.c.account,
                _bot_files.c.chat_id == _bot_receipts.c.chat_id,
                _bot_files.c.message_id == _bot_receipts.c.message_id,
            ))
            unattributed_bot_file_ids = int(connection.execute(
                select(func.count()).select_from(unclaimed).where(
                    chunks.c.object_id.is_(None),
                    _bot_receipts.c.object_id.is_(None),
                )
            ).scalar_one())
        elif has_files:
            from teledrive.storage.bot_api_adapter import _bot_files
            unattributed_bot_file_ids = int(connection.execute(
                select(func.count()).select_from(_bot_files.outerjoin(
                    chunks, and_(
                        _bot_files.c.account == chunks.c.account,
                        _bot_files.c.chat_id == chunks.c.chat_id,
                        _bot_files.c.message_id == chunks.c.message_id,
                    )
                )).where(chunks.c.object_id.is_(None))
            ).scalar_one())

        # Count USER-MTProto unknown ACKs/receipts as protected remote assets.
        if inspector.has_table("storage_mtproto_send_intents"):
            from teledrive.storage.user_mode import mtproto_send_intents
            unresolved_bot_intents += int(connection.execute(
                select(func.count()).select_from(mtproto_send_intents).where(
                    mtproto_send_intents.c.message_id.is_(None)
                )
            ).scalar_one())
            unjournaled_bot_receipts += int(connection.execute(
                select(func.count()).select_from(
                    mtproto_send_intents.outerjoin(chunks, and_(
                        chunks.c.account == mtproto_send_intents.c.account,
                        chunks.c.chat_id == mtproto_send_intents.c.chat_id,
                        chunks.c.message_id == mtproto_send_intents.c.message_id,
                    ))
                ).where(
                    mtproto_send_intents.c.message_id.is_not(None),
                    chunks.c.object_id.is_(None),
                )
            ).scalar_one())

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
            total_telegram_messages=(
                len(message_identities) + unjournaled_bot_receipts
                + unattributed_bot_file_ids
            ),
            live_object_heads=len(head_ids),
            copy_aliases=len(aliases),
            multipart_parts=len(multipart_objects),
            protected_chunks=audits,
            eligible_for_deletion=0,
            unjournaled_bot_receipts=unjournaled_bot_receipts,
            unresolved_bot_intents=unresolved_bot_intents,
            unattributed_bot_file_ids=unattributed_bot_file_ids,
        )
