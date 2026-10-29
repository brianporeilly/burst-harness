"""Credentials: one place to read them, and each run handed only what it is granted."""

import threading

from harness import credentials, git, mcp
from harness.config import HarnessConfig


def test_a_credential_comes_from_the_overlay_then_the_environment(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", " from-env ")
    monkeypatch.delenv("HARNESS_GIT_TOKEN", raising=False)
    assert credentials.get("GH_TOKEN") == "from-env" and git.token() == "from-env"
    with credentials.overlay({"HARNESS_GIT_TOKEN": "alices"}):
        assert git.token() == "alices"  # the first of HARNESS_GIT_TOKEN, GH_TOKEN, GITHUB_TOKEN
        with credentials.overlay({"GH_TOKEN": ""}):
            assert credentials.get("GH_TOKEN") == ""  # hidden
            assert credentials.get("HARNESS_GIT_TOKEN") == "alices"  # the outer overlay still applies
        seen = []
        thread = threading.Thread(target=lambda: seen.append(credentials.get("HARNESS_GIT_TOKEN")))
        thread.start(), thread.join()
        assert seen == [""]  # a plain thread does not carry the overlay; the caller's context does
    assert git.token() == "from-env"


def test_what_counts_as_a_credential():
    for name in ("KUBE_TOKEN", "CONTEXT7_API_KEY", "SLACK_WEBHOOK_URL", "AWS_ACCESS_KEY_ID", "DB_PASSWORD",
                 "SSH_AUTH_SOCK", "HARNESS_GIT_TOKEN", "HARNESS_GATEWAY_TOKEN", "my_secret"):
        assert credentials.is_secret(name), name
    for name in ("PATH", "HOME", "HARNESS_MAX_TOKENS", "HARNESS_TOKEN_COMMAND", "HARNESS_TOKEN_FILE",
                 "HARNESS_GATEWAY_TOKEN_FILE", "ANTHROPIC_BASE_URL", "AWS_PROFILE", "XAUTHORITY", "TOKENIZERS_PARALLELISM"):
        assert not credentials.is_secret(name), name


def test_a_run_gets_its_grants_and_no_other_credential(monkeypatch):
    for name, value in {"KUBE_TOKEN": "k", "SLACK_BOT_TOKEN": "s", "GH_TOKEN": "g", "ANTHROPIC_API_KEY": "a",
                        "HARNESS_TOKEN_COMMAND": "pass show x", "PLUGIN_KEY": "p", "PATH": "/bin"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(credentials.PASS_ENV, raising=False)
    env = credentials.for_run(["GH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"], {"X": "1"})
    assert env["GH_TOKEN"] == "g" and env["ANTHROPIC_API_KEY"] == "a" and "OPENAI_API_KEY" not in env
    assert "KUBE_TOKEN" not in env and "SLACK_BOT_TOKEN" not in env and "PLUGIN_KEY" not in env
    assert env["PATH"] == "/bin" and env["HARNESS_TOKEN_COMMAND"] == "pass show x" and env["X"] == "1"
    monkeypatch.setenv(credentials.PASS_ENV, "PLUGIN_KEY, OTHER")
    assert credentials.for_run([])["PLUGIN_KEY"] == "p"
    with credentials.overlay({"GH_TOKEN": "bobs"}):
        assert credentials.for_run(["GH_TOKEN"])["GH_TOKEN"] == "bobs"


def test_what_tasks_and_jobs_are_granted():
    cfg = HarnessConfig(mcp=("context7=https://mcp.context7.com/mcp",))
    assert mcp.key_var("context7") == "CONTEXT7_API_KEY"
    task = credentials.for_task(cfg)
    assert {"GH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "HARNESS_GATEWAY_TOKEN", "CONTEXT7_API_KEY"} <= set(task)
    assert "SLACK_BOT_TOKEN" not in task
    quiet = credentials.for_job(HarnessConfig(), ["KUBE_TOKEN"], ["stdout", "file:/r.md"])
    assert "KUBE_TOKEN" in quiet and "SLACK_BOT_TOKEN" not in quiet and "GH_TOKEN" not in quiet
    assert {"SLACK_BOT_TOKEN", "SLACK_WEBHOOK_URL"} <= set(credentials.for_job(HarnessConfig(), [], ["slack:#ops"]))
