"""Fail-closed external workflow proof for mainline release requests."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

REQUIRED = {
    "01 · Quality | Security & Regression",
    "02 · Integration | Databases",
    "02 · Integration | REST & S3",
    "03 · Resilience | Docker Fault Injection",
    "04 · Live Telegram | User MTProto E2E",
    "04 · Live Telegram | Multiworker PostgreSQL E2E",
}

def check(payload: dict, sha: str) -> tuple[int, list[str]]:
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        return 1, ["Malformed Actions result"]
    selected = {}
    for run in sorted(runs, key=lambda x: (x.get("created_at", ""), x.get("id", 0)), reverse=True):
        if run.get("head_sha") == sha and run.get("name") in REQUIRED:
            selected.setdefault(run["name"], run)
    reasons, pending, failed = [], False, False
    for name in sorted(REQUIRED):
        item = selected.get(name)
        if item is None:
            reasons.append(name + ": missing")
            pending = True
        elif item.get("status") != "completed":
            reasons.append(name + ": " + str(item.get("status")))
            pending = True
        elif item.get("conclusion") != "success":
            reasons.append(name + ": " + str(item.get("conclusion")))
            failed = True
    return (1 if failed else 2 if pending else 0), reasons

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--sha", required=True)
    a = p.parse_args()
    result, reasons = check(json.loads(a.input.read_text("utf-8")), a.sha)
    for reason in reasons:
        print(reason)
    print("P0/P1 same-SHA release gate:", ("PASS", "FAIL", "WAIT")[result])
    return result

if __name__ == "__main__":
    raise SystemExit(main())
