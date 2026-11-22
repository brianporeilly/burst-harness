"""A review of a task's open PR, asked for from the orchestrator (TaskRunner.review, `harness review`): Claude Code
with a model, or a profile, with a fresh context; the task as it was afterwards."""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from orchestrator.app import create_app
from orchestrator.tasks import TasksError

from .test_orchestrator import CHANGE, FakeBackend
from .test_tasks import _runner, _settled, harness  # noqa: F401 (harness: a fixture)

PR = "https://github.com/me/proj/pull/7"


def _with_pr(tmp_path, harness, **options):  # noqa: F811
    runner = _runner(tmp_path, **options)
    runner.create({"repo": "me/proj", "prompt": "Add f", "slug": "t", "engine": "strands", "provider": "llamacpp",
                   "base_url": "http://llm.lan:8080"})
    harness.started[0][1].finish(0, state="pr", pr=PR, said={"summary": "added f"})
    _settled(runner, "t", "pr")
    return runner


def test_a_review_runs_claude_code_on_the_prs_branch_and_leaves_the_task_as_it_was(tmp_path, harness):  # noqa: F811
    runner = _with_pr(tmp_path, harness)
    runner.review("t", {"model": "claude-sonnet-5-5"}, user="brian")
    argv, process = harness.started[1]
    assert argv[argv.index("--engine") + 1] == "claude-code" and "bypassPermissions" in argv
    at = argv.index("review")
    assert argv[at - 2:at] == ["--model", "claude-sonnet-5-5"]
    assert argv[at:] == ["review", "t", "--pr", PR, "--branch", "harness/t"]
    assert "HARNESS_PROVIDER" not in process.env and "HARNESS_BASE_URL" not in process.env  # not the task's model
    assert runner.task("t")["state"] == "running"

    process.finish(0, state="reviewed", said={"review": "f.py:2 returns 1.", "by": "Claude Code (claude-sonnet-5-5)",
                                              "comment": PR + "#issuecomment-1"})
    done = _settled(runner, "t", "pr")
    burst = done["bursts"][-1]
    assert burst["state"] == "reviewed" and burst["review"] == {"model": "claude-sonnet-5-5", "by": "brian"}
    assert burst["said"]["comment"] == PR + "#issuecomment-1"
    assert done["said"] == {"summary": "added f"} and done["pr"] == PR  # the task's own, as they were
    assert "review" not in done and "after" not in done
    assert "=== review 2," in runner.task("t")["output"]

    runner.resume("t", "address the review")  # continuing is a burst of the task again, on its own model
    argv = harness.argv()
    assert argv[argv.index("--engine") + 1] == "strands" and "run" in argv and "review" not in argv


def test_a_profile_reviews_and_a_failed_review_still_leaves_the_task_as_it_was(tmp_path, harness, monkeypatch):  # noqa: F811
    runner = _with_pr(tmp_path, harness)
    with pytest.raises(TasksError, match="No profile named nobody"):
        runner.review("t", {"profile": "nobody"})
    runner.base = runner.base.__class__(**{**runner.base.__dict__, "agents": {"critic": {"provider": "anthropic"}}})
    runner.review("t", {"profile": "critic"})
    argv, process = harness.started[1]
    assert argv[argv.index("--engine") + 1] == "strands" and argv[-2:] == ["--by", "critic"]
    process.finish(1)
    done = _settled(runner, "t", "pr")
    assert done["bursts"][-1]["state"] == "failed" and done["state"] == "pr"


def test_only_an_open_pr_of_an_idle_task_is_reviewed(tmp_path, harness):  # noqa: F811
    runner = _runner(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "Add f", "slug": "t"})
    with pytest.raises(TasksError, match="has no PR to review"):
        runner.review("t", {})
    harness.started[0][1].finish(0, state="pr", pr=PR)
    _settled(runner, "t", "pr")
    with pytest.raises(TasksError, match="a profile or a model, not both"):
        runner.review("t", {"profile": "p", "model": "m"})
    with pytest.raises(TasksError, match="a model's id"):
        runner.review("t", {"model": "two words"})
    runner.review("t", {})
    with pytest.raises(TasksError, match="the task is running"):
        runner.review("t", {})
    harness.started[1][1].finish(0, state="reviewed", said={"review": "NO FINDINGS", "by": "Claude Code"})
    _settled(runner, "t", "pr")
    harness.prs[PR] = "merged"
    with pytest.raises(TasksError, match="not open to review"):
        runner.review("t", {})


def test_a_queued_review_cancelled_leaves_the_task_as_it_was(tmp_path, harness):  # noqa: F811
    runner = _with_pr(tmp_path, harness, limit=1)
    runner.create({"repo": "me/other", "prompt": "Busy", "slug": "busy"})  # takes the one slot
    runner.review("t", {"model": "claude-opus-5-5"})
    assert runner.task("t")["state"] == "queued"
    runner.cancel("t")
    assert runner.task("t")["state"] == "pr" and "review" not in runner.task("t")


def test_the_review_api(tmp_path, harness):  # noqa: F811
    runner = _with_pr(tmp_path, harness)
    client = TestClient(create_app(FakeBackend(), tasks=runner, hosts=("orchestrator.lan",), user_header="Remote-User"),
                        base_url="http://orchestrator.lan")
    signed_in = {**CHANGE, "Remote-User": "brian"}
    assert client.post("/api/tasks/t/review", json={}, headers={"Remote-User": "b"}).status_code == 403
    asked = client.post("/api/tasks/t/review", json={"model": "claude-opus-5-5"}, headers=signed_in)
    assert asked.status_code == 202 and asked.json()["state"] in ("queued", "running")
    assert client.post("/api/tasks/t/review", json={}, headers=signed_in).status_code == 400  # one at a time


def test_slack_says_a_review_started_and_where_its_comment_is():
    from orchestrator.slack import SlackBot

    about = SlackBot._about
    record = {"repo": "me/proj", "review": {"model": "claude-sonnet-5-5"}, "bursts": [{}]}
    assert about(None, "started", record) == ":mag: Reviewing the PR with Claude Code (claude-sonnet-5-5)."
    done = {"repo": "me/proj", "bursts": [{"state": "reviewed", "review": {"profile": "critic"},
                                           "said": {"by": "critic", "comment": PR + "#c1"}}]}
    assert about(None, "reviewed", done) == f":mag: critic reviewed the PR: {PR}#c1."
    failed = {"repo": "me/proj", "bursts": [{"state": "failed", "review": {"profile": "critic"}}]}
    assert about(None, "reviewed", failed) == ":x: The review by critic failed."
