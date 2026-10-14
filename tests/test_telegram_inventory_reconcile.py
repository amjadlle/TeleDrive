"""Adversarial offline comparison between Telegram markers and external manifests."""
from __future__ import annotations

import copy
import hashlib
from uuid import uuid4

import pytest
from teledrive.storage.reconcile_inventory import reconcile_inventory


def inputs(parts=(b"abc", b"def")):
    identifier = str(uuid4())
    chunks = [{
        "ordinal": i, "account": "ci", "chat_id": -100123,
        "message_id": 100 + i, "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    } for i, data in enumerate(parts)]
    plan = {
        "object_id": identifier, "bucket": "recovery-test",
        "key": "restored/key.bin", "size": sum(len(p) for p in parts),
        "sha256": hashlib.sha256(b"".join(parts)).hexdigest(),
        "chunks": chunks,
    }
    inventory = {
        "schema_version": 1,
        "scope": {"account": "ci", "chat_id": -100123, "scan_limit": 100},
        "candidate_objects": [{
            "object_id": identifier, "chunks": [{
                "ordinal": c["ordinal"], "account": c["account"],
                "chat_id": c["chat_id"], "message_id": c["message_id"],
                "checksum_prefix": c["sha256"][:16],
            } for c in chunks],
        }],
        "candidate_object_count": 1, "candidate_chunk_count": len(chunks),
        "scanned_messages": len(chunks), "truncated": False,
    }
    return plan, inventory


def test_exact_identity_is_only_permission_to_verify_bytes():
    plan, inventory = inputs()
    report = reconcile_inventory(inventory, [plan])
    assert report["all_plans_match"]
    assert report["ready_for_independent_byte_verification"]
    assert report["objects_checked"] == 1
    assert report["matches"][0]["matched_chunk_count"] == 2
    assert not report["remote_ownership_verified"]
    assert not report["payload_verified"]
    assert not report["historic_versions_reconstructed"]
    assert report["telegram_deletions"] == 0


@pytest.mark.parametrize("field,value,raises", [
    ("message_id", 555, False), ("ordinal", 5, False),
    ("checksum_prefix", "0" * 16, False),
    ("chat_id", -100888, True), ("account", "other", True),
])
def test_wrong_coordinate_never_matches(field, value, raises):
    plan, inventory = inputs()
    inventory["candidate_objects"][0]["chunks"][0][field] = value
    if raises:
        with pytest.raises(ValueError, match="Untrusted"):
            reconcile_inventory(inventory, [plan])
    else:
        assert not reconcile_inventory(inventory, [plan])["all_plans_match"]


def test_missing_or_duplicate_chunk_is_not_accepted():
    plan, inventory = inputs()
    inventory["candidate_objects"][0]["chunks"].pop()
    inventory["candidate_chunk_count"] = 1
    assert not reconcile_inventory(inventory, [plan])["all_plans_match"]
    plan, inventory = inputs()
    inventory["candidate_objects"][0]["chunks"][1]["message_id"] = 100
    with pytest.raises(ValueError, match="Duplicate"):
        reconcile_inventory(inventory, [plan])


def test_truncated_window_never_proves_unknown_history():
    plan, inventory = inputs()
    inventory["truncated"] = True
    report = reconcile_inventory(inventory, [plan])
    assert report["all_plans_match"] and report["inventory_window_truncated"]
    assert not report["historic_versions_reconstructed"]


def test_unmapped_candidates_are_counted_not_named():
    plan, inventory = inputs()
    outsider = copy.deepcopy(inventory["candidate_objects"][0])
    outsider["object_id"] = str(uuid4())
    for chunk in outsider["chunks"]:
        chunk["message_id"] += 500
    inventory["candidate_objects"].append(outsider)
    inventory["candidate_object_count"] = 2
    inventory["candidate_chunk_count"] = 4
    inventory["scanned_messages"] = 4
    report = reconcile_inventory(inventory, [plan])
    assert report["all_plans_match"]
    assert report["extra_unmapped_candidates"] == 1
    assert report["automatic_import_performed"] is False


@pytest.mark.parametrize("mutation", [
    lambda i: i.update(schema_version=2),
    lambda i: i.update(candidate_object_count=99),
    lambda i: i.update(scanned_messages=0),
    lambda i: i.update(truncated="yes"),
    lambda i: i["candidate_objects"].append(i["candidate_objects"][0]),
])
def test_malformed_inventory_is_rejected(mutation):
    plan, inventory = inputs()
    mutation(inventory)
    with pytest.raises(ValueError):
        reconcile_inventory(inventory, [plan])


def test_wrong_manifest_fails_before_remote_access():
    plan, inventory = inputs()
    plan["sha256"] = "0" * 16
    with pytest.raises(ValueError, match="independent"):
        reconcile_inventory(inventory, [plan])


def test_empty_object_is_not_supported_by_marker_inventory():
    plan, inventory = inputs(parts=())
    inventory["candidate_objects"][0]["chunks"] = []
    inventory["candidate_chunk_count"] = 0
    inventory["scanned_messages"] = 0
    answer = reconcile_inventory(inventory, [plan])
    assert answer["matches"][0]["result"] == "unproven_empty_object"
    assert not answer["all_plans_match"]
