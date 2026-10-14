"""Offline preflight: compare discovered Telegram markers with independently supplied plans.

No bytes, original bucket/key or historical versions can be inferred from a
caption. Matching coordinates are NOT proof of remote ownership or integrity.
"""
from __future__ import annotations

import hashlib
import json

from .rebuild import validate_batch


def reconcile_inventory(inventory: dict, plans: list[dict]) -> dict:
    validated = validate_batch(plans)
    if not isinstance(inventory, dict) or inventory.get("schema_version") != 1:
        raise ValueError("Unsupported Telegram inventory schema")
    scope = inventory.get("scope")
    candidates = inventory.get("candidate_objects")
    if (not isinstance(scope, dict) or type(scope.get("chat_id")) is not int
            or not isinstance(scope.get("account"), str) or not scope["account"]
            or not isinstance(candidates, list) or len(candidates) > 10000
            or type(inventory.get("scanned_messages")) is not int
            or not 0 <= inventory["scanned_messages"] <= 10000
            or type(inventory.get("truncated")) is not bool
            or type(inventory.get("candidate_object_count")) is not int
            or inventory["candidate_object_count"] != len(candidates)
            or type(inventory.get("candidate_chunk_count")) is not int):
        raise ValueError("Malformed bounded Telegram inventory")

    observed = {}
    seen_messages = set()
    total = 0
    for candidate in candidates:
        if (not isinstance(candidate, dict)
                or not isinstance(candidate.get("object_id"), str)
                or candidate["object_id"] in observed
                or not isinstance(candidate.get("chunks"), list)
                or len(candidate["chunks"]) > 4096):
            raise ValueError("Ambiguous object marker inventory")
        parts = []
        for part in candidate["chunks"]:
            if (not isinstance(part, dict)
                    or set(part) != {"ordinal", "account", "chat_id",
                                    "message_id", "checksum_prefix"}
                    or type(part["ordinal"]) is not int or part["ordinal"] < 0
                    or type(part["chat_id"]) is not int
                    or type(part["message_id"]) is not int or part["message_id"] <= 0
                    or part["account"] != scope["account"]
                    or part["chat_id"] != scope["chat_id"]
                    or not isinstance(part["checksum_prefix"], str)
                    or len(part["checksum_prefix"]) != 16
                    or any(c not in "0123456789abcdef" for c in part["checksum_prefix"])):
                raise ValueError("Untrusted Telegram candidate coordinates")
            coords = (part["account"], part["chat_id"], part["message_id"])
            if coords in seen_messages:
                raise ValueError("Duplicate Telegram message in inventory")
            seen_messages.add(coords)
            parts.append((part["ordinal"], *coords, part["checksum_prefix"]))
            total += 1
        observed[candidate["object_id"]] = parts
    if total != inventory["candidate_chunk_count"] or total > inventory["scanned_messages"]:
        raise ValueError("Inconsistent candidate chunk counts")
    outcomes = []
    for plan, refs in validated:
        original = [(x.ordinal, x.account, x.chat_id, x.message_id, x.sha256[:16])
                    for x in refs]
        found = observed.get(plan["object_id"])
        if not refs:
            result = "unproven_empty_object"
        elif found is None:
            result = "not_observed_in_scanned_window"
        elif len(found) != len(original) or sorted(found) != sorted(original):
            result = "message_identity_mismatch"
        else:
            result = "candidate_coordinates_match"
        outcomes.append({
            "object_id": plan["object_id"], "result": result,
            "matched_chunk_count": len(refs) if result == "candidate_coordinates_match" else 0,
            "payload_verified": False,
        })
    matched = all(x["result"] == "candidate_coordinates_match" for x in outcomes)
    fingerprint = hashlib.sha256(json.dumps(
        {"plans": plans, "inventory": inventory}, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return {
        "schema_version": 1, "report_sha256": fingerprint,
        "objects_checked": len(outcomes), "all_plans_match": matched,
        "ready_for_independent_byte_verification": matched,
        "inventory_window_truncated": inventory["truncated"],
        "extra_unmapped_candidates": len(set(observed) - {p["object_id"] for p, _ in validated}),
        "matches": outcomes,
        "historic_versions_reconstructed": False,
        "remote_ownership_verified": False, "payload_verified": False,
        "automatic_import_performed": False,
        "telegram_deletions": 0, "production_database_modified": False,
    }
