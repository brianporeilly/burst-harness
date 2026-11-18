"""check_command: run before a PR is opened. A failure refuses the PR with its output; past max_fixup_attempts
failures the model is told to pause, and finishing anyway pauses it."""

import json
from dataclasses import replace

import pytest

from harness import cli, terminal
from harness.claude_code import ClaudeCodeEngine
from harness.config import HarnessConfig
from harness.context import HarnessContext
from harness.exec_target import LocalTarget

from . import fake_llama
from .conftest import git
from .test_agents import llama  # noqa: F401 -- the fixture

FINISH = {"tool": "finish_with_pr", "args": {"title": "Fix it", "body": "Fixed."}}


def _result(out):
    return json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])


def _start(tmp_path, origin, url, monkeypatch, command):
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    monkeypatch.setenv("HARNESS_CHECK_COMMAND", command)
    cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "start", "t1", "--task", "Go."])
    return work


def test_a_failing_check_refuses_the_pr_until_it_passes(tmp_path, origin, llama, capsys, monkeypatch):  # noqa: F811
    url, requests = llama([FINISH, {"tool": "shell", "args": {"command": "touch ok"}}, FINISH, {"text": "done"}])
    _start(tmp_path, origin, url, monkeypatch, "echo checking; test -f ok || { echo 'ok is missing' >&2; exit 3; }")
    result = _result(capsys.readouterr().out)
    # Pushed, no PR (the origin is a path): the end a PR would be, reached on the second try.
    assert result["state"] == "pushed_no_pr" and len(requests) == 3
    assert result["metrics"]["checks"] == {"run": 2, "failed": 1}
    refused = fake_llama._text(requests[1]["messages"][-1]["content"])
    assert "No PR: the repo's check" in refused and "exited 3" in refused and "ok is missing" in refused
    assert "Nothing was pushed" in refused
    assert "the harness runs this repo's check" in json.dumps(requests[0])  # told up front


def test_failing_again_and_again_ends_in_a_pause(tmp_path, origin, llama, capsys, monkeypatch):  # noqa: F811
    url, requests = llama([FINISH] * 6 + [{"text": "done"}])
    work = _start(tmp_path, origin, url, monkeypatch, "false")
    result = _result(capsys.readouterr().out)
    # Three failures to fix (max_fixup_attempts), the third saying to pause; the fourth finish is paused for it.
    assert len(requests) == 4 and result["state"] == "paused"
    assert result["metrics"]["checks"] == {"run": 4, "failed": 4}
    assert "don't finish again" not in fake_llama._text(requests[2]["messages"][-1]["content"])
    assert "don't finish again" in fake_llama._text(requests[3]["messages"][-1]["content"])
    assert result["said"]["next_step"] == "Make `false` pass."
    assert "Stopped trying to open a PR" in (work / "docs" / "tasks" / "t1.md").read_text()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "task")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "start")
    return tmp_path


def _ctx(repo, **settings):
    return HarnessContext(target=LocalTarget(repo=str(repo)), cfg=replace(HarnessConfig(), **settings), slug="t",
                          branch="task", state_key="k")


def test_a_slow_check_times_out_and_long_output_is_cut_to_its_end(repo):
    ctx = _ctx(repo, check_command="sleep 5", check_timeout=1)
    with pytest.raises(terminal.CheckFailed, match=r"timed out after 1s"):
        terminal.check(ctx)

    ctx = _ctx(repo, check_command="seq 1 5000; exit 1")
    with pytest.raises(terminal.CheckFailed) as failed:
        terminal.check(ctx)
    text = str(failed.value)
    assert "[...]" in text and text.endswith("5000") and "\n1\n" not in text
    assert len(text) < terminal.CHECK_OUTPUT_CHARS + 400

    ctx = _ctx(repo)  # no check_command: nothing runs
    terminal.check(ctx)
    assert ctx.checks == {}


def test_claude_codes_finish_outcome_is_refused_with_the_checks_output(repo):
    ctx = _ctx(repo, check_command="echo 'tests: 1 failed'; exit 1")
    engine = ClaudeCodeEngine(ctx)
    error = engine.apply({"state": "finish", "title": "t", "body": "b"})
    assert error.startswith("No PR: the repo's check") and "tests: 1 failed" in error
    assert ctx.terminal is None and git(repo, "log", "--oneline").count("\n") == 0  # nothing committed
    assert "the harness runs this repo's check" in engine.rules()
