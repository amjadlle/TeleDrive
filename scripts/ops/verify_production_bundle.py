"""Offline, dual-attested production evidence bundle verifier.

This validates receipts and operator signatures; it does NOT poll production
hosts or assert an installation exists. Do not sign off a deployment without
independently verifying original sources and machine ownership.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def canonical_payload(bundle: dict) -> bytes:
    evidence = dict(bundle)
    evidence.pop("approvals", None)
    return json.dumps(evidence, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def verify_bundle(bundle: dict, *, expected_sha: str,
                  owner_public_key: bytes, operator_public_key: bytes) -> dict:
    if not isinstance(bundle, dict) or not _COMMIT.fullmatch(expected_sha):
        raise ValueError("Invalid production release identity")
    if bundle.get("release_sha") != expected_sha:
        raise ValueError("Receipts do not match the reviewed release source")
    if owner_public_key == operator_public_key or len(owner_public_key) != 32 or len(operator_public_key) != 32:
        raise ValueError("Independent owner and operator keys are required")
    if not {"soak", "dr", "cutover", "topology", "approvals"}.issubset(bundle):
        raise ValueError("Missing production evidence family")

    topology = bundle["topology"]
    if not isinstance(topology, dict) or not isinstance(topology.get("hosts"), list):
        raise ValueError("Physical deployment inventory absent")
    hosts = topology["hosts"]
    if not 2 <= len(hosts) <= 8:
        raise ValueError("At least two independently attested hosts required")
    labels, machines, dbs = set(), set(), set()
    for host in hosts:
        if not isinstance(host, dict) or set(host) != {
            "label", "machine_id_sha256", "shared_database_sha256",
        }:
            raise ValueError("Invalid independent host evidence")
        if (not isinstance(host["label"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", host["label"])
                or any(not isinstance(host[k], str) or not _SHA256.fullmatch(host[k])
                       for k in ("machine_id_sha256", "shared_database_sha256"))):
            raise ValueError("Malformed physical deployment identifiers")
        labels.add(host["label"])
        machines.add(host["machine_id_sha256"])
        dbs.add(host["shared_database_sha256"])
    if len(labels) != len(hosts) or len(machines) != len(hosts) or len(dbs) != 1:
        raise ValueError("Physical hosts must differ and share one SQL database")
    if topology.get("independent_hosts_owner_attested") is not True:
        raise ValueError("Original host ownership has not been attested")

    soak = bundle["soak"]
    if (not isinstance(soak, dict) or soak.get("telemetry_gate_passed") is not True
            or type(soak.get("duration_seconds")) is not int
            or soak["duration_seconds"] < 3600
            or soak.get("final_shared_counter_agreement") is not True
            or soak.get("telegram_mutations_performed") != 0
            or set(soak.get("host_labels", [])) != labels
            or not isinstance(soak.get("explicit_429_deltas_by_host"), dict)
            or set(soak["explicit_429_deltas_by_host"]) != labels
            or any(type(delta) is not int or delta <= 0
                   for delta in soak["explicit_429_deltas_by_host"].values())):
        raise ValueError("Missing independent one-hour real 429 soak evidence")

    dr = bundle["dr"]
    if (not isinstance(dr, dict) or dr.get("verified") is not True
            or dr.get("source_database_modified") is not False
            or dr.get("telegram_uploads") != 0 or dr.get("telegram_deletions") != 0
            or type(dr.get("size")) is not int or dr["size"] < 0
            or dr.get("readback_bytes") != dr["size"]
            or not isinstance(dr.get("sha256"), str)
            or not _SHA256.fullmatch(dr["sha256"])
            or not isinstance(dr.get("archive_sha256"), str)
            or not _SHA256.fullmatch(dr["archive_sha256"])
            or bundle.get("original_telegram_data_attested") is not True):
        raise ValueError("Missing verified original-media DR receipt")

    cutover = bundle["cutover"]
    if not isinstance(cutover, dict):
        raise ValueError("Cutover evidence missing")
    if cutover.get("not_applicable") is True:
        if bundle.get("no_legacy_source_attested") is not True:
            raise ValueError("No-legacy exemption must be independently attested")
    elif not (
        cutover.get("verified") is True
        and type(cutover.get("eligible_files")) is int
        and cutover["eligible_files"] > 0
        and cutover.get("verified_files") == cutover["eligible_files"]
        and cutover.get("not_yet_published") == 0
        and cutover.get("inconsistent") == 0
        and cutover.get("truncated") is False
        and cutover.get("telegram_uploads") == 0
        and cutover.get("telegram_deletions") == 0
    ):
        raise ValueError("Cutover is incomplete or ownership not approved")

    approval = bundle["approvals"]
    if not isinstance(approval, dict) or set(approval) != {"owner", "operator"}:
        raise ValueError("Independent production approvals missing")
    signed_message = canonical_payload(bundle)
    for role, key_bytes in (("owner", owner_public_key), ("operator", operator_public_key)):
        try:
            encoded = approval[role]
            if not isinstance(encoded, str):
                raise ValueError("Invalid approval type")
            signature = base64.b64decode(encoded, validate=True)
            Ed25519PublicKey.from_public_bytes(key_bytes).verify(signature, signed_message)
        except (ValueError, TypeError, Exception) as exc:
            # Deliberately suppress key contents and signing details.
            raise ValueError("Invalid " + role + " evidence signature") from exc

    return {
        "technical_evidence_verified": True,
        "two_independent_attestations_verified": True,
        "release_sha": expected_sha,
        "bundle_sha256": hashlib.sha256(signed_message).hexdigest(),
        # Real deployment remains an owner-controlled operational decision,
        # never silently changed by a machine-readable proof.
        "production_acceptance_signed_off": False,
        "physical_telegram_deletion_enabled": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--owner-public-key", required=True, type=Path,
                        help="Raw 32-byte Ed25519 public key")
    parser.add_argument("--operator-public-key", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = verify_bundle(
            json.loads(args.bundle.read_text("utf-8")),
            expected_sha=args.release_sha,
            owner_public_key=args.owner_public_key.read_bytes(),
            operator_public_key=args.operator_public_key.read_bytes(),
        )
    except Exception as exc:
        parser.exit(1, "Production evidence verification refused: "
                    + type(exc).__name__ + "\\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
