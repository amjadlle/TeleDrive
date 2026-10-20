#!/usr/bin/env python3
"""Read-only GC dry-run plan producer.

Enumerates every SQL reference for one failed object, fingerprints the graph
and records the owner gates that remain unmet. This command NEVER deletes
Telegram messages, SQL rows or metadata. Physical deletion stays disabled
until independent payload backup, verified restore, writer fencing and
two-person approval are proven.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from teledrive.db import connect_database
from teledrive.storage.catalog import BucketCatalog
from teledrive.storage.gc import StorageGCInventory
from teledrive.storage.manifest import ManifestStore


def run(args) -> dict:
    db = connect_database(args.database)
    try:
        catalog = BucketCatalog(ManifestStore(db))
        plan = StorageGCInventory(catalog.manifests).plan(
            args.object_id, now=args.now, backup_set_id=args.backup_set_id,
            writer_fence_token=args.writer_fence_token,
            min_age_seconds=args.min_age_seconds,
        )
        # Re-verify the plan is internally consistent before writing it.
        StorageGCInventory(catalog.manifests).verify_plan(plan)
        return plan
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--object-id", required=True)
    parser.add_argument("--min-age-seconds", type=int, default=7 * 86400)
    parser.add_argument("--backup-set-id", default="")
    parser.add_argument("--writer-fence-token", default="")
    parser.add_argument("--now", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        plan = run(args)
    except Exception as exc:
        parser.exit(1, "GC dry-run plan refused: " + type(exc).__name__ + "\n")
    # Private evidence file with mode 0600; no opaque Telegram file IDs.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(plan, stream, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps({
        "plan_written": str(args.output),
        "object_id": plan["object_id"],
        "reference_blocked": plan["reference_blocked"],
        "reference_blockers": plan["reference_blockers"],
        "deletion_permitted": plan["deletion_permitted"],
        "dry_run": plan["dry_run"],
        "remote_history_scan_required": plan["remote_history_scan_required"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
