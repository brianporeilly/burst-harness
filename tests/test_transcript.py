"""A burst's transcript: every message of the lead and its delegates, tool and model calls timed, the metrics;
one JSON per burst in the state directory, named in HARNESS_RESULT."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from harness import cli, transcript
from harness.claude_code import ClaudeCodeEngine
from harness.config import HarnessConfig
from harness.context import HarnessContext
from harness.exec_target import LocalTarget

from .conftest import git
from .test_agents import llama  # noqa: F401 -- the fixture

STEP = {"tool": "shell", "args": {"command": "echo step"}}


def _result(out):
    return json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])


def test_a_bursts_lead_and_delegate_are_recorded(tmp_path, origin, llama, capsys):  # noqa: F811
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    helper, helper_requests = llama([STEP, {"text": "helped"}])
    lead, lead_requests = llama([{"tool": "delegate", "args": {"to": "helper", "task": "Help out."}}, STEP,
                                 {"tool": "pause_for_review", "args": {"summary": "s", "next_step": "n"}},
                                 {"text": "Paused."}])
    scenario = tmp_path / "scenario.toml"
    scenario.write_text(f'provider = "llamacpp"\nbase_url = "{lead}"\ndelegate_to = ["helper"]\n'
                        f'[agents.helper]\nprovider = "llamacpp"\nbase_url = "{helper}"\nmodel_id = "helper"\n'
                        'use_for = "helping"\n')
    cli.main(["--config", str(scenario), "--repo", str(work), "start", "t1", "--task", "Go."])
    result = _result(capsys.readouterr().out)

    path = Path(result["transcript"])
    assert path.parent.name == "transcripts" and path.name.endswith("-t1.json")
    data = json.loads(path.read_text())
    assert (data["schema"], data["slug"], data["engine"], data["state"]) == (1, "t1", "strands", "paused")
    assert data["metrics"] == result["metrics"] and data["config"] == result["config"]

    lead_entry, helper_entry = data["agents"]
    assert (lead_entry["name"], lead_entry["format"]) == ("lead", "strands") and "system" in lead_entry
    assert lead_entry["messages"][0]["role"] == "user" and "Go." in json.dumps(lead_entry["messages"][0])
    assert all(isinstance(m["at"], float | int) for m in lead_entry["messages"])
    assert "toolResult" in json.dumps(lead_entry["messages"])
    assert (helper_entry["name"], helper_entry["task"]) == ("helper", "Help out.")
    assert "helped" in json.dumps(helper_entry["messages"])

    by_agent = [(call["agent"], call["name"], call["status"]) for call in data["tools"]]
    assert ("helper", "shell", "success") in by_agent and ("lead", "delegate", "success") in by_agent
    assert all(call["seconds"] >= 0 for call in data["tools"])
    assert [call["at"] for call in data["tools"]] == sorted(call["at"] for call in data["tools"])
    assert len(data["models"]) == len(lead_requests) + len(helper_requests)


def test_what_the_window_dropped_is_still_in_it():
    # In process: the fake llama server counts the request's assistant turns to pick its reply, which a trimmed
    # window would set back.
    from strands import Agent

    from harness import agent as harness_agent

    from .test_agent_loop import Looping, step

    agent = Agent(model=Looping(15), tools=[step], callback_handler=None,
                  conversation_manager=harness_agent.ChunkedWindow(window_size=12, per_turn=True))
    record = transcript.Transcript()
    record.watch(agent)
    harness_agent.call(agent, "Go.")
    messages = record.agents[0]["messages"]
    assert len(agent.messages) < 20 <= len(messages)  # the window dropped some; the transcript has them all
    assert "Go." in json.dumps(messages[0][1])


def test_claude_codes_stream_is_recorded(tmp_path):
    ctx = HarnessContext(target=LocalTarget(repo=str(tmp_path)), cfg=HarnessConfig(), slug="t", branch="b",
                         state_key="k")
    engine = ClaudeCodeEngine(ctx)
    transcript.of(ctx).claude({}, prompt="Fix it.")
    for event in ({"type": "system", "subtype": "init"},
                  {"type": "assistant", "message": {"role": "assistant", "content": [
                      {"type": "text", "text": "Running the tests."},
                      {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "pytest"}}]}},
                  {"type": "user", "message": {"role": "user", "content": [
                      {"type": "tool_result", "tool_use_id": "tu1", "content": "1 failed", "is_error": True}]}}):
        engine._on_line(json.dumps(event))
    data = transcript.of(ctx).data(ctx, "paused", {"metrics": None, "config": None})
    entry = data["agents"][0]
    assert (entry["name"], entry["format"]) == ("lead", "claude-code")
    assert [m["role"] for m in entry["messages"]] == ["user", "assistant", "user"]  # the init event is not a message
    assert entry["messages"][0]["content"][0]["text"] == "Fix it."
    assert data["tools"] == [{"agent": "lead", "id": "tu1", "name": "Bash", "at": data["tools"][0]["at"],
                              "seconds": data["tools"][0]["seconds"], "status": "error"}]


def test_the_newest_are_kept_and_none_is_written_when_off(tmp_path):
    def ctx(kept):
        return SimpleNamespace(cfg=replace(HarnessConfig(), state_dir=str(tmp_path), transcripts_kept=kept),
                               state_key="k", slug="t", branch="b", transcript=None)

    paths = []
    for started in (1_700_000_000, 1_700_000_100, 1_700_000_200):
        run = ctx(2)
        record = transcript.of(run)
        record.started = started
        record.claude({}, prompt="go")
        paths.append(record.write(run, "paused", {}))
    assert [p.exists() for p in paths] == [False, True, True]

    off = ctx(0)
    transcript.of(off).claude({}, prompt="go")
    assert transcript.of(off).write(off, "paused", {}) is None
    nothing = ctx(5)
    assert transcript.of(nothing).write(nothing, "paused", {}) is None  # nothing recorded, nothing written
