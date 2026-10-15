"""Fail-closed, non-destructive SQL graph checks for Telegram garbage collection.

An SQL preview does not prove absence of remote media or stop concurrent writers.
No method in this module is allowed to delete any metadata or payload.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import inspect, select
from teledrive.storage.catalog import heads, published_versions, removed_versions
from teledrive.storage.manifest import chunks, object_aliases, objects, writer_fences


class GCReferenceGuard:
    REQUIRED = frozenset({
        "storage_objects", "storage_chunks", "storage_object_aliases",
        "storage_writer_fences", "storage_object_heads",
        "storage_published_versions", "storage_removed_versions",
    })

    def __init__(self, manifests):
        self.db = manifests.db

    def validate_schema(self):
        names = set(inspect(self.db.engine).get_table_names())
        if not self.REQUIRED.issubset(names):
            raise RuntimeError("Incomplete GC reference graph schema")
        if ("storage_bot_send_intents" in names) != ("storage_bot_upload_receipts" in names):
            raise RuntimeError("Bot reconciliation metadata incomplete")
        return names

    def evidence(self, identifier):
        from teledrive.s3.multipart import parts
        from teledrive.storage.bot_api_adapter import _bot_receipts, _bot_send_intents
        from teledrive.storage.user_mode import mtproto_send_intents

        names = self.validate_schema()
        db = self.db.connection
        known_ids = set(db.execute(select(objects.c.id)).scalars())
        if identifier not in known_ids:
            raise RuntimeError("GC candidate missing")
        edges = dict(db.execute(select(
            object_aliases.c.object_id, object_aliases.c.source_object_id
        )).all())
        for alias in edges:
            visited = set()
            current = alias
            while current in edges:
                if current in visited or current not in known_ids:
                    raise RuntimeError("Copy alias graph is corrupt")
                visited.add(current)
                current = edges[current]
            if current not in known_ids:
                raise RuntimeError("Copy alias graph is corrupt")

        linked = {identifier}
        for alias in edges:
            current = alias
            while current in edges:
                current = edges[current]
                if current == identifier:
                    linked.add(alias)
                    break

        def refs(column):
            return sorted(db.execute(
                select(column).where(column.in_(linked))
            ).scalars())

        graph = {
            "head": refs(heads.c.object_id),
            "published_version": refs(published_versions.c.object_id),
            "removed_version": refs(removed_versions.c.object_id),
            "copy_alias": sorted(linked - {identifier}),
            "writer_fence": refs(writer_fences.c.object_id),
            "multipart_part": refs(parts.c.object_id)
                if "storage_multipart_parts" in names else [],
            "bot_send_intent": refs(_bot_send_intents.c.object_id)
                if "storage_bot_send_intents" in names else [],
            "bot_receipt": refs(_bot_receipts.c.object_id)
                if "storage_bot_upload_receipts" in names else [],
            "mtproto_send_intent_or_receipt": refs(mtproto_send_intents.c.object_id)
                if "storage_mtproto_send_intents" in names else [],
        }
        chunk_journal = [
            (int(n), account, int(chat), int(mid), digest)
            for n, account, chat, mid, digest in db.execute(
                select(chunks.c.ordinal, chunks.c.account, chunks.c.chat_id,
                       chunks.c.message_id, chunks.c.sha256)
                .where(chunks.c.object_id == identifier)
                .order_by(chunks.c.ordinal)
            )
        ]
        fingerprint = hashlib.sha256(json.dumps({
            "id": identifier, "graph": graph, "journal": chunk_journal,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        blockers = sorted(k for k, refs_found in graph.items() if refs_found)
        return {
            "reference_blocked": bool(blockers),
            "reference_blockers": blockers,
            "sql_reference_fingerprint": fingerprint,
            "copy_alias_descendants": len(graph["copy_alias"]),
            "writer_fence_present": bool(graph["writer_fence"]),
            "historical_version_retained": bool(
                graph["published_version"] or graph["removed_version"]
            ),
            "unresolved_bot_intents": len(graph["bot_send_intent"]),
            "durable_bot_receipts": len(graph["bot_receipt"]),
            "writer_barrier_verified": False,
            "independent_payload_backup_verified": False,
            "restore_drill_verified": False,
            "two_person_deletion_approval_verified": False,
            "deletion_permitted": False,
        }
