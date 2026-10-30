import importlib.util
import json
import sys
from pathlib import Path

import pytest

from harness import containers, mcp, session
from harness.config import HarnessConfig

CONTEXT7 = "context7=https://mcp.context7.com/mcp"

_spec = importlib.util.spec_from_file_location(
    "burst_gateway", Path(__file__).resolve().parent.parent / "gateway" / "burst_gateway.py"
)
gw = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gw
_spec.loader.exec_module(gw)


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    monkeypatch.delenv("CONTEXT7_API_KEY", raising=False)


def test_servers_are_name_equals_https_url():
    cfg = HarnessConfig(mcp=(CONTEXT7, " docs = https://docs.lan:8443/mcp?x=1 "))
    assert mcp.servers(cfg) == {"context7": "https://mcp.context7.com/mcp", "docs": "https://docs.lan:8443/mcp?x=1"}
    assert mcp.servers(HarnessConfig()) == {}


@pytest.mark.parametrize("entry, message", [
    ("https://mcp.context7.com/mcp", "NAME=URL"),
    ("a b=https://x.com/mcp", "NAME=URL"),
    ("c7=http://mcp.context7.com/mcp", "only passes HTTPS"),
    ("c7=https:///mcp", "only passes HTTPS"),
])
def test_bad_entries_are_refused_when_the_config_loads(entry, message):
    with pytest.raises(ValueError, match=message):
        HarnessConfig.load(None, {"mcp": (entry,)}, user_config=None)
    with pytest.raises(ValueError, match="listed twice"):
        HarnessConfig.load(None, {"mcp": (CONTEXT7, CONTEXT7)}, user_config=None)


def test_plain_http_on_loopback_is_for_strands_alone():
    local = ("planner=http://127.0.0.1:8931/mcp", "v6=http://[::1]:8931/mcp", "named=http://localhost:8931/mcp")
    cfg = HarnessConfig.load(None, {"mcp": local}, user_config=None)  # engine=strands by default
    assert mcp.servers(cfg)["planner"] == "http://127.0.0.1:8931/mcp" and len(mcp.servers(cfg)) == 3
    assert mcp.hosts(cfg) == () and mcp.inject_rules(cfg) == []  # from the driver: nothing for the gateway
    for entry in ("lan=http://10.0.0.5:8931/mcp", "lookalike=http://127.0.0.1.example.com/mcp"):
        with pytest.raises(ValueError, match="only passes HTTPS"):
            HarnessConfig.load(None, {"mcp": (entry,)}, user_config=None)
    for engine in ("opencode", "claude-code"):
        with pytest.raises(ValueError, match="only passes HTTPS"):
            HarnessConfig.load(None, {"mcp": local[:1], "engine": engine}, user_config=None)


def test_the_setting_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("HARNESS_MCP", f"{CONTEXT7},docs=https://docs.lan/mcp")
    assert list(mcp.servers(HarnessConfig.load(None, {}, user_config=None))) == ["context7", "docs"]


def test_a_sandbox_engine_reaches_the_servers_through_the_gateway(monkeypatch):
    cfg = HarnessConfig(engine="opencode", mcp=(CONTEXT7, "docs=https://docs.lan:8443/mcp"), egress_allow=("a.com",))
    assert containers.allowlist(cfg) == ["a.com", "mcp.context7.com", "docs.lan:8443"]
    # Read-only jobs may make MCP's requests, to the server's URL only.
    assert {"host": "mcp.context7.com", "method": "POST", "path": "/mcp*"} in mcp.writes(cfg)
    assert containers.inject_rules(cfg) == []  # no key, nothing to add

    monkeypatch.setenv("CONTEXT7_API_KEY", "c7-sekret")
    assert containers.inject_rules(cfg) == [
        {"host": "mcp.context7.com", "path": "/mcp*", "headers": {"Authorization": "Bearer c7-sekret"}}]
    # The gateway takes the rules as they are, and adds the key only there.
    sandbox = gw.Sandbox(inject=gw.Inject.parse(json.dumps(containers.inject_rules(cfg))),
                         writes=gw.Write.parse(json.dumps(list(mcp.writes(cfg)))), readonly=True)
    assert sandbox.inject_for("mcp.context7.com", "/mcp") is not None
    assert sandbox.inject_for("mcp.context7.com", "/api/v1/search") is None
    assert sandbox.may_send("mcp.context7.com", "POST", "/mcp")
    assert not sandbox.may_send("mcp.context7.com", "POST", "/api/v1/search")


def test_strands_connects_from_the_driver(monkeypatch):
    monkeypatch.setenv("CONTEXT7_API_KEY", "c7-sekret")
    cfg = HarnessConfig(engine="strands", mcp=(CONTEXT7,))
    assert mcp.hosts(cfg) == () and mcp.inject_rules(cfg) == [] and mcp.writes(cfg) == ()
    (client,) = mcp.clients(cfg)
    from strands.tools.mcp import MCPClient
    assert isinstance(client, MCPClient)
    assert client._prefix == "context7" and client.continue_on_error
    assert mcp.clients(HarnessConfig()) == []


def test_opencode_is_told_about_the_servers():
    cfg = HarnessConfig(provider="openai", base_url="https://api.example.com/v1", mcp=(CONTEXT7,))
    assert session.opencode_config(cfg)["mcp"] == {
        "context7": {"type": "remote", "url": "https://mcp.context7.com/mcp", "enabled": True}}
    assert "mcp" not in session.opencode_config(HarnessConfig(provider="openai", base_url="https://api.example.com/v1"))


def test_a_claude_code_session_gets_them_in_its_claude_json():
    assert json.loads(session.claude_json(HarnessConfig(mcp=(CONTEXT7,)))["CLAUDE_JSON"]) == {
        "mcpServers": {"context7": {"type": "http", "url": "https://mcp.context7.com/mcp"}}}
    assert session.claude_json(HarnessConfig()) == {}


def test_claude_code_runs_get_the_servers_with_their_tools_allowed():
    from types import SimpleNamespace

    from harness.claude_code import ClaudeCodeEngine
    from harness.exec_target import LocalTarget

    def command(cfg):
        engine = ClaudeCodeEngine.__new__(ClaudeCodeEngine)
        engine.ctx = SimpleNamespace(cfg=cfg, branch="b", slug="s")
        engine.target, engine.hook_path, engine.has_env_file = LocalTarget("."), "hook.py", False
        import shlex
        return shlex.split(engine.command("-p", "go"))

    argv = command(HarnessConfig(mcp=(CONTEXT7, "docs=https://docs.lan/mcp"), claude_args="--verbose"))
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {
        "context7": {"type": "http", "url": "https://mcp.context7.com/mcp"},
        "docs": {"type": "http", "url": "https://docs.lan/mcp"}}}
    assert argv[argv.index("--allowedTools") + 1] == "mcp__context7,mcp__docs"
    assert argv[-1] == "--verbose"
    assert "--mcp-config" not in command(HarnessConfig())


def test_a_report_job_says_where_the_servers_are_reached_from(tmp_path, monkeypatch):
    from harness import job

    directory = tmp_path / "digest"
    directory.mkdir()
    (directory / "prompt.md").write_text("do it")
    (directory / "job.toml").write_text(f'engine = "opencode"\nmcp = ["{CONTEXT7}"]\n')
    spec = job.load(directory)
    cfg = job.config(spec)
    assert {"host": "mcp.context7.com", "method": "POST", "path": "/mcp*"} in job.write_rules(spec, cfg)
    assert "mcp      context7: https://mcp.context7.com/mcp (from the sandbox)" in job.describe(spec, cfg)
    monkeypatch.setenv("CONTEXT7_API_KEY", "c7-sekret")
    text = job.describe(spec, cfg)
    assert "key $CONTEXT7_API_KEY added by the gateway" in text and "c7-sekret" not in text


def test_opencode_gets_the_servers_even_without_a_model_to_describe():
    cfg = HarnessConfig(mcp=(CONTEXT7,))
    assert session.opencode_config(cfg) is None  # anthropic, but no key on the driver
    assert session.opencode_file(cfg, None)["mcp"]["context7"]["url"] == "https://mcp.context7.com/mcp"
    assert session.opencode_file(HarnessConfig(), None) is None
