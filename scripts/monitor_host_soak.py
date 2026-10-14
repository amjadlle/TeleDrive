#!/usr/bin/env python3
"""Read-only multi-host P1 production-soak telemetry receipt.

This command NEVER uploads/deletes Telegram messages or modifies SQL.
It cannot attest physical host independence, original-media DR, or legacy
cutover; those require separate signed operator evidence.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path


HEALTH = "/api/v1/admin/storage/health"


def parse_hosts(raw: str) -> list[dict[str, str]]:
    hosts = json.loads(raw)
    if not isinstance(hosts, list) or not 2 <= len(hosts) <= 8:
        raise ValueError("Specify two to eight separate HTTPS health endpoints")
    result = []
    names, peers = set(), set()
    for entry in hosts:
        if not isinstance(entry, dict) or set(entry) != {"label", "url"}:
            raise ValueError("Invalid host entry")
        label, url = entry["label"], entry["url"]
        if (not isinstance(label, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", label)
                or not isinstance(url, str)):
            raise ValueError("Invalid host label or URL")
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                or parsed.port not in (None, 443)):
            raise ValueError("Soak requires clean HTTPS host origins")
        origin = "https://" + parsed.hostname.lower()
        if label in names or origin in peers:
            raise ValueError("Host labels and network endpoints must be distinct")
        names.add(label)
        peers.add(origin)
        result.append({"label": label, "origin": origin})
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Health endpoint redirected; credentials withheld")


def health_probe(origin: str, username: str, password: str) -> dict:
    # Never follow even a same-origin redirect while passing credentials.
    secret = base64.b64encode(
        (username + ":" + password).encode("utf-8")
    ).decode("ascii")
    req = urllib.request.Request(
        origin + HEALTH,
        headers={"Authorization": "Basic " + secret, "Accept": "application/json"},
    )
    with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError("Health endpoint refused the observation")
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError("Oversized health response")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Malformed health response")
    return payload


def selected_metrics(payload: dict) -> dict:
    fields = (
        "bot_explicit_429_total", "unresolved_bot_send_count",
        "unjournaled_bot_ack_count", "stale_pending_sample_count",
        "stale_multipart_sample_count",
    )
    result = {}
    for key in fields:
        value = payload.get(key)
        if type(value) is not int or value < 0:
            raise ValueError("Invalid health telemetry counter")
        result[key] = value
    states = payload.get("job_states")
    if not isinstance(states, dict):
        raise ValueError("Missing upload backlog state")
    for name in ("queued", "running", "failed"):
        value = states.get(name, 0)
        if type(value) is not int or value < 0:
            raise ValueError("Invalid upload backlog metric")
        result[name + "_jobs"] = value
    if (payload.get("samples_truncated") is not False
            or payload.get("physical_deletion_enabled") is not False):
        raise ValueError("Unsafe or truncated storage-health inventory")
    return result


def summarize(samples: list[dict], *, duration: int, interval: int) -> dict:
    if len(samples) < 2 or samples[-1]["elapsed"] < duration:
        raise ValueError("Soak did not cover the requested duration")
    if any(b["elapsed"] - a["elapsed"] > interval * 2
           for a, b in zip(samples, samples[1:])):
        raise ValueError("Unobserved gap in production soak")
    labels = list(samples[0]["hosts"])
    if len(labels) < 2 or any(set(s["hosts"]) != set(labels) for s in samples):
        raise ValueError("Host topology changed during acceptance")
    deltas = {}
    safe = True
    for label in labels:
        observations = [s["hosts"][label] for s in samples]
        counts = [x["bot_explicit_429_total"] for x in observations]
        if counts != sorted(counts):
            safe = False  # SQL changed/rolled back; cannot certify a counter.
        deltas[label] = counts[-1] - counts[0]
        last = observations[-1]
        if any(last[x] for x in (
            "unresolved_bot_send_count", "unjournaled_bot_ack_count",
            "stale_pending_sample_count", "stale_multipart_sample_count",
            "queued_jobs", "running_jobs", "failed_jobs",
        )):
            safe = False
    # Shared counter agreement is useful evidence but cannot by itself
    # prove independent physical hosts or that traffic was unmodified.
    agree = len({
        samples[-1]["hosts"][x]["bot_explicit_429_total"] for x in labels
    }) == 1
    return {
        "duration_seconds": samples[-1]["elapsed"],
        "sample_count": len(samples),
        "host_labels": labels,
        "explicit_429_deltas_by_host": deltas,
        "final_shared_counter_agreement": agree,
        "telemetry_gate_passed": safe and agree and all(v > 0 for v in deltas.values()),
        "physical_host_independence_verified": False,
        "original_media_restore_verified": False,
        "legacy_cutover_approved": False,
        "production_acceptance_signed_off": False,
        "telegram_mutations_performed": 0,
    }


def collect(hosts: list[dict], probe, *, duration: int, interval: int,
            clock=time.monotonic, sleep=time.sleep) -> dict:
    beginning = clock()
    samples = []
    while True:
        elapsed = int(clock() - beginning)
        node_metrics = {
            h["label"]: selected_metrics(probe(h["origin"]))
            for h in hosts
        }
        samples.append({"elapsed": elapsed, "hosts": node_metrics})
        if elapsed >= duration:
            break
        deadline = beginning + min(
            duration, ((len(samples)) * interval),
        )
        sleep(max(0, deadline - clock()))
    return summarize(samples, duration=duration, interval=interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=int, default=3600)
    parser.add_argument("--interval-seconds", type=int, default=60)
    args = parser.parse_args()
    if args.duration_seconds < 3600 or not 30 <= args.interval_seconds <= 600:
        parser.error("Production soak requires >=3600 seconds, 30-600s sampling")
    try:
        hosts = parse_hosts(os.environ.get("TELEDRIVE_P1_HEALTH_HOSTS", ""))
        user = os.environ["TELEDRIVE_P1_BASIC_USERNAME"]
        password = os.environ["TELEDRIVE_P1_BASIC_PASSWORD"]
        if not user or not password:
            raise ValueError("Missing protected health credentials")
        report = collect(
            hosts, lambda origin: health_probe(origin, user, password),
            duration=args.duration_seconds, interval=args.interval_seconds,
        )
        # Fail if the destination already exists. Never overwrite existing
        # audit evidence or print secrets and HTTP exception text.
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(report, out, indent=2, sort_keys=True)
            out.write("\n")
        print(json.dumps({
            "telemetry_gate_passed": report["telemetry_gate_passed"],
            "report_written": True,
            "production_acceptance_signed_off": False,
        }))
        return 0 if report["telemetry_gate_passed"] else 2
    except Exception as exc:
        parser.exit(1, "Soak verification refused: " + type(exc).__name__ + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
