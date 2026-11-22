"""The claude-code engine end to end, with a fake `claude` standing in for the model."""

import json
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from harness import cli
from harness.claude_code import OUTCOME_PATH

from .conftest import git as sh_git
from .test_cli import _args

FAKE = r'''#!PYTHON
import json, os, sys, time
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
if mode == "slow":  # still at work when the burst's time is up
    time.sleep(60)
outcome = None
if mode in ("pause", "linger"):
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
      "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "output_tokens": 5},
      "result": os.environ.get("FAKE_CLAUDE_REPLY", "")})
if mode == "linger":  # a background job, or a wake-up it scheduled, keeps it running after its turn
    time.sleep(60)
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
    assert ctx.said["summary"] == "did a thing" and ctx.said["next_step"] == "do the next thing"  # for HARNESS_RESULT
    assert "2 turn(s)" in report and "$0.010" in report
    pushed = sh_git(origin, "ls-tree", "-r", "--name-only", "harness/t").split()
    assert "work.txt" in pushed and "docs/tasks/t.md" in pushed
    assert OUTCOME_PATH not in pushed
    doc = sh_git(origin, "show", "harness/t:docs/tasks/t.md")
    assert "did a thing" in doc and "do the next thing" in doc

    (argv,) = fake_claude()
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    settings = json.loads(argv[argv.index("--settings") + 1])
    hooks = settings["hooks"]["PreToolUse"][0]
    assert hooks["matcher"] == "Bash" and "git_policy.py" in hooks["hooks"][0]["command"]
    assert "attribution" not in settings  # Claude Code's own, unless claude_attribution is false
    assert "burst-harness" in argv[argv.index("--append-system-prompt") + 1]
    assert not any("dangerously" in a or "bypass" in a.lower() for a in argv)


def test_claude_attribution_false_leaves_it_out_of_commits_and_prs(origin, tmp_path, monkeypatch, fake_claude):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "pause")
    monkeypatch.setenv("HARNESS_CLAUDE_ATTRIBUTION", "false")  # as the orchestrator gives it to a burst
    ctx = _ctx(origin, tmp_path)
    assert ctx.cfg.claude_attribution is False
    _start(ctx)
    (argv,) = fake_claude()
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["attribution"] == {"commit": "", "pr": ""}
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"  # the git rules stay


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
    assert set(ctx.said) == {"reason"}


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


def test_claude_is_given_the_model_unless_claude_args_names_one(origin, tmp_path, monkeypatch, fake_claude):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "pause")
    _start(_ctx(origin, tmp_path))
    argv = fake_claude()[0]
    assert argv[argv.index("--model") + 1] == cli.HarnessConfig().model_id

    monkeypatch.setenv("HARNESS_CLAUDE_ARGS", "--model sonnet")
    ctx = cli._open(_args(repo=str(tmp_path / "w2"), repo_url=str(origin), engine="claude-code"))
    _start(ctx)
    argv = fake_claude()[-1]
    assert argv.count("--model") == 1 and argv[argv.index("--model") + 1] == "sonnet"


def test_bypassing_permissions_is_refused_outside_a_sandbox(origin, tmp_path, fake_claude):
    from harness.claude_code import ClaudeCodeEngine, ClaudeCodeError

    ctx = cli._open(_args(repo=str(tmp_path / "w"), repo_url=str(origin), engine="claude-code",
                          permission_mode="bypassPermissions"))
    with pytest.raises(ClaudeCodeError, match="not on the local target"):
        ClaudeCodeEngine(ctx).prepare()
    ctx.cfg = replace(ctx.cfg, claude_permission_mode="yolo")
    with pytest.raises(ClaudeCodeError, match="one of default, acceptEdits, plan, bypassPermissions"):
        ClaudeCodeEngine(ctx).prepare()
    assert fake_claude() == []


def _merged_round_one(origin, tmp_path):
    """The task's first round, merged: its document is on main, its branch gone."""
    from harness import state

    work = tmp_path / "merged"
    sh_git(tmp_path, "clone", "-q", str(origin), str(work))
    (work / "docs" / "tasks").mkdir(parents=True)
    doc = state.TaskDoc(slug="t", goal="build the thing in phases", done="phase 1",
                        next_step="phase 2: the second part")
    (work / "docs" / "tasks" / "t.md").write_text(doc.render())
    sh_git(work, "add", "-A")
    sh_git(work, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "phase 1 (#1)")
    sh_git(work, "push", "-q", "origin", "main")


def test_a_new_branch_of_a_task_merged_before_is_its_next_round(origin, tmp_path, monkeypatch, fake_claude):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "pause")
    _merged_round_one(origin, tmp_path)
    args = _args(repo=str(tmp_path / "w"), repo_url=str(origin), engine="claude-code", branch="harness/t-2",
                 task="build the thing in phases", message="do phase 2, and keep it small")
    cli.cmd_run(args)
    prompt = (argv := fake_claude()[0])[argv.index("-p") + 1]
    assert "merged into main" in prompt and "harness/t-2" in prompt and "do phase 2, and keep it small" in prompt
    # Not started over from the first prompt: the goal is there only in the task document it is given.
    assert prompt.count("build the thing in phases") == 1
    assert "build the thing in phases" in prompt.split("<task-document", 1)[1].split("</task-document>", 1)[0]
    assert "from earlier rounds, already merged" in prompt
    assert "harness/t-2" in sh_git(origin, "branch", "--list", "harness/t-2")
    doc = sh_git(origin, "show", "harness/t-2:docs/tasks/t.md")
    assert "## Goal\n\nbuild the thing in phases" in doc  # the document carried on, its goal kept


def test_without_a_message_the_next_round_carries_on_from_the_next_step(origin, tmp_path, monkeypatch, fake_claude):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "pause")
    _merged_round_one(origin, tmp_path)
    cli.cmd_run(_args(repo=str(tmp_path / "w"), repo_url=str(origin), engine="claude-code",
                      task="build the thing in phases", message=None))  # its own branch, deleted after the merge
    prompt = (argv := fake_claude()[0])[argv.index("-p") + 1]
    assert "harness/t," in prompt and cli.NEXT_STEP in prompt


def test_a_branch_named_with_branch_is_checked():
    cfg = cli.HarnessConfig()
    assert cli._branch(cfg, _args(slug="t")) == "harness/t"
    assert cli._branch(cfg, _args(slug="t", branch="harness/t-3")) == "harness/t-3"
    for bad in ("-x", "a..b", "a b", "x.lock", "x/", "--upload-pack=y"):
        with pytest.raises(SystemExit, match="not a branch name"):
            cli._branch(cfg, _args(slug="t", branch=bad))


def test_with_no_gateway_the_mcp_keys_go_in_a_file_for_the_run(origin, tmp_path, monkeypatch, fake_claude):
    from harness.claude_code import ClaudeCodeEngine

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "pause")
    monkeypatch.setenv("HARNESS_MCP", "context7=https://mcp.context7.com/mcp")
    monkeypatch.setenv("CONTEXT7_API_KEY", "c7-sekret")
    uploaded = {}
    upload = ClaudeCodeEngine._upload

    def recorded(self, path, content):
        uploaded[path] = content
        upload(self, path, content)

    monkeypatch.setattr(ClaudeCodeEngine, "_upload", recorded)
    ctx = _ctx(origin, tmp_path)  # the local target: no gateway
    _start(ctx)
    (argv,) = fake_claude()
    path = argv[argv.index("--mcp-config") + 1]
    assert path.endswith("mcp.json") and json.loads(uploaded[path]) == {"mcpServers": {"context7": {
        "type": "http", "url": "https://mcp.context7.com/mcp", "headers": {"Authorization": "Bearer c7-sekret"}}}}
    assert not any("c7-sekret" in arg for arg in argv)
    assert not Path(path).exists()  # removed with the run


def test_a_key_that_cannot_be_used_stops_the_run_before_it_starts(origin, tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_MCP", "atlassian=https://mcp.atlassian.com/v2/mcp")
    monkeypatch.setenv("ATLASSIAN_BASIC_AUTH", "bWVAZXhhbXBsZS5jb206dG9r")
    with pytest.raises(SystemExit, match="ATLASSIAN_BASIC_AUTH is user:secret"):
        _ctx(origin, tmp_path)


def test_claude_is_ended_once_its_outcome_is_written_and_its_turn_is_over(origin, tmp_path, monkeypatch,
                                                                         fake_claude):
    """One burst waited 45 minutes for Claude Code's background job, its outcome long written."""
    import time

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "linger")
    ctx = _ctx(origin, tmp_path)
    started = time.time()
    _start(ctx)
    assert time.time() - started < 20 and ctx.terminal == "paused"  # not the minute it would have lingered
    assert "work.txt" in sh_git(origin, "ls-tree", "-r", "--name-only", "harness/t").split()


def test_claude_is_ended_when_the_bursts_time_is_up_and_the_burst_pauses(origin, tmp_path, monkeypatch,
                                                                       fake_claude):
    import time

    from harness import budget

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "slow")
    ctx = _ctx(origin, tmp_path)
    stop = time.time() + 3
    monkeypatch.setattr(budget.Budget, "stop_at", lambda self: stop)  # as max_burst_minutes or a deadline sets it
    started = time.time()
    _start(ctx)
    assert time.time() - started < 25
    assert ctx.terminal == "paused" and ctx.said["budget"] == "the burst's time is up"
    assert "work.txt" in sh_git(origin, "ls-tree", "-r", "--name-only", "harness/t").split()  # nothing stranded


def test_a_lead_whose_server_is_down_falls_back_to_claude_code(origin, tmp_path, monkeypatch, fake_claude):
    from harness import models

    monkeypatch.setenv("FAKE_CLAUDE_MODE", "pause")
    monkeypatch.setenv("HARNESS_FALLBACK_TO", "claude-code")
    monkeypatch.setattr(models, "reachable", lambda url: url != "http://127.0.0.1:9")
    ctx = cli._open(_args(repo=str(tmp_path / "w"), repo_url=str(origin), engine="strands", provider="llamacpp",
                          base_url="http://127.0.0.1:9"))
    _start(ctx)
    assert ctx.terminal == "paused" and ctx.cfg.engine == "claude-code"
    assert ctx.fallback == {"from": "http://127.0.0.1:9", "to": "claude-code"}
    (argv,) = fake_claude()
    assert "--model" not in argv  # fallback_model unset: Claude Code's own default
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"  # this machine: the mode as set

    # With the server there, nothing changes; and without fallback_to, a down server is the burst's to find out.
    up = cli._open(_args(repo=str(tmp_path / "w2"), repo_url=str(origin), engine="strands", provider="llamacpp",
                         base_url="http://127.0.0.1:10"))
    cli._fall_back(up)
    assert up.cfg.engine == "strands" and up.fallback is None
    monkeypatch.delenv("HARNESS_FALLBACK_TO")
    off = cli._open(_args(repo=str(tmp_path / "w3"), repo_url=str(origin), engine="strands", provider="llamacpp",
                          base_url="http://127.0.0.1:9"))
    cli._fall_back(off)
    assert off.cfg.engine == "strands" and off.fallback is None


def test_a_fallback_can_be_a_profile_and_must_name_one(tmp_path):
    from types import SimpleNamespace

    from harness import models
    from harness.agents import AgentsError
    from harness.config import HarnessConfig

    profile = {"provider": "anthropic", "model_id": "claude-sonnet-5-5", "use_for": "anything"}
    cfg = HarnessConfig.load(None, {"provider": "llamacpp", "base_url": "http://127.0.0.1:9",
                                    "agents": {"claude": profile}, "fallback_to": "claude"}, user_config=None)
    ctx = SimpleNamespace(cfg=cfg, agent="", fallback=None, target=None)
    real = models.reachable
    try:
        models.reachable = lambda url: False
        cli._fall_back(ctx)
    finally:
        models.reachable = real
    assert (ctx.cfg.provider, ctx.cfg.model_id, ctx.agent) == ("anthropic", "claude-sonnet-5-5", "claude")
    with pytest.raises(AgentsError, match="fallback_to names no profile: nobody"):
        HarnessConfig.load(None, {"fallback_to": "nobody"}, user_config=None)


def test_reachable_counts_any_reply_and_not_a_closed_port(tmp_path):
    import http.server
    import threading

    from harness import models

    class Loading(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(503)  # llama-server while it loads a model: there, not ready
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Loading)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert models.reachable(f"http://127.0.0.1:{server.server_port}", tries=1)
    finally:
        server.shutdown()
    assert not models.reachable("http://127.0.0.1:9", tries=2, wait=0.01)
