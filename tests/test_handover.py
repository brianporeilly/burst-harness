"""Bursts start from the repo (handover.py): a fresh conversation, given the task document and git as they are
now, rather than the previous burst's messages. resume_from = "snapshot" keeps the old way."""

import json

import pytest

from harness import cli, handover

from . import fake_llama
from .conftest import git
from .test_agents import llama  # noqa: F401 -- the fixture

PAUSE = {"tool": "pause_for_review", "args": {"summary": "Did step one.", "next_step": "Do step two."}}
FINISH = {"tool": "finish_with_pr", "args": {"title": "Round one", "body": "Done."}}
ROUND_ONE = "Review the roadmap and scaffold the docs."


def _result(out):
    return json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])


def _first_prompt(request):
    return fake_llama._text(request["messages"][1]["content"])


def _clone(tmp_path, origin, name="work"):
    work = tmp_path / name
    git(tmp_path, "clone", "-q", str(origin), str(work))
    return work


def _cli(url, work, *more):
    return cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), *more])


def test_a_resumed_burst_starts_fresh_from_the_document_and_git(tmp_path, origin, llama, capsys):  # noqa: F811
    url, requests = llama([{"tool": "shell", "args": {"command": "echo one > one.txt"}}, PAUSE, {"text": "Paused."}])
    work = _clone(tmp_path, origin)
    assert _cli(url, work, "start", "t1", "--task", ROUND_ONE) == 0
    capsys.readouterr()

    url, requests = llama([PAUSE, {"text": "Paused."}])
    assert _cli(url, work, "resume", "t1") == 0
    out = capsys.readouterr().out
    assert "Starting a fresh conversation from the repo" in out and "Restored" not in out
    first = requests[0]["messages"]
    assert [m["role"] for m in first] == ["system", "user"]  # nothing of the last burst's conversation
    prompt = _first_prompt(requests[0])
    assert "fresh conversation" in prompt and cli.NEXT_STEP in prompt
    document = prompt.split("<task-document", 1)[1].split("</task-document>", 1)[0]
    assert "Do step two." in document and "Did step one." in document  # the document as it is now
    assert "wip(t1)" in prompt.split("<commits", 1)[1]  # the branch's commits since the base
    assert (tmp_path / "state" / "origin" / "session" / "t1.previous").is_dir()  # set aside, not deleted


def test_a_next_round_does_not_carry_the_last_rounds_conversation(tmp_path, origin, llama, capsys):  # noqa: F811
    url, _ = llama([{"tool": "shell", "args": {"command": "echo one > one.txt"}}, FINISH, {"text": "Done."}])
    work = _clone(tmp_path, origin)
    assert _cli(url, work, "start", "t1", "--task", ROUND_ONE) == 0
    capsys.readouterr()
    git(origin, "update-ref", "refs/heads/main", "refs/heads/harness/t1")  # round one's PR, merged

    url, requests = llama([PAUSE, {"text": "Paused."}])
    work = _clone(tmp_path, origin, "round-two")
    assert _cli(url, work, "run", "t1", "--task", ROUND_ONE, "--branch", "harness/t1-2",
                "--message", "Build the workspace.") == 0
    assert [m["role"] for m in requests[0]["messages"]] == ["system", "user"]
    prompt = _first_prompt(requests[0])
    assert "next round" in prompt and "harness/t1-2" in prompt and "Build the workspace." in prompt
    assert "from earlier rounds, already merged" in prompt
    # Round one's prompt is there only as the document's goal, not as a conversation to carry on.
    assert prompt.count(ROUND_ONE) == 1 and ROUND_ONE in prompt.split("</task-document>")[0]
    assert _result(capsys.readouterr().out)["state"] == "paused"


def test_an_answer_comes_with_its_question(tmp_path, origin, llama, capsys):  # noqa: F811
    url, _ = llama([{"tool": "handoff_to_user", "args": {"message": "Which colour should the logo be?"}},
                    {"text": "Waiting."}])
    work = _clone(tmp_path, origin)
    assert _cli(url, work, "start", "t1", "--task", "Make a logo.") == cli.EXIT_NEEDS_INPUT
    capsys.readouterr()

    url, requests = llama([PAUSE, {"text": "Paused."}])
    assert _cli(url, work, "resume", "t1", "--message", "Blue.") == 0
    prompt = _first_prompt(requests[0])
    assert "The last burst ended by asking:\n- Which colour should the logo be?\n\nThe answer: Blue." in prompt
    capsys.readouterr()

    url, requests = llama([PAUSE, {"text": "Paused."}])  # asked once: a later burst isn't told it again
    assert _cli(url, work, "resume", "t1", "--message", "Carry on.") == 0
    assert "ended by asking" not in _first_prompt(requests[0])


def test_resume_from_snapshot_keeps_the_last_bursts_conversation(tmp_path, origin, llama, capsys,  # noqa: F811
                                                                 monkeypatch):
    monkeypatch.setenv("HARNESS_RESUME_FROM", "snapshot")
    url, _ = llama([PAUSE, {"text": "Paused."}])
    work = _clone(tmp_path, origin)
    assert _cli(url, work, "start", "t1", "--task", ROUND_ONE) == 0
    capsys.readouterr()

    url, requests = llama([PAUSE, PAUSE, {"text": "Paused."}])
    _cli(url, work, "resume", "t1")
    out = capsys.readouterr().out
    assert "Restored" in out and "fresh conversation" not in out
    assert len(requests[0]["messages"]) > 2 and ROUND_ONE in json.dumps(requests[0]["messages"])


def test_resume_from_is_checked_and_shapes_the_variant(tmp_path, origin, monkeypatch):
    from dataclasses import replace

    from harness import metrics
    from harness.config import HarnessConfig

    base = HarnessConfig()
    assert metrics.config_label(replace(base, resume_from="snapshot"))["hash"] != metrics.config_label(base)["hash"]
    monkeypatch.setenv("HARNESS_RESUME_FROM", "memory")
    with pytest.raises(SystemExit, match="Unknown resume_from 'memory'"):
        cli.main(["--repo", str(_clone(tmp_path, origin)), "resume", "t1"])


def test_set_aside_keeps_one_previous_snapshot(tmp_path):
    current = handover.session_dir(tmp_path, "t1")
    assert not handover.set_aside(tmp_path, "t1")
    for n in (1, 2):
        current.mkdir(parents=True)
        (current / "snapshot.json").write_text(str(n))
        assert handover.set_aside(tmp_path, "t1") and not current.exists()
    assert (tmp_path / "session" / "t1.previous" / "snapshot.json").read_text() == "2"
