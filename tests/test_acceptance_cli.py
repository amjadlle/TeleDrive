"""Offline, no-secret, no-network regressions for P1 operator wizard."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import acceptance as w


def test_report_is_unique_private_and_no_automatic_signoff(tmp_path):
    record = {"stages": {"multi_host_soak": {"status": "PASS", "reason": "ok"}}}
    first = w.save_report(tmp_path, record)
    second = w.save_report(tmp_path, record)
    assert first != second
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    saved = json.loads(first.read_text())
    assert saved["production_acceptance_signed_off"] is False
    assert saved["operator_review_required"] is True
    assert saved["physical_telegram_deletions"] == 0


def test_github_e2e_only_accepts_real_success_not_skipped(monkeypatch):
    mocked = [
        {"workflowName": name, "conclusion": conclusion,
         "headSha": "ab" * 20, "databaseId": i + 1}
        for i, (name, conclusion) in enumerate([
            (w.WORKFLOWS["user"], "success"),
            (w.WORKFLOWS["multiworker"], "success"),
        ])
    ]
    monkeypatch.setattr(w.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(w, "safe_run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(mocked)))
    result = {"stages": {}}
    w.github_stage(result)
    assert result["stages"]["github_e2e"]["status"] == "PASS"
    assert "bot" not in result["github"]


@pytest.mark.parametrize("bad", ["not-a-number", "0", "-5"])
def test_user_channel_fails_without_a_valid_integer_before_telegram(monkeypatch, bad):
    prompts = iter(["account", "target", "bucket", "key"])
    monkeypatch.setattr(w, "secret", lambda label: bad if "kanal" in label else "fake-value")
    monkeypatch.setattr(w, "ask", lambda label, default="": next(prompts))
    with pytest.raises((ValueError, StopIteration)):
        w.ask_user_identity()


def test_soak_unavailable_is_blocked_not_green(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "yes", lambda *args, **kwargs: False)
    result = {"stages": {}}
    w.soak_stage(result, tmp_path)
    assert result["stages"]["multi_host_soak"]["status"] == "BLOCKED"
    assert not list(tmp_path.iterdir())


def test_soak_redacts_origins_and_basic_auth(monkeypatch, tmp_path):
    approvals = iter([True, True])
    addresses = iter(["https://node-a.example.org", "https://node-b.example.org", "admin"])
    monkeypatch.setattr(w, "yes", lambda *args, **kwargs: next(approvals))
    monkeypatch.setattr(w, "ask", lambda *args, **kwargs: next(addresses))
    monkeypatch.setattr(w, "secret", lambda *args, **kwargs: "very-secret-password")

    def run(args, *, env=None, **kwargs):
        assert env["TELEDRIVE_P1_BASIC_PASSWORD"] == "very-secret-password"
        target = Path(args[args.index("--output") + 1])
        target.write_text(json.dumps({
            "sample_count": 61,
            "duration_seconds": 3600,
            "telemetry_gate_passed": True,
            "final_shared_counter_agreement": True,
            "explicit_429_deltas_by_host": {"host-a": 3, "host-b": 3},
        }))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(w, "safe_run", run)
    record = {"stages": {}}
    w.soak_stage(record, tmp_path)
    assert record["stages"]["multi_host_soak"]["status"] == "PASS"
    assert "very-secret-password" not in json.dumps(record)
    assert "example.org" not in json.dumps(record)
    assert "production_acceptance_signed_off" not in json.dumps(record)


def test_no_native_restore_to_same_host_even_different_database(tmp_path):
    credentials = {"host": "db.internal", "port": 5432, "dbname": "prod",
                   "user": "owner", "password": "secret"}
    staging = dict(credentials, dbname="staging")
    with pytest.raises(ValueError, match="Aynı DB sunucusuna"):
        w.native_restore("postgresql", credentials, staging,
                         tmp_path / "backup.dump", tmp_path)
    assert not list(tmp_path.iterdir())


def test_mtproto_ack_wizard_never_uses_apply(monkeypatch, tmp_path):
    session = tmp_path / "session"
    session.write_text("string-session")
    session.chmod(0o600)
    monkeypatch.setattr(w, "yes", lambda *args, **kwargs: True)
    answers = iter(["account", "object-id", "0", "api-id"])
    monkeypatch.setattr(w, "ask", lambda *args, **kwargs: next(answers))
    def secret(label):
        if "kanal" in label:
            return "-1001234567890"
        if "SQL" in label or "veritabanı" in label:
            return "/tmp/disposable-staging.db"
        if "Telegram message" in label:
            return "12345"
        if "SHA-256" in label:
            return "a" * 64
        if "boyutu" in label:
            return "7"
        return "not-real-secret"
    monkeypatch.setattr(w, "secret", secret)
    from scripts import reconcile_mtproto_receipt
    async def fake_verify(args):
        assert args.apply is False
        assert args.database.endswith("disposable-staging.db")
        return {"verified": True, "applied": False}
    monkeypatch.setattr(reconcile_mtproto_receipt, "run", fake_verify)
    result = {"stages": {}}
    w.ack_stage(result, session)
    assert result["stages"]["mtproto_ack"]["status"] == "PASS"
    assert result["ack"]["applied"] is False
    assert os.environ.get("TELEDRIVE_RECONCILE_SESSION") != "string-session"


def test_session_step_uses_async_login_and_never_displays_session(monkeypatch, tmp_path):
    path = tmp_path / "session"
    async def fake_login(output):
        assert output == path
        return True
    monkeypatch.setattr(w, "yes", lambda *args, **kwargs: True)
    monkeypatch.setattr(w, "session_login", fake_login)
    record = {"stages": {}}
    w.session_stage(record, path)
    assert record["stages"]["telegram_session"]["status"] == "PASS"


def test_script_has_tty_gate_instead_of_stdin_heredoc():
    assert w.main.__code__.co_name == "main"
    assert "--evidence-dir" in w.main.__code__.co_consts
    assert "python - <<PY" in (w.__doc__ or "") or "stdin" in (w.__doc__ or "")
