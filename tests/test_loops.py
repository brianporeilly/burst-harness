"""Noticing a loop: the same call failing the same way is told at loop_warn, and stopped at loop_stop."""

import json

from harness import cli, loops
from harness.config import HarnessConfig

from . import fake_llama
from .conftest import git
from .test_agents import llama  # noqa: F401 -- the fixture

FAIL = {"tool": "shell", "args": {"command": "echo 'test_x failed in 0.52s' >&2; false"}}


def _result(out):
    return json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])


def _said(request):
    return json.dumps(request["messages"])


def test_a_lead_failing_the_same_way_is_warned_then_paused(tmp_path, origin, llama, capsys):  # noqa: F811
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    url, requests = llama([FAIL] * 10 + [{"text": "done"}])
    code = cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "start", "t1", "--task", "Go."])
    result = _result(capsys.readouterr().out)
    assert code == cli.EXIT_DONE and result["state"] == "paused" and len(requests) == 5
    assert result["said"]["loop"].startswith("the same shell call failed the same way 5 times")
    assert "failed the same way 3 times" not in _said(requests[2]) and "failed the same way 3 times" in _said(requests[3])
    assert "Stopped in a loop" in (work / "docs" / "tasks" / "t1.md").read_text()


def test_a_delegate_in_a_loop_is_stopped_and_its_lead_goes_on(tmp_path, origin, llama, capsys):  # noqa: F811
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    helper, helper_requests = llama([FAIL] * 10 + [{"text": "helped"}])
    lead, lead_requests = llama([{"tool": "delegate", "args": {"to": "helper", "task": "Fix it."}},
                                 {"tool": "pause_for_review", "args": {"summary": "s", "next_step": "n"}},
                                 {"text": "Paused."}])
    scenario = tmp_path / "scenario.toml"
    scenario.write_text(f'provider = "llamacpp"\nbase_url = "{lead}"\ndelegate_to = ["helper"]\n'
                        f'[agents.helper]\nprovider = "llamacpp"\nbase_url = "{helper}"\nmodel_id = "helper"\n'
                        'use_for = "helping"\n')
    cli.main(["--config", str(scenario), "--repo", str(work), "start", "t1", "--task", "Go."])
    result = _result(capsys.readouterr().out)
    assert len(helper_requests) == 5 and "helper was stopped in a loop" in fake_llama._text(
        lead_requests[1]["messages"][-1]["content"])
    assert result["state"] == "paused" and "loop" not in result["said"]  # the lead paused of its own accord


def test_what_counts_as_the_same_failure():
    watch = loops.Loops(HarnessConfig())

    def shell(code, text):
        return {"status": "success", "content": [{"json": {"output": "", "error": text, "exit_code": code}}]}

    assert watch.note("shell", {"command": "pytest"}, shell(1, "1 failed in 0.52s")) == 1
    assert watch.note("shell", {"command": "pytest"}, shell(1, "1 failed in 0.61s")) == 2  # durations aside
    assert watch.note("shell", {"command": "pytest"}, shell(1, "2 failed in 0.61s")) == 1  # a different failure
    assert watch.note("shell", {"command": "pytest -x"}, shell(1, "1 failed in 0.61s")) == 1  # a different call
    assert watch.note("shell", {"command": "pytest"}, shell(0, "1 passed")) == 0  # success is never counted
    assert watch.note("read", {"path": "x"}, {"status": "error", "content": [{"text": "no such file"}]}) == 1
    assert watch.note("edit", {"path": "x"}, None, ValueError("old_str not found")) == 1
    assert watch.reason == ""

    off = loops.Loops(HarnessConfig(loop_warn=0, loop_stop=0))
    for _ in range(9):
        off.note("shell", {"command": "false"}, shell(1, ""))
    assert off.reason == ""
