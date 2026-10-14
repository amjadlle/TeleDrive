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
    "telegram-multiworker-e2e.yml",
    "release.yml",
    "docker-fault-injection-ci.yml",
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
    assert "tests/test_desktop_support.py" in steps
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
    assert job["timeout-minutes"] == "25"


def test_final_p0_contract_runs_on_every_database_and_desktop_push():
    for name in ("database-ci.yml", "desktop-ci.yml", "storage-api-ci.yml"):
        rules = workflow(name)["on"]
        for event in ("push", "pull_request"):
            assert event in rules
            assert "paths" not in rules[event]
            assert "paths-ignore" not in rules[event]
    commands = "\n".join(
        s.get("run", "")
        for s in workflow("storage-api-ci.yml")["jobs"]["storage-api"]["steps"]
    )
    assert "tests/test_s3_p0_final.py" in commands



def test_acceptance_report_updates_cannot_skip_required_ci():
    for name in ("database-ci.yml", "desktop-ci.yml", "storage-api-ci.yml"):
        events = workflow(name)["on"]
        assert "paths" not in events["push"]
        assert "paths" not in events["pull_request"]



def test_status_audit_rechecks_every_release_workflow():
    for name in ("database-ci.yml", "desktop-ci.yml", "storage-api-ci.yml"):
        triggers = workflow(name)["on"]
        for event in ("push", "pull_request"):
            assert "paths" not in triggers[event]
    release = workflow("release.yml")["on"]["push"]
    assert "paths" not in release
    assert "tags" in release and "branches" in release



def test_storage_ci_runs_legacy_migration_on_every_change():
    events = workflow("storage-api-ci.yml")["on"]
    for event in ("push", "pull_request"):
        assert "paths" not in events[event]
    command = "\n".join(
        x.get("run", "") for x in workflow("storage-api-ci.yml")["jobs"]["storage-api"]["steps"]
    )
    for path in (
        "tests/test_legacy_bridge.py",
        "tests/test_legacy_import_admin.py",
        "tests/test_storage_jobs.py",
    ):
        assert path in command


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


def test_bot_send_intent_contract_and_operator_health_run_on_every_push():
    storage = workflow("storage-api-ci.yml")
    for event in ("push", "pull_request"):
        assert "paths" not in storage["on"][event]
    executed = "\n".join(
        s.get("run", "") for s in storage["jobs"]["storage-api"]["steps"]
    )
    for test_file in (
        "tests/test_bot_storage.py",
        "tests/test_storage_ops_health.py",
        "tests/test_bot_receipt_reconcile.py",
        "tests/test_bot_rate_slots.py",
    ):
        assert test_file in executed
    security_steps = "\n".join(
        s.get("run", "") for s in workflow("security-ci.yml")["jobs"]["security"]["steps"]
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
    assert job["concurrency"]["group"] != workflow(
        "telegram-storage-e2e.yml"
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

@pytest.mark.parametrize("name", [
    "telegram-bot-e2e.yml", "telegram-multiworker-e2e.yml",
])
def test_bot_target_is_masked_secret_before_any_live_step(name):
    """Reject accidental reintroduction of clear-text Actions channel IDs."""
    raw = (WORKFLOWS / name).read_text(encoding="utf-8")
    doc = workflow(name)
    job = (doc["jobs"]["telegram-bot-roundtrip"]
           if name == "telegram-bot-e2e.yml"
           else doc["jobs"]["three-process-telegram"])
    target = job["env"]["TELEDRIVE_E2E_BOT_CHAT_ID"]
    assert target == "${{ secrets.TELEDRIVE_E2E_BOT_CHAT_ID }}"
    assert "vars.TELEDRIVE_E2E_BOT_CHAT_ID" not in raw
    assert not re.search(r"(?m)^\s*TELEDRIVE_E2E_BOT_CHAT_ID:\s*['\"]?-100[0-9]+", raw)
    mask = job["steps"][0]
    assert mask["name"] == "Mask dedicated Telegram test target"
    assert mask["shell"] == "bash"
    assert "::add-mask::$TELEDRIVE_E2E_BOT_CHAT_ID" in mask["run"]
    assert "exit 1" in mask["run"]
    assert mask["run"].index("exit 1") < mask["run"].index("::add-mask::")
    assert job["steps"][1]["name"] == "Checkout"


def test_telegram_setup_writes_bot_target_as_secret_and_avoids_value_output():
    content = (ROOT / "scripts" / "setup_telegram_e2e.py").read_text("utf-8")
    assert 'store_secret(repo, "TELEDRIVE_E2E_BOT_CHAT_ID"' in content
    assert 'store_variable(repo, "TELEDRIVE_E2E_BOT_CHAT_ID"' not in content
    assert "def secure_existing_bot_chat_id(" in content
    assert '["gh", "variable", "delete"' in content
    assert "--secure-bot-chat-id" in content


def test_telethon_optional_numeric_target_uses_secret_not_public_variable():
    """A numeric user-session peer ID needs masking as much as a Bot peer."""
    raw = (WORKFLOWS / "telegram-storage-e2e.yml").read_text("utf-8")
    job = workflow("telegram-storage-e2e.yml")["jobs"]["telegram-roundtrip"]
    assert job["env"]["TELEDRIVE_E2E_TARGET"] == "${{ secrets.TELEDRIVE_E2E_TARGET }}"
    assert "vars.TELEDRIVE_E2E_TARGET" not in raw
    assert job["steps"][0]["name"] == "Mask optional Telegram user target"
    assert "::add-mask::$TELEDRIVE_E2E_TARGET" in job["steps"][0]["run"]
    assert job["steps"][1]["name"] == "Checkout"


def test_e2e_chat_bootstrap_migration_hides_value_and_removes_old_variable(
    monkeypatch, capsys,
):
    from scripts import setup_telegram_e2e as setup

    example_private_peer = "-100" + "987654321012"
    observed = {}
    monkeypatch.setattr(setup, "require_github_cli", lambda repo: None)
    from subprocess import CompletedProcess
    subprocess_calls = []

    def masked_variable_read(cmd, **kwargs):
        subprocess_calls.append((cmd, kwargs))
        return CompletedProcess(cmd, 0, stdout=example_private_peer.encode(), stderr=b"")

    monkeypatch.setattr(setup.subprocess, "run", masked_variable_read)
    monkeypatch.setattr(
        setup.getpass, "getpass",
        lambda prompt: pytest.fail("Existing Actions variable must not be prompted"),
    )
    monkeypatch.setattr(
        setup, "store_secret",
        lambda repo, key, value: observed.update(
            {"repository": repo, "name": key, "private_value": value}
        ),
    )
    monkeypatch.setattr(
        setup, "remove_public_bot_chat_variable",
        lambda repo: observed.update({"removed_variable_from": repo}),
    )
    setup.secure_existing_bot_chat_id("some-owner/some-repo")
    assert observed == {
        "repository": "some-owner/some-repo",
        "name": "TELEDRIVE_E2E_BOT_CHAT_ID",
        "private_value": example_private_peer,
        "removed_variable_from": "some-owner/some-repo",
    }
    captured = capsys.readouterr()
    assert example_private_peer not in captured.out + captured.err
    assert "Saved GitHub Actions secret" in captured.out
    assert subprocess_calls == [(
        ["gh", "variable", "get", "TELEDRIVE_E2E_BOT_CHAT_ID",
         "--repo", "some-owner/some-repo"],
        {"capture_output": True, "check": False},
    )]


@pytest.mark.parametrize("bad", [
    "", "not-a-channel", "123456789", "-100", "-100NOTNUMERIC",
    "-1001111", "-10012345678901234567890",
])
def test_e2e_chat_bootstrap_rejects_invalid_private_channel_before_write(
    bad, monkeypatch,
):
    from scripts import setup_telegram_e2e as setup

    writes = []
    monkeypatch.setattr(setup, "require_github_cli", lambda repo: None)
    from subprocess import CompletedProcess
    def no_existing_variable(cmd, **kwargs):
        if cmd[:3] == ["gh", "variable", "get"]:
            return CompletedProcess(cmd, 1, stdout=b"", stderr=b"")
        assert cmd[:3] == ["gh", "variable", "list"]
        return CompletedProcess(cmd, 0, stdout=b"[]", stderr=b"")

    monkeypatch.setattr(setup.subprocess, "run", no_existing_variable)
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: bad)
    monkeypatch.setattr(
        setup, "store_secret",
        lambda *args: writes.append(args),
    )
    with pytest.raises(ValueError, match="numeric private channel"):
        setup.secure_existing_bot_chat_id("some-owner/some-repo")
    assert not writes


@pytest.mark.parametrize("present,delete_success", [
    (True, True), (True, False), (False, True),
])
def test_e2e_chat_variable_cleanup_never_logs_unmasked_value(
    present, delete_success, monkeypatch, capsys,
):
    from scripts import setup_telegram_e2e as setup
    from subprocess import CompletedProcess
    import json

    calls = []
    sample_id = "-100" + "987654321012"
    names = ([{"name": "TELEDRIVE_E2E_BOT_CHAT_ID"}] if present
             else [{"name": "AN_UNRELATED_VARIABLE"}])

    def mock_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:3] == ["gh", "variable", "list"]:
            refreshed = not present or (len(calls) > 1 and delete_success)
            rows = ([{"name": "AN_UNRELATED_VARIABLE"}] if refreshed else names)
            return CompletedProcess(cmd, 0, stdout=json.dumps(rows).encode(),
                                    stderr=sample_id.encode())
        assert cmd[:3] == ["gh", "variable", "delete"]
        return CompletedProcess(cmd, 0 if delete_success else 1,
                                stdout=b"", stderr=sample_id.encode())

    monkeypatch.setattr(setup.subprocess, "run", mock_run)
    if present and not delete_success:
        with pytest.raises(RuntimeError, match="could not be deleted"):
            setup.remove_public_bot_chat_variable("some-owner/some-repo")
    else:
        setup.remove_public_bot_chat_variable("some-owner/some-repo")
    assert calls[0][0] == [
        "gh", "variable", "list", "--repo", "some-owner/some-repo",
        "--json", "name",
    ]
    assert all(kwargs["capture_output"] for _, kwargs in calls)
    assert [cmd[2] for cmd, _ in calls] == (
        ["list", "delete"] if present and not delete_success else
        ["list", "delete", "list"] if present else ["list", "list"]
    )
    captured = capsys.readouterr()
    assert sample_id not in captured.out + captured.err


@pytest.mark.parametrize("kind", ["permission", "malformed-json", "wrong-shape"])
def test_e2e_chat_variable_cleanup_fails_closed_on_unverifiable_state(
    kind, monkeypatch,
):
    from scripts import setup_telegram_e2e as setup
    from subprocess import CompletedProcess
    if kind == "permission":
        result = CompletedProcess([], 1, stdout=b"", stderr=b"denied")
    elif kind == "malformed-json":
        result = CompletedProcess([], 0, stdout=b"not-json", stderr=b"")
    else:
        result = CompletedProcess([], 0, stdout=b'{"name":"x"}', stderr=b"")
    monkeypatch.setattr(setup.subprocess, "run",
                        lambda cmd, **kw: result)
    with pytest.raises(RuntimeError, match="Actions variable"):
        setup.remove_public_bot_chat_variable("some-owner/some-repo")


def test_e2e_chat_migration_rejects_untrusted_existing_variable_without_writing(
    monkeypatch,
):
    from scripts import setup_telegram_e2e as setup
    from subprocess import CompletedProcess

    monkeypatch.setattr(setup, "require_github_cli", lambda repo: None)
    monkeypatch.setattr(
        setup.subprocess, "run",
        lambda cmd, **kwargs: CompletedProcess(
            cmd, 0, stdout=b"unsafe\r\n::warning::hijack", stderr=b"",
        ),
    )
    monkeypatch.setattr(
        setup.getpass, "getpass",
        lambda prompt: pytest.fail("Malformed available variable must not be prompted"),
    )
    monkeypatch.setattr(
        setup, "store_secret",
        lambda *args: pytest.fail("Unvalidated channel ID must never be stored"),
    )
    with pytest.raises(ValueError, match="numeric private channel"):
        setup.secure_existing_bot_chat_id("some-owner/some-repo")


def test_e2e_chat_migration_unreadable_existing_variable_fails_closed(
    monkeypatch,
):
    """A permission/network read error must not silently become a prompt."""
    from subprocess import CompletedProcess
    from scripts import setup_telegram_e2e as setup

    monkeypatch.setattr(setup, "require_github_cli", lambda repo: None)
    commands = []

    def read_failure(cmd, **kwargs):
        assert kwargs["capture_output"] is True
        commands.append(cmd[:3])
        if cmd[:3] == ["gh", "variable", "get"]:
            return CompletedProcess(cmd, 1, stdout=b"", stderr=b"denied")
        assert cmd[:3] == ["gh", "variable", "list"]
        return CompletedProcess(
            cmd, 0,
            stdout=b'[{"name":"TELEDRIVE_E2E_BOT_CHAT_ID"}]',
            stderr=b"",
        )

    monkeypatch.setattr(setup.subprocess, "run", read_failure)
    monkeypatch.setattr(
        setup.getpass, "getpass",
        lambda _: pytest.fail("Do not prompt when the original variable exists"),
    )
    monkeypatch.setattr(
        setup, "store_secret",
        lambda *args: pytest.fail("Do not overwrite secrets after a read error"),
    )
    with pytest.raises(RuntimeError, match="cannot be securely read"):
        setup.secure_existing_bot_chat_id("some-owner/some-repo")
    assert commands == [
        ["gh", "variable", "get"], ["gh", "variable", "list"],
    ]


def test_e2e_chat_migration_missing_variable_uses_hidden_input(
    monkeypatch, capsys,
):
    from subprocess import CompletedProcess
    from scripts import setup_telegram_e2e as setup

    private_peer = "-100" + "123450987654"
    commands = []
    saved = []
    monkeypatch.setattr(setup, "require_github_cli", lambda repo: None)

    def missing_variable(cmd, **kwargs):
        assert kwargs["capture_output"] is True
        commands.append(cmd[:3])
        if cmd[:3] == ["gh", "variable", "get"]:
            return CompletedProcess(cmd, 1, stdout=b"", stderr=b"not found")
        assert cmd[:3] == ["gh", "variable", "list"]
        return CompletedProcess(cmd, 0, stdout=b"[]", stderr=b"")

    monkeypatch.setattr(setup.subprocess, "run", missing_variable)
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: private_peer)
    monkeypatch.setattr(
        setup, "store_secret",
        lambda repo, key, value: saved.append((repo, key, value)),
    )
    monkeypatch.setattr(setup, "remove_public_bot_chat_variable", lambda _: None)
    setup.secure_existing_bot_chat_id("some-owner/some-repo")
    assert saved == [(
        "some-owner/some-repo", "TELEDRIVE_E2E_BOT_CHAT_ID", private_peer,
    )]
    assert commands == [
        ["gh", "variable", "get"], ["gh", "variable", "list"],
    ]
    printed = capsys.readouterr()
    assert private_peer not in printed.out + printed.err


def test_e2e_chat_variable_cleanup_detects_racy_recreation(
    monkeypatch, capsys,
):
    """A GitHub CLI success is insufficient if the variable still exists."""
    from subprocess import CompletedProcess
    from scripts import setup_telegram_e2e as setup

    private_peer = "-100" + "888777666555"
    commands = []

    def variable_reappears(cmd, **kwargs):
        assert kwargs["capture_output"] is True
        commands.append(cmd[:3])
        if cmd[:3] == ["gh", "variable", "list"]:
            return CompletedProcess(
                cmd, 0,
                stdout=b'[{"name":"TELEDRIVE_E2E_BOT_CHAT_ID"}]',
                stderr=private_peer.encode(),
            )
        assert cmd[:3] == ["gh", "variable", "delete"]
        return CompletedProcess(cmd, 0, stdout=b"", stderr=private_peer.encode())

    monkeypatch.setattr(setup.subprocess, "run", variable_reappears)
    with pytest.raises(RuntimeError, match="still present"):
        setup.remove_public_bot_chat_variable("some-owner/some-repo")
    assert commands == [
        ["gh", "variable", "list"], ["gh", "variable", "delete"],
        ["gh", "variable", "list"],
    ]
    printed = capsys.readouterr()
    assert private_peer not in printed.out + printed.err


def test_secure_bot_id_cli_help_requires_no_telethon_install():
    """The GitHub-only secret migration must be usable without Telegram SDK."""
    import subprocess
    import sys

    command = [
        sys.executable, "-S",
        str(ROOT / "scripts" / "setup_telegram_e2e.py"),
        "--help",
    ]
    finished = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=15,
    )
    assert finished.returncode == 0, finished.stderr
    assert "--secure-bot-chat-id" in finished.stdout
    assert "ModuleNotFoundError" not in finished.stderr


def test_bot_variable_discovery_parses_full_name_listing(monkeypatch):
    """The names-only JSON parser must not truncate a long variable list."""
    from subprocess import CompletedProcess
    import json
    from scripts import setup_telegram_e2e as setup

    legacy_name = "TELEDRIVE_E2E_BOT_CHAT_ID"
    entries = [{"name": f"OTHER_{n}"} for n in range(40)]
    entries.append({"name": legacy_name})
    calls = []

    def all_variable_names(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs["capture_output"] is True
        assert cmd[-2:] == ["--json", "name"]
        return CompletedProcess(cmd, 0, stdout=json.dumps(entries).encode())

    monkeypatch.setattr(setup.subprocess, "run", all_variable_names)
    names = setup.legacy_bot_chat_variable_names("some-owner/some-repo")
    assert legacy_name in names
    assert len(names) == 41
    assert len(calls) == 1


def test_live_bot_workers_are_serialized_without_cancelling_user_e2e():
    """GitHub permits only one pending job per concurrency group."""
    bot_group = "teledrive-live-bot-${{ github.repository }}"
    user_group = "teledrive-live-user-${{ github.repository }}"
    bot = workflow("telegram-bot-e2e.yml")
    user = workflow("telegram-storage-e2e.yml")
    workers = workflow("telegram-multiworker-e2e.yml")
    assert bot["concurrency"]["group"] == bot_group
    assert user["concurrency"]["group"] == user_group
    assert workers["jobs"]["three-process-telegram"]["concurrency"]["group"] == bot_group
    assert user_group != bot_group  # both required jobs must finish on same SHA
    for value in (
        bot["concurrency"], user["concurrency"],
        workers["jobs"]["three-process-telegram"]["concurrency"],
    ):
        assert value["cancel-in-progress"] == "false"


def test_every_test_file_is_referenced_by_a_real_ci_workflow():
    """New regression modules cannot silently sit outside GitHub Actions."""
    workflow_sources = "\n".join(
        (WORKFLOWS / name).read_text(encoding="utf-8")
        for name in EXPECTED
    )
    missing = [
        path.name
        for path in sorted((ROOT / "tests").glob("test_*.py"))
        if f"tests/{path.name}" not in workflow_sources
    ]
    assert not missing, f"Unscheduled regression test modules: {missing}"


def test_critical_ci_is_unfiltered_at_every_commit_sha():
    for name in ("database-ci.yml", "desktop-ci.yml", "storage-api-ci.yml"):
        events = workflow(name)["on"]
        for trigger in ("push", "pull_request"):
            assert "paths" not in events[trigger]
            assert "paths-ignore" not in events[trigger]
    assert "paths" not in workflow("release.yml")["on"]["push"]


@pytest.mark.parametrize("name", EXPECTED)
def test_mainline_only_push_triggers_after_p1_merge(name):
    """Keep retired development branches out of release-gating workflows."""
    config = workflow(name)
    assert "push" in config["on"]
    branches = config["on"]["push"].get("branches", [])
    assert "main" in branches, name
    assert "feature/docker-webui-multiaccount" not in branches, name


def test_p1_multiworker_marker_runs_the_three_process_job_without_regular_bot_collision():
    """A mainline one-shot multiworker acceptance does not queue Bot twice."""
    bot = workflow("telegram-bot-e2e.yml")
    worker = workflow("telegram-multiworker-e2e.yml")
    regular = bot["jobs"]["telegram-bot-roundtrip"]
    triple = worker["jobs"]["three-process-telegram"]
    assert "[p1-live-multiworker]" in regular["if"]
    assert "github.event_name != 'push'" in regular["if"]
    assert "!contains" in regular["if"]
    assert "[p1-live-multiworker]" in triple["if"]
    assert "github.event_name == 'workflow_dispatch'" in triple["if"]
    assert triple["concurrency"]["group"] == bot["concurrency"]["group"]
    assert bot["concurrency"]["cancel-in-progress"] == "false"
    assert triple["concurrency"]["cancel-in-progress"] == "false"


def test_reusable_docker_fault_injection_workflow_contract():
    """Independent Docker fault injection persists across roadmap phases."""
    assert not (WORKFLOWS / "p1-dev-acceptance.yml").exists()
    doc = workflow("docker-fault-injection-ci.yml")
    assert doc["name"] == "TeleDrive Docker Fault Injection CI"
    assert "workflow_dispatch" in doc["on"]
    assert "push" in doc["on"] and "pull_request" in doc["on"]
    assert "paths" not in doc["on"]["push"]
    assert doc["permissions"]["contents"] == "read"
    commands = "\n".join(s.get("run", "") for s in
                         doc["jobs"]["disposable-docker-fault-injection"]["steps"])
    assert "scripts/run_docker_fault_injection.sh" in commands
    assert (ROOT / "docker-compose.fault-ci.yml").is_file()
    assert (ROOT / "Dockerfile.fault-ci").is_file()
    assert (ROOT / "scripts/docker_fault_scenarios.py").is_file()
    assert (ROOT / "docs/DOCKER_FAULT_INJECTION.md").is_file()


def test_docker_fault_config_has_every_required_runtime_upload_field():
    """A running HTTP healthcheck does not prove the WebUI config can load.

    Catches the actual Docker DEV regression from 1f32159, where the
    generated config omitted sleep_min_seconds and storage health returned
    HTTP 500 (while both /api/health probes remained green).
    """
    source = (ROOT / "teledrive" / "uploader.py").read_text(encoding="utf-8")
    harness = (ROOT / "scripts" / "run_docker_fault_injection.sh").read_text(
        encoding="utf-8",
    )
    required = set(re.findall(r'up\["([^"]+)"\]', source))
    assert required
    missing = sorted(field for field in required if field + ":" not in harness)
    assert not missing, f"Docker fault config missing required upload fields: {missing}"
    # Verify the ACTUAL production config parser in each clean Web container,
    # not only the permissive Uvicorn /api/health startup probe.
    assert "for node in web-a web-b" in harness
    assert 'load_config("/config/config.yaml")' in harness
    assert 'exec -T "$node"' in harness


def test_disposable_docker_dev_acceptance_has_real_restart_and_restore_gates():
    """Verify the acceptance runner does not regress into metadata-only mocks."""
    runner = (ROOT / "scripts" / "run_docker_fault_injection.sh").read_text(
        encoding="utf-8",
    )
    cases = (ROOT / "scripts" / "docker_fault_scenarios.py").read_text(
        encoding="utf-8",
    )
    assert 'kill -s SIGKILL web-a' in runner
    assert '"${COMPOSE[@]}" restart postgres' in runner
    assert '"${COMPOSE[@]}" run -T --rm scenarios restarted' in runner
    assert '"${COMPOSE[@]}" run -T --rm scenarios restored' in runner
    assert '"${COMPOSE[@]}" run -T --rm scenarios acceptance' in runner
    assert '{"seed", "restarted", "restored", "acceptance"}' in runner
    assert 'docker_removed' in runner and 'and docker_removed' in runner
    assert 'checks_count") == 13' in runner
    assert '"DEV-13"' in cases and '"DEV-11"' in cases
    assert 'InterruptedDownload' in cases
    assert 'raise TimeoutError("injected transport disconnect")' in cases
    assert 'missing.replace(media)' in cases
    assert 'tempfile' not in runner or 'docker compose' in runner


def test_disposable_dev_acceptance_does_not_require_live_telegram_credentials():
    """No production secret may be mounted inside disposable Compose."""
    compose = (ROOT / "docker-compose.fault-ci.yml").read_text(
        encoding="utf-8",
    )
    runner = (ROOT / "scripts" / "run_docker_fault_injection.sh").read_text(
        encoding="utf-8",
    )
    for private in ("TELEDRIVE_E2E_SESSION", "TELEDRIVE_BOT_TOKEN",
                    "TELEDRIVE_STORAGE_BOT_CHAT_ID"):
        assert private not in compose
    assert 'if [[ -n "${TELEDRIVE_BOT_TOKEN:-}"' in runner
    assert 'down -v --remove-orphans' in runner
    assert 'production_acceptance_signed_off": False' in runner

