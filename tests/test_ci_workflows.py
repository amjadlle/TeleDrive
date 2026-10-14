"""Contract checks for the GitHub Actions pipeline itself.

These run without GitHub secrets or live Telegram access and catch accidental
false-green E2E jobs, unsafe release permissions, and missing test coverage.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
EXPECTED = (
    "security-ci.yml",
    "storage-api-ci.yml",
    "database-ci.yml",
    "desktop-ci.yml",
    "telegram-storage-e2e.yml",
    "telegram-bot-e2e.yml",
    "release.yml",
)


def workflow(name: str):
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"),
                     Loader=yaml.BaseLoader)


def step(job, name: str):
    return next(s for s in job["steps"] if s["name"] == name)


@pytest.mark.parametrize("name", EXPECTED)
def test_workflow_parses_and_has_jobs_with_no_continue_on_error(name):
    path = WORKFLOWS / name
    assert path.is_file(), f"Missing {name}"
    doc = workflow(name)
    assert isinstance(doc, dict) and isinstance(doc.get("jobs"), dict)
    assert doc["jobs"], name
    assert not re.search(r"(?m)^\s*continue-on-error:\s*true", path.read_text())


@pytest.mark.parametrize("name", EXPECTED)
def test_all_external_actions_are_pinned_to_full_commit_sha(name):
    path = WORKFLOWS / name
    for action in re.findall(r"(?m)^\s*uses:\s*(\S+)", path.read_text()):
        if action.startswith("./"):
            continue
        assert re.fullmatch(r".+@[0-9a-f]{40}", action), (name, action)


@pytest.mark.parametrize("name,job_name,test_file", (
    ("telegram-storage-e2e.yml", "telegram-roundtrip",
     "tests/test_telegram_storage_e2e.py"),
    ("telegram-bot-e2e.yml", "telegram-bot-roundtrip",
     "tests/test_telegram_bot_e2e.py"),
))
def test_live_e2e_is_push_manual_only_fails_closed_and_never_aborts_cleanup(
    name, job_name, test_file
):
    doc = workflow(name)
    events = doc["on"]
    assert "workflow_dispatch" in events
    assert "push" in events and "pull_request" not in events
    assert doc["permissions"]["contents"] == "read"
    assert doc["concurrency"]["cancel-in-progress"] == "false"
    assert (ROOT / test_file).is_file()

    job = doc["jobs"][job_name]
    checks = next(s for s in job["steps"] if s.get("id") == "configured")
    script = checks["run"]
    assert "::error::" in script and "exit 1" in script
    assert 'enabled=false' not in script  # no misleading green workflow
    assert 'enabled=true' in script
    assert any(test_file in s.get("run", "") for s in job["steps"])
    assert not any(s.get("continue-on-error") == "true" for s in job["steps"])


def test_release_requires_regressions_before_build_and_tag_before_publication():
    doc = workflow("release.yml")
    assert doc["permissions"]["contents"] == "read"
    assert "tags" in doc["on"]["push"]
    jobs = doc["jobs"]
    verify = jobs["verify"]
    assert "git merge-base --is-ancestor" in "\n".join(
        s.get("run", "") for s in verify["steps"]
    )
    assert any("pytest" in s.get("run", "") for s in verify["steps"])
    assert any("pip-audit" in s.get("run", "") for s in verify["steps"])
    assert jobs["build"]["needs"] == "verify"
    assert jobs["publish"]["needs"] == "build"
    assert "github.ref_type == 'tag'" in jobs["publish"]["if"]
    assert jobs["publish"]["permissions"]["contents"] == "write"


def test_security_includes_bot_target_and_api_tests():
    doc = workflow("security-ci.yml")
    assert doc["permissions"]["contents"] == "read"
    scripts = "\n".join(s.get("run", "") for s in doc["jobs"]["security"]["steps"])
    for path in (
        "tests/test_ci_workflows.py",
        "tests/test_bot_storage.py",
        "tests/test_telegram_target_resolution.py",
        "tests/test_storage_recovery.py",
        "tests/test_api_keys.py",
        "tests/test_rest_api.py",
        "tests/test_s3_sigv4_defense.py",
        "tests/test_s3_p0_acceptance.py",
        "tests/test_s3_p0_final.py",
    ):
        assert path in scripts
    for tool in ("pip-audit", "bandit", "docker build", "trivy"):
        if tool == "trivy":
            assert any("trivy-action@" in s.get("uses", "")
                       for s in doc["jobs"]["security"]["steps"])
        else:
            assert tool in scripts


def test_db_and_desktop_matrix_and_storage_contracts_are_present():
    db = workflow("database-ci.yml")["jobs"]["database"]
    assert {"mysql", "postgres"} <= set(db["services"])
    assert any("tests/test_database_live.py" in s.get("run", "")
               and "tests/test_storage_engine.py" in s.get("run", "")
               and "tests/test_storage_recovery.py" in s.get("run", "")
               for s in db["steps"])

    desktop = workflow("desktop-ci.yml")["jobs"]["desktop"]
    assert set(desktop["strategy"]["matrix"]["os"]) == {
        "ubuntu-latest", "windows-latest"
    }
    steps = "\n".join(s.get("run", "") for s in desktop["steps"])
    assert "tests/test_desktop_ui.py" in steps
    assert "tests/test_storage_engine.py" in workflow("desktop-ci.yml")["on"]["push"]["paths"]
    assert "scripts/build-linux.sh" in steps
    assert "scripts/build.ps1" in steps

    api = workflow("storage-api-ci.yml")["jobs"]["storage-api"]
    commands = "\n".join(s.get("run", "") for s in api["steps"])
    for path in ("tests/test_rest_api.py", "tests/test_rest_transfer.py",
                 "tests/test_storage_catalog.py", "tests/test_bot_storage.py",
                 "tests/test_p0_security_matrix.py"):
        assert path in commands


def test_live_multipart_push_is_explicitly_opted_in():
    bot = workflow("telegram-bot-e2e.yml")
    job = bot["jobs"]["telegram-bot-roundtrip"]
    expression = job["env"]["TELEDRIVE_E2E_MULTIPART"]
    assert "inputs.run_multipart" in expression
    assert "[p0-live-multipart]" in expression
    assert "github.event_name == 'push'" in expression
    assert job["timeout-minutes"] == "15"


def test_final_p0_contract_runs_on_database_and_desktop_workflow_changes():
    # Cross-service regressions must re-run at the release SHA; passing an
    # unrelated earlier desktop/database build is not immutable signoff.
    for name in ("database-ci.yml", "desktop-ci.yml"):
        rules = workflow(name)["on"]
        assert "tests/test_s3_p0_final.py" in rules["push"]["paths"]
        assert "tests/test_s3_p0_final.py" in rules["pull_request"]["paths"]


def test_final_release_acceptance_report_triggers_all_three_filtered_ci_jobs():
    # An updated acceptance record must invalidate the previous same-SHA
    # CI baseline rather than leaving stale green checks on an older SHA.
    for name in ("database-ci.yml", "desktop-ci.yml", "storage-api-ci.yml"):
        events = workflow(name)["on"]
        assert "docs/P0_ACCEPTANCE.md" in events["push"]["paths"]
        assert "docs/P0_ACCEPTANCE.md" in events["pull_request"]["paths"]


def test_status_audit_rechecks_every_filtered_release_workflow():
    for name in ("database-ci.yml", "desktop-ci.yml", "storage-api-ci.yml"):
        triggers = workflow(name)["on"]
        for event in ("push", "pull_request"):
            assert "docs/IMPLEMENTATION_STATUS.md" in triggers[event]["paths"]
    assert "docs/IMPLEMENTATION_STATUS.md" in workflow("release.yml")["on"]["push"]["paths"]


def test_storage_ci_retriggers_when_legacy_migration_or_db_lease_contract_changes():
    # A green CI for a different SHA must not be mistaken for passing the
    # updated legacy migration tests or the SQL lease implementation.
    events = workflow("storage-api-ci.yml")["on"]
    for event in ("push", "pull_request"):
        paths = set(events[event]["paths"])
        assert {
            "teledrive/db.py",
            "teledrive/uploader.py",
            "tests/test_legacy_bridge.py",
            "tests/test_legacy_import_admin.py",
        } <= paths
    command = "\\n".join(
        x.get("run", "") for x in workflow("storage-api-ci.yml")["jobs"]["storage-api"]["steps"]
    )
    assert "tests/test_legacy_bridge.py" in command
    assert "tests/test_legacy_import_admin.py" in command

def test_real_bot_soak_is_explicitly_opted_in_and_never_silently_skipped():
    bot = workflow("telegram-bot-e2e.yml")
    assert bot["on"]["workflow_dispatch"]["inputs"]["run_soak"]["type"] == "boolean"
    script = bot["jobs"]["telegram-bot-roundtrip"]["steps"]
    assert any("tests/test_telegram_bot_e2e.py" in s.get("run", "") for s in script)
    expression = bot["jobs"]["telegram-bot-roundtrip"]["env"]["TELEDRIVE_E2E_SOAK"]
    assert "inputs.run_soak" in expression
    assert "[p1-live-soak]" in expression
    assert "github.event_name == 'push'" in expression
    assert bot["concurrency"]["cancel-in-progress"] == "false"
    assert bot["jobs"]["telegram-bot-roundtrip"]["timeout-minutes"] == "25"


def test_bot_send_intent_contract_and_operator_health_trigger_correct_ci():
    storage = workflow("storage-api-ci.yml")
    for event in ("push", "pull_request"):
        paths = storage["on"][event]["paths"]
        assert "teledrive/storage/**" in paths
        assert "web/**" in paths
        assert "tests/test_bot_storage.py" in paths
        assert "tests/test_storage_ops_health.py" in paths or "tests/test_storage_*.py" in paths
    executed = "\\n".join(
        step.get("run", "") for step in storage["jobs"]["storage-api"]["steps"]
    )
    assert "tests/test_bot_storage.py" in executed
    assert "tests/test_storage_ops_health.py" in executed
    security_steps = "\\n".join(
        step.get("run", "") for step in workflow("security-ci.yml")["jobs"]["security"]["steps"]
    )
    assert "tests/test_bot_storage.py" in security_steps

def test_production_acceptance_dr_ack_cutover_gates_are_run_in_ci():
    """Safety-critical operator artifacts must stay in non-skipped CI gates."""
    for filename in ("storage-api-ci.yml", "security-ci.yml", "release.yml"):
        content = (WORKFLOWS / filename).read_text(encoding="utf-8")
        for path in (
            "tests/test_bot_receipt_reconcile.py",
            "tests/test_bot_rate_slots.py",
            "tests/test_telegram_dr_rehearsal.py",
            "tests/test_legacy_cutover_acceptance.py",
        ):
            assert path in content, (filename, path)


def test_explicit_multi_process_live_postgres_bot_acceptance_not_auto_spammed():
    name = "telegram-multiworker-e2e.yml"
    path = WORKFLOWS / name
    assert path.is_file()
    content = path.read_text(encoding="utf-8")
    wf = workflow(name)
    assert "workflow_dispatch" in wf["on"]
    assert "push" in wf["on"] and "pull_request" not in wf["on"]
    assert wf["permissions"]["contents"] == "read"
    job = wf["jobs"]["three-process-telegram"]
    assert "concurrency" not in wf  # skipped opt-in may not evict Bot queue
    assert job["concurrency"]["cancel-in-progress"] == "false"
    assert job["concurrency"]["group"] == workflow(
        "telegram-bot-e2e.yml"
    )["concurrency"]["group"]
    assert "postgres" in job["services"]
    assert "postgres:17-alpine" in job["services"]["postgres"]["image"]
    assert "[p1-live-multiworker]" in job["if"]
    assert "workflow_dispatch" in job["if"]
    assert "TELEDRIVE_E2E_BOT_TOKEN" in job["env"]
    assert "TELEDRIVE_E2E_BOT_CHAT_ID" in job["env"]
    assert "DB_TEST_POSTGRES_URL" in job["env"]
    assert job["env"]["TELEDRIVE_E2E_MULTIWORKER"] == "1"
    assert any("tests/test_telegram_bot_multiworker_e2e.py" in step.get(
        "run", "") for step in job["steps"])
    assert "continue-on-error" not in content
    for action in re.findall(r"(?m)^\s*uses:\s*(\S+)", content):
        assert re.fullmatch(r".+@[0-9a-f]{40}", action)


def test_cross_client_bot_ack_requires_opt_in():
    wf = workflow("telegram-bot-e2e.yml")
    expressions = wf["jobs"]["telegram-bot-roundtrip"]["env"]
    assert "inputs.run_ack_proof" in expressions["TELEDRIVE_E2E_CROSS_CLIENT"]
    assert "[p1-live-ack-proof]" in expressions["TELEDRIVE_E2E_CROSS_CLIENT"]
    assert "TELEDRIVE_E2E_API_ID" in expressions
    assert "TELEDRIVE_E2E_API_HASH" in expressions
    assert "TELEDRIVE_E2E_SESSION" in expressions
