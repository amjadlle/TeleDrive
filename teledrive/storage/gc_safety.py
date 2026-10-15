"""Fail-closed, non-destructive SQL graph checks for Telegram garbage collection.

An SQL preview does not prove absence of remote media or stop concurrent writers.
No method in this module is allowed to delete any metadata or payload.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import func, inspect, or_, select
from teledrive.storage.catalog import heads, published_versions, removed_versions
from teledrive.storage.manifest import chunks, object_aliases, objects, writer_fences

ACTIVE_JOB_STATES = ("queued", "running", "staged", "publishing")


def durable_job_blockers(db, names, linked):
    """Active durable upload-job IDs that hold or will publish linked objects.

    A queued/running job can still publish the candidate head (by object_id,
    expected_head_id, or the same bucket/key), so it is a live reference.
    Terminal jobs are excluded: their published head is covered by the head
    graph entry, and failed/cancelled jobs hold no pending publication.
    """
    if "storage_upload_jobs" not in names:
        return []
    from teledrive.storage.jobs import jobs as upload_jobs

    by_identity = db.execute(select(upload_jobs.c.id).where(
        upload_jobs.c.state.in_(ACTIVE_JOB_STATES),
        or_(
            upload_jobs.c.object_id.in_(linked),
            upload_jobs.c.expected_head_id.in_(linked),
        ),
    )).scalars()
    blocked = set(by_identity)
    keyed = db.execute(select(
        objects.c.bucket, objects.c.object_key
    ).where(objects.c.id.in_(linked))).all()
    for bucket, key in {(row[0], row[1]) for row in keyed}:
        blocked.update(db.execute(select(upload_jobs.c.id).where(
            upload_jobs.c.state.in_(ACTIVE_JOB_STATES),
            upload_jobs.c.bucket == bucket,
            upload_jobs.c.object_key == key,
        )).scalars())
    return sorted(blocked)


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
        return names

    def evidence(self, identifier):
        from teledrive.s3.multipart import parts
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
            "mtproto_send_intent_or_receipt": refs(mtproto_send_intents.c.object_id)
                if "storage_mtproto_send_intents" in names else [],
            "durable_job": durable_job_blockers(db, names, linked),
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
        # Unresolved = pre-send intents without any remote message ID yet.
        # Acknowledged receipts still block via the graph entry above.
        unresolved_intents = 0
        if "storage_mtproto_send_intents" in names:
            unresolved_intents = int(db.execute(
                select(func.count()).select_from(mtproto_send_intents).where(
                    mtproto_send_intents.c.object_id.in_(linked),
                    mtproto_send_intents.c.message_id.is_(None),
                )
            ).scalar_one())
        return {
            "reference_blocked": bool(blockers),
            "reference_blockers": blockers,
            "sql_reference_fingerprint": fingerprint,
            "copy_alias_descendants": len(graph["copy_alias"]),
            "writer_fence_present": bool(graph["writer_fence"]),
            "historical_version_retained": bool(
                graph["published_version"] or graph["removed_version"]
            ),
            "unresolved_mtproto_intents": unresolved_intents,
            "writer_barrier_verified": False,
            "independent_payload_backup_verified": False,
            "restore_drill_verified": False,
            "two_person_deletion_approval_verified": False,
            "deletion_permitted": False,
        }
