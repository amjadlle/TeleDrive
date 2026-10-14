"""P1 production telemetry gate: safe network configuration and no fake signoff."""
from __future__ import annotations

import pytest

from scripts.monitor_p1_soak import (
    collect, parse_hosts, selected_metrics, summarize,
)


def fixture_metrics(ack_count=0, **overrides):
    data = {
        "bot_explicit_429_total": ack_count,
        "unresolved_bot_send_count": 0,
        "unjournaled_bot_ack_count": 0,
        "stale_pending_sample_count": 0,
        "stale_multipart_sample_count": 0,
        "job_states": {"queued": 0, "running": 0, "failed": 0},
        "samples_truncated": False,
        "physical_deletion_enabled": False,
    }
    data.update(overrides)
    return data


def test_two_distinct_https_health_endpoints_required():
    values = parse_hosts(
        '[{"label":"west","url":"https://west.example.org"},'
        '{"label":"east","url":"https://east.example.org/"}]'
    )
    assert [host["label"] for host in values] == ["west", "east"]
    assert [host["origin"] for host in values] == [
        "https://west.example.org", "https://east.example.org",
    ]


@pytest.mark.parametrize("input_value", [
    "[]",
    '[{"label":"alone","url":"https://a.example.org"}]',
    '[{"label":"one","url":"http://a.example.org"},'
    '{"label":"two","url":"https://b.example.org"}]',
    '[{"label":"one","url":"https://user:pass@a.example.org"},'
    '{"label":"two","url":"https://b.example.org"}]',
    '[{"label":"one","url":"https://a.example.org/redirect"},'
    '{"label":"two","url":"https://b.example.org"}]',
    '[{"label":"one","url":"https://a.example.org"},'
    '{"label":"one","url":"https://b.example.org"}]',
    '[{"label":"one","url":"https://a.example.org"},'
    '{"label":"two","url":"https://A.EXAMPLE.ORG"}]',
    '[{"label":"one","url":"https://a.example.org?secret=1"},'
    '{"label":"two","url":"https://b.example.org"}]',
])
def test_unsafe_host_configuration_fails_closed(input_value):
    with pytest.raises(ValueError):
        parse_hosts(input_value)


@pytest.mark.parametrize("bad", [
    {"bot_explicit_429_total": True},
    {"bot_explicit_429_total": -1},
    {"job_states": {"queued": "one"}},
    {"samples_truncated": True},
    {"physical_deletion_enabled": True},
])
def test_unsafe_metrics_never_count_as_accepted(bad):
    with pytest.raises(ValueError):
        selected_metrics(fixture_metrics(**bad))


def test_hour_soak_simulation_observes_shared_counter_without_claiming_signoff():
    # CI only compresses the telemetry loop; CLI enforces a real hour.
    hosts = [
        {"label": "west", "origin": "https://west.example.org"},
        {"label": "east", "origin": "https://east.example.org"},
    ]
    clock_value = [0.0]

    def clock():
        return clock_value[0]

    def sleep(seconds):
        clock_value[0] += seconds

    def probe(_origin):
        return fixture_metrics(3 if clock_value[0] >= 2 else 1)

    report = collect(hosts, probe, duration=4, interval=2, clock=clock, sleep=sleep)
    assert report["telemetry_gate_passed"] is True
    assert report["sample_count"] == 3
    assert report["explicit_429_deltas_by_host"] == {"west": 2, "east": 2}
    assert report["production_acceptance_signed_off"] is False
    assert report["physical_host_independence_verified"] is False
    assert report["original_media_restore_verified"] is False
    assert report["legacy_cutover_approved"] is False
    assert report["telegram_mutations_performed"] == 0
    assert "url" not in str(report)
    assert "password" not in str(report)


@pytest.mark.parametrize("scenario", [
    "no-actual-429",
    "second-host-disagrees",
    "unresolved-bot-ack",
    "backlog-not-drained",
    "counter-decreased",
    "missed-probe",
])
def test_unsafe_soak_evidence_must_not_pass(scenario):
    def observation(time, left=1, right=1, **changes):
        a = selected_metrics(fixture_metrics(left, **changes))
        b = selected_metrics(fixture_metrics(right, **changes))
        return {"elapsed": time, "hosts": {"west": a, "east": b}}

    beginning = observation(0)
    ending = observation(3600, left=3, right=3)
    if scenario == "no-actual-429":
        ending = observation(3600)
    elif scenario == "second-host-disagrees":
        ending = observation(3600, left=3, right=2)
    elif scenario == "unresolved-bot-ack":
        ending = observation(3600, left=3, right=3, unresolved_bot_send_count=1)
    elif scenario == "backlog-not-drained":
        ending = observation(3600, left=3, right=3,
                             job_states={"queued": 1, "running": 0, "failed": 0})
    elif scenario == "counter-decreased":
        beginning = observation(0, left=3, right=3)
        ending = observation(3600, left=2, right=2)
    samples = [beginning, ending]
    if scenario == "missed-probe":
        with pytest.raises(ValueError, match="Unobserved gap"):
            summarize(samples, duration=3600, interval=60)
    else:
        summary = summarize(samples, duration=3600, interval=3600)
        assert summary["telemetry_gate_passed"] is False
        assert summary["production_acceptance_signed_off"] is False
