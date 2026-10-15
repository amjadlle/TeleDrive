"""Bounded synthetic endurance instrumentation, not production certification."""
from __future__ import annotations
import pytest
from scripts.ops.capacity_probe import percentile, run


def test_percentile_is_deterministic():
    assert percentile([9, 1, 4, 3], 0.95) == 9
    assert percentile([9, 1, 4, 3], 0.5) == 3


def test_short_disposable_roundtrip_produces_truthful_report():
    result = run(duration_seconds=1, object_bytes=4097, interval_seconds=0)
    assert result["verified_roundtrips"] >= 1
    assert result["verified_bytes"] == result["verified_roundtrips"] * 4097
    assert result["p95_roundtrip_ms"] >= 0
    assert result["real_telegram_tested"] is False
    assert result["physical_hosts_tested"] is False
    assert result["production_acceptance_signed_off"] is False


@pytest.mark.parametrize("params", [
    {"duration_seconds": 0, "object_bytes": 1024},
    {"duration_seconds": 21601, "object_bytes": 1024},
    {"duration_seconds": 1, "object_bytes": 0},
    {"duration_seconds": 1, "object_bytes": 4 * 1024 * 1024 + 1},
    {"duration_seconds": 1, "object_bytes": 1, "interval_seconds": -1},
])
def test_invalid_soak_bounds_fail_closed(params):
    with pytest.raises(ValueError):
        run(**params)


def test_disk_media_retention_survives_database_restart(tmp_path):
    result = run(duration_seconds=1, object_bytes=65537,
                 interval_seconds=0, workspace=tmp_path / "disposable")
    assert result["sql_restarts"] >= 1
    assert result["historical_rereads"] >= 3
    assert result["retained_telegram_bytes"] == result["verified_bytes"]
    assert result["retained_telegram_chunks"] >= result["verified_roundtrips"]
    assert result["real_telegram_tested"] is False


def test_synthetic_message_corruption_is_visible_to_original_storage_engine(tmp_path):
    from scripts.ops.capacity_probe import IsolatedMedia
    from teledrive.db import connect_database
    from teledrive.storage.catalog import BucketCatalog
    from teledrive.storage.engine import TelegramObjectStorage, IntegrityError
    from teledrive.storage.manifest import ManifestStore
    import hashlib
    import asyncio

    scratch = tmp_path / "workspace"
    scratch.mkdir()
    media = IsolatedMedia(scratch / "telegram")
    db = connect_database(str(scratch / "db.sqlite"))
    try:
        catalog = BucketCatalog(ManifestStore(db))
        catalog.create_bucket("capacity-test", 100)
        src = scratch / "source.bin"
        src.write_bytes(b"corruption-target")
        item = asyncio.run(TelegramObjectStorage(
            catalog.manifests, media, chunk_size=3, scratch_directory=scratch,
        ).put_file("capacity-test", "broken", src))
        catalog.publish(item.id, now=100)
        src.unlink()
        media.path(1).write_bytes(b"BAD")
        with pytest.raises(IntegrityError):
            asyncio.run(TelegramObjectStorage(
                catalog.manifests, media, chunk_size=3, scratch_directory=scratch,
            ).download_to_path(item.id, scratch / "output"))
        assert not (scratch / "output").exists()
    finally:
        db.close()
