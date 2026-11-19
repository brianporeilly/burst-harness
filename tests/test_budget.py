"""Limits on a burst: one reached pauses it, with the limit as its next step, rather than running on."""

import json
import time
from dataclasses import replace
from types import SimpleNamespace

from harness import budget, cli
from harness.claude_code import ClaudeCodeEngine, _budget_stop
from harness.config import HarnessConfig
from harness.context import HarnessContext
from harness.exec_target import LocalTarget

from .conftest import git
from .test_agents import llama  # noqa: F401 -- the fixture

STEP = {"tool": "shell", "args": {"command": "true"}}


def _result(out):
    return json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])


def _work(tmp_path, origin):
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    return work


def test_a_burst_at_its_model_calls_pauses_with_the_limit_as_its_next_step(tmp_path, origin, llama, capsys,  # noqa: F811
                                                                          monkeypatch):
    work = _work(tmp_path, origin)
    url, requests = llama([STEP] * 20 + [{"text": "done"}])
    monkeypatch.setenv("HARNESS_MAX_MODEL_CALLS", "3")
    code = cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "start", "t1", "--task", "Go."])
    result = _result(capsys.readouterr().out)
    assert code == cli.EXIT_DONE and result["state"] == "paused"
    assert result["said"]["budget"] == "max_model_calls: 3 model calls, the limit is 3"
    assert len(requests) == 3 and result["metrics"]["model_calls"] == 3
    doc = (work / "docs" / "tasks" / "t1.md").read_text()
    assert "Stopped at the burst's budget (max_model_calls" in doc and "Continue to carry on" in doc
    assert git(work, "status", "--porcelain") == ""  # everything committed, nothing stranded


def test_a_delegate_is_stopped_too_and_its_lead_after_it(tmp_path, origin, llama, capsys, monkeypatch):  # noqa: F811
    work = _work(tmp_path, origin)
    helper, helper_requests = llama([STEP] * 20 + [{"text": "helped"}])
    lead, lead_requests = llama([{"tool": "delegate", "args": {"to": "helper", "task": "Loop."}}, STEP,
                                 {"text": "done"}])
    scenario = tmp_path / "scenario.toml"
    scenario.write_text(f'provider = "llamacpp"\nbase_url = "{lead}"\ndelegate_to = ["helper"]\nmax_model_calls = 4\n'
                        f'[agents.helper]\nprovider = "llamacpp"\nbase_url = "{helper}"\nmodel_id = "helper"\n'
                        'use_for = "helping"\n')
    cli.main(["--config", str(scenario), "--repo", str(work), "start", "t1", "--task", "Go."])
    result = _result(capsys.readouterr().out)
    # The lead's one call and the helper's three make four: the helper stops, and the lead with it.
    assert (len(lead_requests), len(helper_requests)) == (1, 3)
    assert result["state"] == "paused" and result["said"]["budget"].startswith("max_model_calls: 4")


def test_minutes_and_dollars():
    cfg = replace(HarnessConfig(), max_burst_minutes=1)
    ctx = SimpleNamespace(cfg=cfg, started=time.time() - 90, delegated={}, budget=None)
    assert budget.of(ctx).check() == "max_burst_minutes: 2 minutes, the limit is 1"

    cfg = replace(HarnessConfig(), max_cost_usd=0.5)
    ctx = SimpleNamespace(cfg=cfg, started=time.time(), budget=None,
                          delegated={"helper": {"metrics": {"model_calls": 2, "cost_usd": 0.3}}})
    assert budget.of(ctx).check() == ""
    ctx.delegated["other"] = {"metrics": {"model_calls": 1, "cost_usd": 0.25}}
    assert budget.of(ctx).check() == "max_cost_usd: about $0.55, the limit is $0.50"

    unlimited = SimpleNamespace(cfg=HarnessConfig(), started=0, delegated={}, budget=None)
    assert not budget.of(unlimited).limited and budget.of(unlimited).check() == ""


def test_claude_code_is_given_whats_left_and_pauses_at_it(tmp_path):
    cfg = replace(HarnessConfig(), max_model_calls=30, max_cost_usd=2.0)
    ctx = HarnessContext(target=LocalTarget(repo=str(tmp_path)), cfg=cfg, slug="t", branch="b", state_key="k")
    assert budget.of(ctx).claude_args(10, 0.5) == ["--max-turns", "20", "--max-budget-usd", "1.50"]
    assert budget.of(ctx).claude_args(40, 3.0) == ["--max-turns", "1", "--max-budget-usd", "0.01"]
    assert _budget_stop("error_max_turns", HarnessConfig(), 5, 0) == ""  # its own --max-turns, not the budget's

    engine = ClaudeCodeEngine(ctx)
    engine._on_line(json.dumps({"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
                                "num_turns": 7, "total_cost_usd": 2.01, "usage": {}}))
    assert ctx.budget.reason == "max_cost_usd: $2.01 as Claude Code reports it, the limit is $2.00"


def test_a_sandboxs_deadline_stops_the_burst_before_it():
    """A burst's Job had 8 hours, its sandbox 4: the sandbox was deleted mid-burst, the work in it lost."""
    now = time.time()
    ctx = SimpleNamespace(cfg=HarnessConfig(), started=now, delegated={}, budget=None,
                          target=SimpleNamespace(deadline=now + 20 * 60))
    limits = budget.of(ctx)
    assert limits.limited and abs(limits.stop_at() - (now + 5 * 60)) < 1  # 15 minutes before it
    assert limits.check() == ""
    ctx.target.deadline = now + 10 * 60
    assert limits.check().startswith("the sandbox's deadline: it is deleted at ")
    assert limits.check().endswith("and the burst stops 15 minutes before")

    both = SimpleNamespace(cfg=replace(HarnessConfig(), max_burst_minutes=2), started=now, delegated={},
                           budget=None, target=SimpleNamespace(deadline=now + 60 * 60))
    assert abs(budget.of(both).stop_at() - (now + 120)) < 1  # the sooner of the two
