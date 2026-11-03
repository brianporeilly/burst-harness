"""A provider plugin's target endpoint: Claude Code in the target talks to it, its token kept current."""

import json
import threading
import time
from dataclasses import dataclass, field, replace

import pytest

from harness import containers, job, models
from harness.claude_code import ENV_FILE, TOKEN_FILE, ClaudeCodeEngine, ClaudeCodeError
from harness.config import HarnessConfig
from harness.context import HarnessContext
from harness.exec_target import CommandResult, SubprocessTarget

from . import fake_provider

GATEWAY = "tests.fake_provider:gateway"


@dataclass(frozen=True)
class FakeTarget(SubprocessTarget):
    """A remote target that records what is run in it, and what each command is given on stdin."""

    calls: list = field(default_factory=list, compare=False)
    written: threading.Event = field(default_factory=threading.Event, compare=False)

    def exec_prefix(self, *, stdin=False, tty=False):
        return ["fake-exec"]

    def describe(self):
        return "a fake target"

    def run_shell(self, command, *, check=True, stdin=None, cwd=None):
        self.calls.append((command, stdin))
        if TOKEN_FILE in command:
            self.written.set()
        return CommandResult(0, "", "")

    def uploads(self, path):
        return [stdin for command, stdin in self.calls if f"cat > {path}" in command]


def _engine(provider, **settings):
    target = FakeTarget(repo="/workspace/repo")
    cfg = HarnessConfig(provider=provider, engine="claude-code", **settings)
    return ClaudeCodeEngine(HarnessContext(target=target, cfg=cfg, slug="t", branch="harness/t", state_key="")), target


@pytest.fixture
def driver(monkeypatch):
    """A driver whose own shell has Anthropic settings, as a developer's often does."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-driver")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://driver.example.com")


def test_claude_code_talks_to_the_endpoint_with_a_token_kept_current(driver, monkeypatch):
    monkeypatch.setattr(fake_provider, "TOKENS", fake_provider.Tokens(("token-1", 2), ("token-2", 3600)))
    engine, target = _engine(GATEWAY)
    engine.prepare()
    try:
        assert target.uploads(TOKEN_FILE) == ["token-1"]  # through stdin, never argv
        assert not any("token-1" in command for command, _ in target.calls)
        env = target.uploads(ENV_FILE)[0]
        assert "export ANTHROPIC_BASE_URL=https://ai.example.com/anthropic\n" in env
        assert "export CLAUDE_CODE_API_KEY_HELPER_TTL_MS=300000\n" in env and "export EXAMPLE_FLAG=1\n" in env
        assert "sk-driver" not in env and "driver.example.com" not in env and "ANTHROPIC_API_KEY" not in env
        assert json.loads(engine.hook_settings())["apiKeyHelper"] == f"cat {TOKEN_FILE}"

        # The first token lasts 2s: asked again at half that, and the new one written.
        target.written.clear()
        deadline = time.time() + 5
        while target.uploads(TOKEN_FILE)[-1] != "token-2" and time.time() < deadline:
            target.written.wait(0.2)
            target.written.clear()
        assert target.uploads(TOKEN_FILE) == ["token-1", "token-2"]
        assert engine.keeper.delay() == 60  # the new one lasts an hour: asked again in a minute
    finally:
        keeper = engine.keeper
        engine.cleanup()
    assert not keeper.thread.is_alive() and engine.keeper is None
    assert (f"rm -f {TOKEN_FILE}", None) in target.calls and (f"rm -f {ENV_FILE}", None) in target.calls


def test_a_token_that_cannot_be_had_stops_the_burst(driver, monkeypatch):
    monkeypatch.setattr(fake_provider, "TOKENS", fake_provider.Tokens(("", None)))
    engine, _ = _engine(GATEWAY)
    with pytest.raises(ClaudeCodeError, match="gave no token"):
        engine.prepare()


def test_without_an_endpoint_nothing_changes(driver):
    engine, target = _engine("anthropic")
    engine.prepare()
    assert target.uploads(ENV_FILE) == ["export ANTHROPIC_API_KEY=sk-driver\n"
                                        "export ANTHROPIC_BASE_URL=https://driver.example.com\n"]
    assert target.uploads(TOKEN_FILE) == [] and engine.keeper is None
    assert "apiKeyHelper" not in json.loads(engine.hook_settings())
    engine.cleanup()


def test_the_gateway_lets_claude_code_reach_the_endpoint(driver, tmp_path):
    cfg = HarnessConfig(provider=GATEWAY, engine="claude-code", state_dir=str(tmp_path), egress_allow=("pypi.org",))
    hosts = containers.allowlist(cfg)
    assert "ai.example.com" in hosts and "driver.example.com" not in hosts
    assert "driver.example.com" in containers.allowlist(replace(cfg, provider="anthropic"))
    # The Strands engine calls the model from the driver: the endpoint is not the sandbox's to reach.
    assert "ai.example.com" not in containers.allowlist(replace(cfg, engine="strands"))
    with pytest.raises(containers.ContainerError, match="cannot hold yet"):
        containers.inject_rules(replace(cfg, egress_inject=("model",)))
    spec = job.JobSpec(name="j", path=tmp_path, prompt="x", settings={})
    assert {"host": "ai.example.com", "method": "POST", "path": "/anthropic/v1/messages*"} in job.write_rules(spec, cfg)


def test_an_endpoint_must_be_https(monkeypatch):
    monkeypatch.setattr(fake_provider.gateway, "target_endpoint",
                        lambda cfg: models.TargetEndpoint(base_url="http://ai.example.com", token=str))
    with pytest.raises(models.ProviderError, match="https"):
        models.target_endpoint(HarnessConfig(provider=GATEWAY))
    assert models.target_endpoint(HarnessConfig()) is None
