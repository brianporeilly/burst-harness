"""The claude-code engine end to end, with a fake `claude` standing in for the model."""

import json
import os
import stat
import sys

import pytest

from harness import cli
from harness.claude_code import OUTCOME_PATH

from .conftest import git as sh_git
from .test_cli import _args

FAKE = r'''#!PYTHON
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\n")
mode = os.environ["FAKE_CLAUDE_MODE"]
resumed = "--continue" in argv
def emit(event): print(json.dumps(event), flush=True)
emit({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "working on it"},
    {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}}]}})
open("work.txt", "a").write("x\n")
os.makedirs(".harness", exist_ok=True)
outcome = None
if mode == "pause":
    outcome = {"state": "pause", "summary": "did a thing", "next_step": "do the next thing"}
elif mode == "late" and resumed:
    outcome = {"state": "pause", "summary": "late", "next_step": "n"}
elif mode == "bad-then-good":
    outcome = {"state": "abandon", "reason": "not worth it"} if resumed else {"state": "done"}
if outcome:
    json.dump(outcome, open(".harness/outcome.json", "w"))
if mode == "fail":
    emit({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in"})
    sys.exit(1)
emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
      "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "output_tokens": 5}})
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE.replace("PYTHON", sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return calls


def _ctx(origin, tmp_path):
    return cli._open(_args(repo=str(tmp_path / "w"), repo_url=str(origin), engine="claude-code"))


def _start(ctx):
    sh_git_branch = cli.git.start_branch(ctx.target, ctx.branch, base="main", remote="origin")
    assert "Created" in sh_git_branch
    return cli._drive(ctx, "do the task", resume=False)


def test_pause_outcome_commits_and_pushes(origin, tmp_path, monkeypatch, fake_claude):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "pause")
    ctx = _ctx(origin, tmp_path)
    report = _start(ctx)
    assert ctx.terminal == "paused"
    assert "2 turn(s)" in report and "$0.010" in report
    pushed = sh_git(origin, "ls-tree", "-r", "--name-only", "harness/t").split()
    assert "work.txt" in pushed and "docs/tasks/t.md" in pushed
    assert OUTCOME_PATH not in pushed
    doc = sh_git(origin, "show", "harness/t:docs/tasks/t.md")
    assert "did a thing" in doc and "do the next thing" in doc

    (argv,) = fake_claude()
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    hooks = json.loads(argv[argv.index("--settings") + 1])["hooks"]["PreToolUse"][0]
    assert hooks["matcher"] == "Bash" and "git_policy.py" in hooks["hooks"][0]["command"]
    assert "burst-harness" in argv[argv.index("--append-system-prompt") + 1]
    assert not any("dangerously" in a or "bypass" in a.lower() for a in argv)


def test_missing_outcome_is_asked_for(origin, tmp_path, monkeypatch, fake_claude):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "late")
    ctx = _ctx(origin, tmp_path)
    _start(ctx)
    calls = fake_claude()
    assert len(calls) == 2 and "--continue" in calls[1]
    assert "without writing" in calls[1][calls[1].index("-p") + 1]
    assert ctx.terminal == "paused"


def test_invalid_outcome_is_explained(origin, tmp_path, monkeypatch, fake_claude):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "bad-then-good")
    ctx = _ctx(origin, tmp_path)
    _start(ctx)
    calls = fake_claude()
    assert "could not be applied" in calls[1][calls[1].index("-p") + 1]
    assert ctx.terminal == "abandoned"


def test_gives_up_after_fixups(origin, tmp_path, monkeypatch, fake_claude):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "never")
    monkeypatch.setenv("HARNESS_MAX_FIXUP_ATTEMPTS", "2")
    ctx = _ctx(origin, tmp_path)
    _start(ctx)
    assert len(fake_claude()) == 3
    assert ctx.terminal is None
    assert cli._finish(None, ctx) == cli.EXIT_NO_TERMINAL_STATE


def test_failed_claude_is_not_retried(origin, tmp_path, monkeypatch, fake_claude):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    ctx = _ctx(origin, tmp_path)
    _start(ctx)
    assert len(fake_claude()) == 1
    assert ctx.terminal is None
