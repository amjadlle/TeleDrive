"""Bounded synthetic endurance instrumentation, not production certification."""
from __future__ import annotations
import pytest
from scripts.p2_capacity_probe import percentile, run


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
