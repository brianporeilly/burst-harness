"""engine=claude-code with delegate_to: a fake `claude` speaking the SDK's stream-json, calling the driver's delegate
tool, whose Strands delegate runs against a fake llama.cpp server."""

import json
import os
import stat
import sys

import pytest

from harness import cli
from harness.claude_lead import TOOL
from harness.config import CONFIG_FILES_ENV

from . import fake_llama
from .test_agents import DEAD
from .test_cli import _args

FAKE = r'''#!PYTHON
import json, os, sys
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
mode = os.environ.get("FAKE_CLAUDE_MODE", "delegate")
def emit(event): print(json.dumps(event), flush=True)
def say(*content): emit({"type": "assistant", "message": {"model": "fake", "content": list(content)}})
def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None
if mode == "fail":
    emit({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in"})
    sys.exit(1)
while (message := read()) is not None:
    if message["type"] == "control_request" and message["request"]["subtype"] == "initialize":
        emit({"type": "control_response", "response": {"subtype": "success", "request_id": message["request_id"],
                                                       "response": {}}})
    elif message["type"] == "user":
        def mcp(n, method, params):
            # As Claude Code does it: a JSON-RPC message for the driver's server, in a control request.
            emit({"type": "control_request", "request_id": f"m{n}", "request": {
                "subtype": "mcp_message", "server_name": "harness",
                "message": {"jsonrpc": "2.0", "id": n, "method": method, "params": params}}})
            reply = read()
            assert reply["type"] == "control_response" and reply["response"]["request_id"] == f"m{n}", reply
            return reply["response"]["response"]["mcp_response"]
        mcp(0, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "fake", "version": "0"}})
        reports = []
        for n, to in enumerate(json.loads(os.environ["FAKE_CLAUDE_DELEGATES"]), 1):
            arguments = {"to": to, "task": "Read notes.txt; say what it says."}
            say({"type": "tool_use", "id": f"t{n}", "name": "mcp__harness__delegate", "input": arguments})
            answer = mcp(n, "tools/call", {"name": "delegate", "arguments": arguments})
            reports.append(answer["result"]["content"][0]["text"] if "result" in answer else json.dumps(answer))
        os.makedirs(".harness", exist_ok=True)
        json.dump({"state": "pause", "summary": " | ".join(reports), "next_step": "n"}, open(".harness/outcome.json", "w"))
        say({"type": "text", "text": "prompt: " + message["message"]["content"]})
        emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 3, "total_cost_usd": 0.02,
              "duration_ms": 1, "duration_api_ms": 1, "session_id": "s", "usage": {"input_tokens": 10, "output_tokens": 5}})
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
    monkeypatch.setenv("FAKE_CLAUDE_DELEGATES", '["helper"]')

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return calls


@pytest.fixture
def helper(tmp_path, monkeypatch):
    """A profile `helper` on a fake llama.cpp server that reads notes.txt and reports it; (its requests)."""
    server, requests = fake_llama.serve([
        {"tool": "shell", "args": {"command": "cat notes.txt"}},
        {"text": "notes.txt says: {last_tool}"},
    ])
    config = tmp_path / "agents.toml"
    config.write_text(f'delegate_to = ["helper"]\n[agents.helper]\nprovider = "llamacpp"\n'
                      f'base_url = "http://127.0.0.1:{server.server_port}"\nmodel_id = "helper"\n'
                      f'use_for = "Reading files"\n[agents.idle]\nprovider = "llamacpp"\nbase_url = "{DEAD}"\n')
    monkeypatch.setenv(CONFIG_FILES_ENV, str(config))
    yield requests
    server.shutdown()


def _start(origin, tmp_path, prompt="do the task"):
    ctx = cli._open(_args(repo=str(tmp_path / "w"), repo_url=str(origin), engine="claude-code"))
    cli.git.start_branch(ctx.target, ctx.branch, base="main", remote="origin")
    (tmp_path / "w" / "notes.txt").write_text("the sky is green\n")
    return ctx, cli._drive(ctx, prompt, resume=False)


def test_claude_code_delegates_to_a_strands_profile_through_the_driver(origin, tmp_path, fake_claude, helper, capsys):
    ctx, report = _start(origin, tmp_path)
    out = capsys.readouterr().out
    # The delegate ran on the driver against its own model, its tool in the checkout, and only its report came back.
    assert ctx.terminal == "paused" and ctx.said["summary"].startswith("notes.txt says: ") \
        and "the sky is green" in ctx.said["summary"]
    assert len(helper) == 2 and "Read notes.txt" in fake_llama._text(helper[0]["messages"][-1]["content"])
    assert "--- delegating to helper (llamacpp helper) ---" in out and "--- back from helper ---" in out
    assert "Claude Code may delegate to: helper (llamacpp helper)." in out
    assert "  -> mcp__harness__delegate:" in out and "prompt: do the task" in out  # printed as without the SDK
    assert ctx.delegated["helper"]["calls"] == 1 and "3 turn(s)" in report and "$0.020" in report
    assert "delegated to helper (llamacpp helper), 1 time(s):" in report

    (argv,) = fake_claude()
    assert "-p" not in argv and argv[argv.index("--input-format") + 1] == "stream-json"  # the prompt is on stdin
    servers = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]
    assert servers == {"harness": {"type": "sdk", "name": "harness"}}
    assert argv[argv.index("--allowedTools") + 1] == TOOL
    system = argv[argv.index("--append-system-prompt") + 1]
    assert "burst-harness" in system and "with the delegate tool:\n- helper: Reading files" in system


def test_a_delegate_that_is_no_profile_is_told_and_claude_carries_on(origin, tmp_path, fake_claude, helper,
                                                                     monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_DELEGATES", '["nobody"]')
    ctx, _ = _start(origin, tmp_path)
    assert ctx.terminal == "paused" and "Rejected: no agent 'nobody'" in ctx.said["summary"]
    assert not helper and "nobody" not in ctx.delegated


def test_a_claude_that_fails_before_its_session_starts_is_not_retried(origin, tmp_path, fake_claude, helper,
                                                                      monkeypatch, capsys):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    ctx, _ = _start(origin, tmp_path)
    assert len(fake_claude()) == 1 and ctx.terminal is None
    assert "claude exited with status 1; not retrying." in capsys.readouterr().err


def test_without_delegate_to_claude_is_run_as_before(origin, tmp_path, fake_claude, helper, monkeypatch):
    monkeypatch.setenv("HARNESS_DELEGATE_TO", "")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")  # answers no SDK: only a plain `claude -p` gets this far
    _start(origin, tmp_path)
    (argv,) = fake_claude()
    assert argv[argv.index("-p") + 1] == "do the task" and "--input-format" not in argv and "--mcp-config" not in argv


def test_a_sigint_while_claude_code_waits_on_a_delegate_stops_the_delegate(origin, tmp_path, fake_claude, monkeypatch):
    import signal
    import threading
    import time

    server, requests = fake_llama.serve([{"tool": "shell", "args": {"command": "sleep 0.05"}}] * 300 + [{"text": "x"}])
    config = tmp_path / "slow.toml"
    config.write_text(f'delegate_to = ["slow"]\n[agents.slow]\nprovider = "llamacpp"\n'
                      f'base_url = "http://127.0.0.1:{server.server_port}"\nmodel_id = "slow"\n')
    monkeypatch.setenv(CONFIG_FILES_ENV, str(config))
    monkeypatch.setenv("FAKE_CLAUDE_DELEGATES", '["slow"]')
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)  # a test runner may have its own
    try:
        threading.Timer(1.5, os.kill, args=(os.getpid(), signal.SIGINT)).start()
        started = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            _start(origin, tmp_path)
        # 300 calls of 50ms each would take 15s and more.
        assert time.monotonic() - started < 8 and 0 < len(requests) < 150
        time.sleep(0.5)
        made = len(requests)
        time.sleep(1)
        assert len(requests) == made  # the delegate stopped too, rather than going on in its thread
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGINT, previous)
        server.shutdown()
