"""Read-only Telegram chunk reachability audit for eventual mark-and-sweep GC.

This module NEVER deletes remote messages or local metadata. It deliberately
treats all historical manifests as retained until a separate versioning /
retention policy and verified Telegram ownership scans are implemented.
"""
from __future__ import annotations
import time

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
    # Confirmed MTProto send receipts without manifest chunks still own media.
    unjournaled_mtproto_receipts: int = 0
    # A pending pre-send intent MAY correspond to a Telegram message.
    unresolved_mtproto_intents: int = 0
    # Queued/running durable upload jobs can still publish new heads.
    active_upload_jobs: int = 0


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

    def plan(self, object_id: str, *, now: int | None = None,
            backup_set_id: str = "", writer_fence_token: str = "",
            min_age_seconds: int = 7 * 86400) -> dict:
        """Build a bounded, integrity-bound dry-run GC plan.

        The plan enumerates every SQL reference for `object_id`, fingerprints
        the reference graph and records the owner gates that must still pass
        before any physical deletion. It NEVER authorizes deletion: the
        deletion gates stay explicit and unmet by default. A plan is stale
        the moment any reference changes; use verify_plan() before acting.
        """
        if not isinstance(object_id, str) or not object_id:
            raise ValueError("GC plan requires an object id")
        if min_age_seconds < 86400:
            raise ValueError("Unsafe GC retention parameters")
        now = int(time.time()) if now is None else int(now)
        from .gc_safety import GCReferenceGuard
        guard = GCReferenceGuard(self.manifests)
        guard.validate_schema()
        db = self.db.connection
        row = db.execute(select(objects.c.id, objects.c.bucket,
                                objects.c.object_key, objects.c.status,
                                objects.c.updated_at)
                         .where(objects.c.id == object_id)).first()
        if row is None:
            raise FileNotFoundError("GC candidate missing")
        ident, bucket, key, status, updated_at = row
        if status != "failed":
            raise ValueError("Only failed objects may be planned for cleanup")
        if int(updated_at) > now - min_age_seconds:
            raise ValueError("Failure has not aged beyond the safety interval")
        evidence = guard.evidence(ident)
        age_seconds = now - int(updated_at)
        return {
            "plan_version": 1,
            "dry_run": True,
            "object_id": ident,
            "bucket": bucket,
            "key": key,
            "status": status,
            "updated_at": int(updated_at),
            "age_seconds": age_seconds,
            "min_age_seconds": min_age_seconds,
            "journaled_chunks": int(db.execute(
                select(func.count()).select_from(chunks).where(
                    chunks.c.object_id == ident
                )
            ).scalar_one()),
            "sql_reference_fingerprint": evidence["sql_reference_fingerprint"],
            "reference_blocked": evidence["reference_blocked"],
            "reference_blockers": evidence["reference_blockers"],
            "copy_alias_descendants": evidence["copy_alias_descendants"],
            "writer_fence_present": evidence["writer_fence_present"],
            "historical_version_retained": evidence["historical_version_retained"],
            "unresolved_mtproto_intents": evidence["unresolved_mtproto_intents"],
            "backup_set_id": backup_set_id or None,
            "writer_fence_token": writer_fence_token or None,
            "remote_history_scan_required": True,
            "writer_barrier_verified": False,
            "independent_payload_backup_verified": False,
            "restore_drill_verified": False,
            "two_person_deletion_approval_verified": False,
            "deletion_permitted": False,
            "owner_gates": [
                "independent_payload_backup_verified",
                "restore_drill_verified",
                "writer_barrier_verified",
                "two_person_deletion_approval_verified",
            ],
            "approved_for_deletion": False,
            "physical_deletion_enabled": False,
            "rollback_strategy": "new_media_upload_and_fresh_sql_identities",
        }

    def verify_plan(self, plan: dict) -> None:
        from .gc_safety import GCReferenceGuard
        guard = GCReferenceGuard(self.manifests)
        guard.verify_plan(plan)

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

        # A durable MTProto receipt may precede the manifest chunk transaction
        # or survive a crash. Count that acknowledged remote message in the
        # inventory even if no immutable chunk journal exists yet.
        inspector = inspect(self.db.engine)
        unresolved_mtproto_intents = 0
        unjournaled_mtproto_receipts = 0
        if inspector.has_table("storage_mtproto_send_intents"):
            from teledrive.storage.user_mode import mtproto_send_intents
            unresolved_mtproto_intents = int(connection.execute(
                select(func.count()).select_from(mtproto_send_intents).where(
                    mtproto_send_intents.c.message_id.is_(None)
                )
            ).scalar_one())
            unjournaled_mtproto_receipts = int(connection.execute(
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

        # In-flight durable upload jobs are live references: a queued/running
        # job can still publish a head over the candidate's bucket/key.
        active_upload_jobs = 0
        if inspector.has_table("storage_upload_jobs"):
            from teledrive.storage.gc_safety import ACTIVE_JOB_STATES
            from teledrive.storage.jobs import jobs as upload_jobs
            active_upload_jobs = int(connection.execute(
                select(func.count()).select_from(upload_jobs).where(
                    upload_jobs.c.state.in_(ACTIVE_JOB_STATES)
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
                len(message_identities) + unjournaled_mtproto_receipts
            ),
            live_object_heads=len(head_ids),
            copy_aliases=len(aliases),
            multipart_parts=len(multipart_objects),
            protected_chunks=audits,
            eligible_for_deletion=0,
            unjournaled_mtproto_receipts=unjournaled_mtproto_receipts,
            unresolved_mtproto_intents=unresolved_mtproto_intents,
            active_upload_jobs=active_upload_jobs,
        )
