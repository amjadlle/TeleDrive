from scripts.check_release_matrix import REQUIRED, check

def sample(status="completed", conclusion="success", sha="abc"):
    return {"workflow_runs": [
        {"name": name, "head_sha": sha, "id": i, "created_at": "2026-10-14T00:00:00Z",
         "status": status, "conclusion": conclusion}
        for i, name in enumerate(sorted(REQUIRED))
    ]}

def test_all_completed_success():
    assert check(sample(), "abc") == (0, [])

def test_skipped_failed_cancelled_are_blocked():
    for outcome in ("skipped", "failure", "cancelled"):
        assert check(sample(conclusion=outcome), "abc")[0] == 1

def test_wrong_sha_and_unfinished_cannot_release():
    assert check(sample(sha="wrong"), "abc")[0] == 2
    assert check(sample(status="in_progress", conclusion=None), "abc")[0] == 2

def test_newest_same_sha_run_wins():
    obj = sample()
    obj["workflow_runs"].append({"name": sorted(REQUIRED)[0], "head_sha": "abc",
      "id": 999, "created_at": "2026-10-14T01:00:00Z",
      "status": "completed", "conclusion": "failure"})
    assert check(obj, "abc")[0] == 1
