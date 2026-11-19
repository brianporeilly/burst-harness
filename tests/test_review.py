"""review_with: a profile reviews the change, read-only, before a PR opens. Its findings go back to the lead
once, then into the PR's body."""

import json
from types import SimpleNamespace

import pytest

from harness import cli, review
from harness.agents import AgentsError
from harness.config import HarnessConfig

from . import fake_llama
from .conftest import git
from .test_agents import llama  # noqa: F401 -- the fixture

FINISH = {"tool": "finish_with_pr", "args": {"title": "Fix it", "body": "Fixed."}}
EDIT = {"tool": "shell", "args": {"command": "echo 'def f(): return 1' > f.py"}}


def _result(out):
    return json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])


def _run(tmp_path, origin, lead, reviewer):
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    scenario = tmp_path / "scenario.toml"
    scenario.write_text(f'provider = "llamacpp"\nbase_url = "{lead}"\nreview_with = "reviewer"\n'
                        f'[agents.reviewer]\nprovider = "llamacpp"\nbase_url = "{reviewer}"\nmodel_id = "reviewer"\n'
                        'use_for = "reviewing"\n')
    cli.main(["--config", str(scenario), "--repo", str(work), "start", "t1", "--task", "Add f."])
    return work


def test_findings_go_back_to_the_lead_once_then_the_pr_opens(tmp_path, origin, llama, capsys):  # noqa: F811
    reviewer, reviewer_requests = llama([{"text": "f.py:1 returns 1, the task document asks for 2."}])
    lead, lead_requests = llama([EDIT, FINISH, FINISH, {"text": "done"}])
    _run(tmp_path, origin, lead, reviewer)
    result = _result(capsys.readouterr().out)

    assert len(reviewer_requests) == 1  # once a burst: the second finish is not reviewed again
    brief = json.dumps(reviewer_requests[0]["messages"])
    assert "Review this change" in brief and "def f(): return 1" in brief and "Add f." in brief
    assert "f.py" in brief and "NO FINDINGS" in brief
    refused = fake_llama._text(lead_requests[2]["messages"][-1]["content"])
    assert "reviewer reviewed the change first" in refused and "asks for 2" in refused
    assert "Your work so far is committed to the branch" in refused
    assert "wip(t1): before the check and review" in git(origin, "log", "--format=%s", "harness/t1")
    assert result["state"] == "pushed_no_pr" and len(lead_requests) == 3  # the second finish went through
    assert result["metrics"]["review"] == {"by": "reviewer", "outcome": "findings"}
    assert result["delegated"]["reviewer"]["calls"] == 1  # its usage counted with the burst's


def test_no_findings_lets_the_first_finish_through(tmp_path, origin, llama, capsys):  # noqa: F811
    reviewer, _ = llama([{"text": "NO FINDINGS"}])
    lead, lead_requests = llama([EDIT, FINISH, {"text": "done"}])
    _run(tmp_path, origin, lead, reviewer)
    result = _result(capsys.readouterr().out)
    assert result["state"] == "pushed_no_pr" and len(lead_requests) == 2
    assert result["metrics"]["review"] == {"by": "reviewer", "outcome": "no findings"}


def test_the_pr_body_carries_the_review_and_a_failed_reviewer_holds_nothing_up(monkeypatch):
    ctx = SimpleNamespace(cfg=HarnessConfig(review_with="reviewer"), review=None)
    monkeypatch.setattr(review, "run", lambda ctx, name: "reviewer failed: ConnectError: refused")
    body = review.before_pr(ctx, "Fixed.")
    assert body.startswith("Fixed.") and "Reviewed by reviewer" in body and "ConnectError" in body
    assert ctx.review["outcome"] == "failed"

    ctx = SimpleNamespace(cfg=HarnessConfig(review_with="reviewer"), review=None)
    monkeypatch.setattr(review, "run", lambda ctx, name: "a.py:3 is wrong")
    with pytest.raises(review.ReviewGiven, match="a.py:3 is wrong"):
        review.before_pr(ctx, "Fixed.")
    body = review.before_pr(ctx, "Fixed, and a.py:3 too.")
    assert "Fixed, and a.py:3 too." in body and "a.py:3 is wrong" in body

    assert review.before_pr(SimpleNamespace(cfg=HarnessConfig(), review=None), "Fixed.") == "Fixed."


def test_review_with_must_name_a_profile_and_isnt_a_repos_to_set(tmp_path):
    from harness.config import REPO_SETTABLE

    with pytest.raises(AgentsError, match="review_with names no profile: nobody"):
        HarnessConfig.load(overrides={"review_with": "nobody"}, user_config=None)
    assert "review_with" not in REPO_SETTABLE


def test_a_review_is_held_to_its_limits_told_first_and_holds_nothing_up(tmp_path, origin, llama, capsys,  # noqa: F811
                                                                        monkeypatch):
    """One review ran two hours, rebuilding the project and writing simulations, till the sandbox was deleted."""
    reviewer, reviewer_requests = llama([{"tool": "shell", "args": {"command": "echo looking"}}] * 50)
    lead, lead_requests = llama([EDIT, FINISH, {"text": "done"}])
    monkeypatch.setenv("HARNESS_REVIEW_MAX_CALLS", "5")
    monkeypatch.setenv("HARNESS_CHECK_COMMAND", "true")
    _run(tmp_path, origin, lead, reviewer)
    result = _result(capsys.readouterr().out)

    assert len(reviewer_requests) == 5  # stopped at its limit
    brief = json.dumps(reviewer_requests[0]["messages"])
    assert "(`true`) has already passed" in brief and "Don't build the project" in brief and "5 model calls" in brief
    told = [i for i, request in enumerate(reviewer_requests) if "nearly out of time" in json.dumps(request["messages"])]
    assert told and told[0] == 2  # after its second call's tools: 5 - 3
    # It gave no findings before it was stopped: nothing held up, the PR went ahead on the first finish.
    assert result["metrics"]["review"] == {"by": "reviewer", "outcome": "failed"}
    assert result["state"] == "pushed_no_pr" and len(lead_requests) == 2


def test_a_review_cut_off_with_findings_still_gives_them(monkeypatch):
    ctx = SimpleNamespace(cfg=HarnessConfig(review_with="reviewer"), review=None)
    monkeypatch.setattr(review, "run", lambda ctx, name: "a.py:3 races with b.py:9\n[harness] reviewer was stopped "
                                                        "at the burst's budget (max_model_calls: 30 model calls)")
    with pytest.raises(review.ReviewGiven, match="cut off at its limit") as given:
        review.before_pr(ctx, "Fixed.")
    assert "a.py:3 races with b.py:9" in str(given.value) and "[harness]" not in str(given.value)
