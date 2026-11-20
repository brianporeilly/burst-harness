"""A smoke test of a whole burst on the fake llama server: a tiny task that sets up, delegates, edits, passes its
check and review, and pushes, with every number filled in. Then the same burst replayed from its transcript.

What launched a burst (the orchestrator, a Job, local-llm-benchmark) reads HARNESS_RESULT and the transcript;
this is the check that a change to the harness still gives them everything they read. Whether a change helped
is the benchmark's to measure, not this.
"""

import json
from pathlib import Path

import pytest

from harness import cli, metrics

from . import fake_llama
from .conftest import git, make_origin
from .test_agents import llama  # noqa: F401 -- the fixture

LEAD = [
    {"tool": "delegate", "args": {"to": "helper", "task": "Write hello.txt saying hi."}},
    {"tool": "shell", "args": {"command": "cat hello.txt"}},
    {"tool": "finish_with_pr", "args": {"title": "Say hi", "body": "Adds hello.txt."}},
    {"text": "Done."},
]
HELPER = [{"tool": "shell", "args": {"command": "echo hi > hello.txt"}}, {"text": "Wrote hello.txt."}]
REVIEWER = [{"text": "NO FINDINGS"}]

# HARNESS_RESULT's keys, and its metrics': a field gone, or renamed, breaks whatever reads it. A field added is
# fine, and is added here.
RESULT_KEYS = {"slug", "branch", "state", "pr", "engine", "target", "kept", "model", "base_url", "agent", "delegated",
               "said", "context_files", "fallback", "metrics", "config", "transcript"}
METRIC_KEYS = {"schema", "seconds", "model_seconds", "tool_seconds", "model_calls", "tool_calls", "tool_errors",
               "tokens", "peak_prompt_tokens", "cut_off", "window_drops", "model_retries", "lost_server", "cost_usd",
               "fixups", "handoffs", "checks", "setup", "review"}


def _burst(tmp_path, name, lead, helper, reviewer, capsys):
    """One burst of the tiny task in a fresh clone of a fresh origin, under tmp_path/name."""
    root = tmp_path / name
    root.mkdir()
    origin = make_origin(root)
    work = root / "work"
    git(root, "clone", "-q", str(origin), str(work))
    scenario = root / "smoke.toml"
    scenario.write_text(
        f'provider = "llamacpp"\nbase_url = "{lead}"\ndelegate_to = ["helper"]\nreview_with = "reviewer"\n'
        'setup_command = "echo ready"\ncheck_command = "grep -qx hi hello.txt"\n'
        f'[agents.helper]\nprovider = "llamacpp"\nbase_url = "{helper}"\nmodel_id = "helper"\nuse_for = "writing"\n'
        f'[agents.reviewer]\nprovider = "llamacpp"\nbase_url = "{reviewer}"\nmodel_id = "reviewer"\n'
        'use_for = "reviewing"\n')
    assert cli.main(["--config", str(scenario), "--repo", str(work), "start", "t1", "--task", "Say hi."]) == 0
    out = capsys.readouterr().out
    result = json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])
    return result, origin


@pytest.fixture
def smoke(tmp_path, llama, capsys, monkeypatch):  # noqa: F811
    def run(name, lead=LEAD, helper=HELPER, reviewer=REVIEWER):
        monkeypatch.delenv("HARNESS_CONFIG", raising=False)  # --config adds to it: a second burst would have it twice
        monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / name / "state"))  # a burst of its own, not a resume
        (lead_url, _), (helper_url, _), (reviewer_url, _) = llama(lead), llama(helper), llama(reviewer)
        return _burst(tmp_path, name, lead_url, helper_url, reviewer_url, capsys)

    return run


def test_a_tiny_task_finishes_with_every_number_filled_in(smoke):
    result, origin = smoke("first")

    assert set(result) == RESULT_KEYS
    assert result["state"] == "pushed_no_pr" and result["engine"] == "strands"  # pushed; the origin is a path
    assert git(origin, "show", "harness/t1:hello.txt").strip() == "hi"

    numbers = result["metrics"]
    assert set(numbers) == METRIC_KEYS and numbers["schema"] == metrics.METRICS_SCHEMA
    # A Strands burst on llama.cpp knows all of it: nothing is left null.
    assert all(value is not None for value in numbers.values()), numbers
    assert numbers["model_calls"] == len(LEAD) - 1  # the last turn's "Done." is never asked for: finishing ended it
    assert numbers["tool_calls"] == 3 and numbers["tool_errors"] == 0
    assert numbers["tokens"]["input"] > 0 and numbers["tokens"]["output"] > 0 and numbers["peak_prompt_tokens"] > 0
    assert numbers["cost_usd"] == 0  # local
    assert numbers["setup"]["exit"] == 0 and numbers["checks"] == {"run": 1, "failed": 0}
    assert numbers["review"] == {"by": "reviewer", "outcome": "no findings"}
    assert (numbers["fixups"], numbers["handoffs"], numbers["cut_off"], numbers["model_retries"]) == (0, 0, 0, 0)
    assert numbers["seconds"] >= numbers["model_seconds"] >= 0

    helper = result["delegated"]["helper"]
    assert helper["calls"] == 1 and helper["metrics"]["tool_calls"] == 1 and helper["metrics"]["schema"] == 1
    assert len(result["config"]["hash"]) == 16 and result["config"]["layers"] == ["smoke"]

    data = json.loads(Path(result["transcript"]).read_text())
    assert (data["state"], data["metrics"], data["config"]) == ("pushed_no_pr", numbers, result["config"])
    assert [agent["name"] for agent in data["agents"]][:2] == ["lead", "helper"]
    assert all(call["status"] == "success" for call in data["tools"])


def test_a_recorded_burst_replays_to_the_same_end(smoke):
    first, _ = smoke("recorded")
    recorded = json.loads(Path(first["transcript"]).read_text())

    scripts = fake_llama.from_transcript(recorded)
    assert scripts["lead"][:3] == LEAD[:3] and scripts["helper"] == HELPER and scripts["reviewer"] == REVIEWER
    again, origin = smoke("replayed", scripts["lead"] + [{"text": "Done."}], scripts["helper"], scripts["reviewer"])
    replayed = json.loads(Path(again["transcript"]).read_text())

    assert again["state"] == first["state"] == "pushed_no_pr"
    assert git(origin, "show", "harness/t1:hello.txt").strip() == "hi"
    calls = [(call["agent"], call["name"], call["status"]) for call in recorded["tools"]]
    assert calls and [(call["agent"], call["name"], call["status"]) for call in replayed["tools"]] == calls
    same = ("model_calls", "tool_calls", "tool_errors", "checks", "review", "fixups")
    assert {key: again["metrics"][key] for key in same} == {key: first["metrics"][key] for key in same}


def test_a_transcript_that_cant_be_replayed_says_why():
    two_calls = {"agents": [{"name": "lead", "format": "strands", "messages": [
        {"role": "assistant", "content": [{"text": "Both."}, {"toolUse": {"name": "a", "input": {}}},
                                          {"toolUse": {"name": "b", "input": {"x": 1}}}]},
        {"role": "user", "content": [{"toolResult": {}}]},
        {"role": "assistant", "content": [{"text": "Done."}]}]}]}
    assert fake_llama.from_transcript(two_calls)["lead"] == [
        {"calls": [{"tool": "a", "args": {}}, {"tool": "b", "args": {"x": 1}}]}, {"text": "Done."}]

    with pytest.raises(ValueError, match="more than once"):
        fake_llama.from_transcript({"agents": [{"name": "lead", "format": "strands", "messages": []},
                                               {"name": "helper", "format": "strands", "messages": []},
                                               {"name": "helper", "format": "strands", "messages": []}]})
    with pytest.raises(ValueError, match="claude-code"):
        fake_llama.from_transcript({"agents": [{"name": "lead", "format": "claude-code", "messages": []}]})


def test_a_turn_of_several_tool_calls_runs_them_all(tmp_path, origin, llama, capsys):  # noqa: F811
    url, _ = llama([{"calls": [{"tool": "shell", "args": {"command": "echo one"}},
                               {"tool": "shell", "args": {"command": "echo two"}}]},
                    {"tool": "pause_for_review", "args": {"summary": "s", "next_step": "n"}}, {"text": "Paused."}])
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "start", "t1", "--task", "Go."])
    result = json.loads(capsys.readouterr().out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])
    assert result["metrics"]["tool_calls"] == 3 and result["metrics"]["model_calls"] == 2
