#!/usr/bin/env python3
"""Large-file S3 round-trip acceptance test.

Generates ten random files with sizes uniformly chosen between 100 MiB and
4 GiB, records their SHA-256 digests, uploads them through the TeleDrive S3
gateway, removes the originals, downloads them again and verifies every byte
against the sealed digest. No production data, tokens or media filenames are
recorded; evidence is redacted and written under 0600 permissions.

Usage (against a running TeleDrive instance with issued S3 credentials):
    export TELEDRIVE_S3_ENDPOINT=http://127.0.0.1:9000
    export TELEDRIVE_S3_ACCESS_KEY=...
    export TELEDRIVE_S3_SECRET_KEY=...
    export TELEDRIVE_LARGE_ROUNDTRIP_DIR=/secure/scratch
    python scripts/ops/s3_large_roundtrip.py

Optional:
    --seed N            deterministic random sizes/content (default: OS entropy)
    --bucket NAME       target bucket (default: ci-large-roundtrip-<seed>)
    --count N           number of files (default: 10)
    --min-mib N         minimum file size in MiB (default: 100)
    --max-mib N         maximum file size in MiB (default: 4096)
    --report PATH       redacted evidence JSON (default: <scratch>/report.json)
    --keep              keep downloaded files after verification
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

CHUNK = 64 * 1024 * 1024  # 64 MiB staging blocks


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def generate_file(path: Path, size: int, rng: random.Random) -> str:
    """Write `size` random bytes and return the SHA-256 digest."""
    digest = hashlib.sha256()
    remaining = size
    with path.open("wb") as handle:
        while remaining:
            block_size = min(CHUNK, remaining)
            block = rng.randbytes(block_size)
            handle.write(block)
            digest.update(block)
            remaining -= block_size
            handle.flush()
    return digest.hexdigest()


def make_client(endpoint: str, access_key: str, secret_key: str):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path", "payload_signing_enabled": True},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 3},
        ),
    )


def upload_file(client, bucket: str, key: str, path: Path) -> int:
    """Multipart upload for large objects; returns total bytes."""
    import os as _os

    size = path.stat().st_size
    if size <= 100 * 1024 * 1024:
        with path.open("rb") as handle:
            client.put_object(Bucket=bucket, Key=key, Body=handle)
        return size
    part_size = 16 * 1024 * 1024  # 16 MiB parts
    parts = []
    multipart = client.create_multipart_upload(Bucket=bucket, Key=key)
    upload_id = multipart["UploadId"]
    try:
        with path.open("rb") as handle:
            part_number = 1
            while True:
                data = handle.read(part_size)
                if not data:
                    break
                response = client.upload_part(
                    Bucket=bucket, Key=key, UploadId=upload_id,
                    PartNumber=part_number, Body=data,
                )
                parts.append({"PartNumber": part_number, "ETag": response["ETag"]})
                part_number += 1
        client.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
    except Exception:
        client.abort_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id,
        )
        raise
    return size


def download_file(client, bucket: str, key: str, destination: Path) -> int:
    response = client.get_object(Bucket=bucket, Key=key)
    size = 0
    with destination.open("wb") as handle:
        for block in iter(lambda: response["Body"].read(1024 * 1024), b""):
            handle.write(block)
            size += len(block)
    return size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=None,
                        help="Deterministic random seed")
    parser.add_argument("--count", type=int, default=10,
                        help="Number of files (default: 10)")
    parser.add_argument("--min-mib", type=int, default=100,
                        help="Minimum size in MiB")
    parser.add_argument("--max-mib", type=int, default=4096,
                        help="Maximum size in MiB")
    parser.add_argument("--bucket", default="",
                        help="Target bucket (default: derived)")
    parser.add_argument("--report", default="",
                        help="Redacted evidence JSON path")
    parser.add_argument("--keep", action="store_true",
                        help="Keep downloaded files after verification")
    args = parser.parse_args()

    endpoint = env("TELEDRIVE_S3_ENDPOINT", "http://127.0.0.1:9000")
    access_key = env("TELEDRIVE_S3_ACCESS_KEY")
    secret_key = env("TELEDRIVE_S3_SECRET_KEY")
    scratch = Path(env("TELEDRIVE_LARGE_ROUNDTRIP_DIR", "/tmp/teledrive-large-roundtrip"))
    if not access_key or not secret_key:
        print("Missing TELEDRIVE_S3_ACCESS_KEY / TELEDRIVE_S3_SECRET_KEY",
              file=sys.stderr)
        return 2
    if args.count < 1 or args.count > 100:
        print("count must be 1..100", file=sys.stderr)
        return 2
    if args.min_mib < 1 or args.max_mib < args.min_mib:
        print("invalid min/max MiB", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    seed_value = args.seed if args.seed is not None else rng.randrange(1 << 32)
    rng = random.Random(seed_value)
    scratch.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(scratch, 0o700)

    bucket = args.bucket or f"ci-large-roundtrip-{seed_value % 100000}"
    report_path = Path(args.report) if args.report else (scratch / "report.json")
    originals = scratch / "originals"
    restored = scratch / "restored"
    originals.mkdir(parents=True, exist_ok=True)
    restored.mkdir(parents=True, exist_ok=True)

    print(f"Endpoint: {endpoint}")
    print(f"Bucket:   {bucket}")
    print(f"Seed:     {seed_value}")
    print(f"Scratch:  {scratch}")

    client = make_client(endpoint, access_key, secret_key)
    try:
        client.create_bucket(Bucket=bucket)
    except Exception as exc:
        print(f"create_bucket: {type(exc).__name__} (continuing if it exists)")

    sizes = [rng.randint(args.min_mib * 1024 * 1024,
                         args.max_mib * 1024 * 1024)
             for _ in range(args.count)]

    records = []
    total = 0
    for index, size in enumerate(sizes, start=1):
        filename = f"large-{index:02d}.bin"
        original = originals / filename
        digest = generate_file(original, size, rng)
        records.append({
            "index": index,
            "key": f"roundtrip/{filename}",
            "size": size,
            "sha256": digest,
            "original_path": str(original),
        })
        total += size
        print(f"[{index:2d}/{args.count}] generated {size / (1024**3):.3f} GiB "
              f"sha256={digest[:12]}...", flush=True)

    evidence = {
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "seed": seed_value,
        "endpoint": endpoint,
        "bucket": bucket,
        "count": args.count,
        "total_bytes": total,
        "files": [],
        "uploads": 0,
        "downloads": 0,
        "verified": 0,
        "failures": [],
        "garbage_collection_enabled": False,
    }

    # Upload phase.
    print("\n=== UPLOAD ===")
    for record in records:
        key = record["key"]
        original = Path(record["original_path"])
        started = time.time()
        try:
            upload_file(client, bucket, key, original)
            record["uploaded"] = True
            evidence["uploads"] += 1
            print(f"  [{record['index']:2d}] uploaded {key} "
                  f"({time.time() - started:.1f}s)", flush=True)
        except Exception as exc:
            record["uploaded"] = False
            evidence["failures"].append({
                "phase": "upload", "key": key,
                "error": type(exc).__name__,
            })
            print(f"  [{record['index']:2d}] UPLOAD FAILED: {type(exc).__name__}",
                  file=sys.stderr, flush=True)

    # Delete originals.
    print("\n=== DELETE ORIGINALS ===")
    for record in records:
        if record.get("uploaded"):
            Path(record["original_path"]).unlink(missing_ok=True)
    print("  originals removed from local disk")

    # Download + verify phase.
    print("\n=== DOWNLOAD + VERIFY ===")
    for record in records:
        key = record["key"]
        if not record.get("uploaded"):
            evidence["files"].append({
                "index": record["index"], "size": record["size"],
                "uploaded": False, "verified": False,
            })
            continue
        destination = restored / key.split("/")[-1]
        destination.parent.mkdir(parents=True, exist_ok=True)
        started = time.time()
        try:
            size = download_file(client, bucket, key, destination)
            actual = sha256_file(destination)
            ok = (size == record["size"] and actual == record["sha256"])
            record["downloaded"] = True
            record["downloaded_size"] = size
            record["downloaded_sha256"] = actual
            record["verified"] = ok
            if ok:
                evidence["verified"] += 1
                evidence["downloads"] += 1
                print(f"  [{record['index']:2d}] verified {key} "
                      f"({time.time() - started:.1f}s)", flush=True)
            else:
                evidence["failures"].append({
                    "phase": "verify", "key": key,
                    "expected_size": record["size"], "actual_size": size,
                    "expected_sha256": record["sha256"],
                    "actual_sha256": actual,
                })
                print(f"  [{record['index']:2d}] MISMATCH {key}",
                      file=sys.stderr, flush=True)
            if not args.keep:
                destination.unlink(missing_ok=True)
        except Exception as exc:
            record["downloaded"] = False
            evidence["failures"].append({
                "phase": "download", "key": key,
                "error": type(exc).__name__,
            })
            print(f"  [{record['index']:2d}] DOWNLOAD FAILED: {type(exc).__name__}",
                  file=sys.stderr, flush=True)

    for record in records:
        evidence["files"].append({
            "index": record["index"],
            "size": record["size"],
            "sha256": record["sha256"],
            "uploaded": bool(record.get("uploaded")),
            "downloaded": bool(record.get("downloaded")),
            "verified": bool(record.get("verified")),
        })

    with open(report_path, "w", encoding="utf-8") as handle:
        json.dump(evidence, handle, indent=2, sort_keys=True)
        handle.write("\n")
    if os.name != "nt":
        os.chmod(report_path, 0o600)

    print("\n=== SUMMARY ===")
    print(f"  files:       {args.count}")
    print(f"  uploads:     {evidence['uploads']}")
    print(f"  downloads:   {evidence['downloads']}")
    print(f"  verified:    {evidence['verified']}")
    print(f"  failures:    {len(evidence['failures'])}")
    print(f"  total bytes: {total}")
    print(f"  report:      {report_path}")
    print(f"  garbage_collection_enabled: false")

    if evidence["failures"] or evidence["verified"] != args.count:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
