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


def test_retrying_between_new_work_is_not_a_loop_but_padding_it_is():
    """local-llm-benchmark's false stops: a service down on purpose, retried now and then between other work."""
    def ok(text="ok"):
        return {"status": "success", "content": [{"text": text}]}

    down = {"status": "error", "content": [{"text": "invite service unavailable: SMTP relay refused"}]}

    watch = loops.Loops(HarnessConfig())
    for n in range(9):  # 9 failures, each after a call that hadn't worked before
        watch.note("shell", {"command": f"step {n}"}, ok())
        assert watch.note("send_invite", {"to": "a@example.com"}, down) == n + 1  # the warning still counts all
    assert watch.reason == ""

    padded = loops.Loops(HarnessConfig())  # the same successful call between: nothing new worked
    for _ in range(5):
        padded.note("shell", {"command": "ls"}, ok())
        padded.note("send_invite", {"to": "a@example.com"}, down)
    assert padded.reason.startswith("the same send_invite call failed the same way 5 times with nothing new")

    other = loops.Loops(HarnessConfig())  # another failure between doesn't reset it either
    for n in range(5):
        other.note("read", {"path": f"missing{n}"}, {"status": "error", "content": [{"text": "no such file"}]})
        other.note("send_invite", {"to": "a@example.com"}, down)
    assert other.reason


RECAPS = [
    "The picture is now clear. The roadmap review and agent-workflow scaffolding are already merged in PR #1, so "
    "this session's job is Phase 0 tooling: the cargo workspace, the toolchain pin and CI. The build cache is empty.",
    "Now I have the full picture. Roadmap review plus agent-workflow scaffolding (AGENTS.md, docs/agents) were "
    "merged in PR #1; this session's job is the cargo workspace, toolchain pin and CI for Phase 0. Build cache empty.",
    "I now have full clarity. Summary: the roadmap review and the agent-workflow scaffolding are merged (PR #1). "
    "This session's job: Phase 0, the cargo workspace, toolchain pin, CI. The build cache is empty, so builds are slow.",
    "This is very clear now. The roadmap review and agent-workflow scaffolding already merged in PR #1. The job of "
    "this session is Phase 0: cargo workspace, toolchain pin, CI. The build cache is empty.",
]
WORK = [
    "Fixing the borrow in deal_damage: take the attack by value so callers can hold the unit mutably meanwhile.",
    "The A* search hangs on a closed grid: the open set never empties because visited nodes are pushed again.",
    "Adding a test for line of sight across diagonal walls, then running cargo test for rift_core alone.",
    "The CI workflow needs the pinned toolchain: reading rust-toolchain.toml and matching it in the action.",
]


def test_replies_restating_the_same_summary_are_a_loop():
    from harness.loops import Repeats

    repeats = Repeats()
    told = [repeats.note(text) for text in [*WORK, *RECAPS]]
    # The first recaps only set the scene; the third repeats two before it, the fourth is a loop, told.
    assert told == [False] * 4 + [False, False, False, True]
    assert repeats.note("") is False and repeats.note("Done.") is False  # nothing, or too little, to compare

    working = Repeats()
    assert not any(working.note(text) for text in WORK * 2)  # one piece of work after another: each different


def test_the_model_is_told_in_its_next_tool_result(tmp_path, origin, monkeypatch, capsys):
    script = [{"text": text, "tool": "shell", "args": {"command": "echo ok"}} for text in RECAPS]
    script += [{"tool": "pause_for_review", "args": {"summary": "s", "next_step": "n"}}, {"text": "Paused."}]

    def run(name):
        server, requests = fake_llama.serve(script)
        try:
            work = tmp_path / name
            git(tmp_path, "clone", "-q", str(origin), str(work))
            cli.main(["--provider", "llamacpp", "--base-url", f"http://127.0.0.1:{server.server_port}",
                      "--repo", str(work), "start", name, "--task", "Build it."])
            out = capsys.readouterr().out
            return requests, json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])
        finally:
            server.shutdown()

    requests, result = run("t1")
    told = [i for i, request in enumerate(requests) if "keep restating the same summary" in json.dumps(request)]
    assert told and told[0] == 4  # after the fourth recap's call: the request that follows it
    assert result["metrics"]["repeats"] == 1

    monkeypatch.setenv("HARNESS_REPEAT_NOTICE", "false")
    requests, result = run("t2")
    assert "keep restating" not in json.dumps(requests) and result["metrics"]["repeats"] == 0
