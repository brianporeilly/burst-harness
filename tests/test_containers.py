import pytest

from harness import containers
from harness.config import HarnessConfig


def test_allowlist_adds_repo_and_base_url_hosts(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example:8443/anthropic")
    cfg = HarnessConfig(repo_url="https://git.example.com/o/r.git", egress_allow=("api.anthropic.com",))
    assert containers.allowlist(cfg) == ["api.anthropic.com", "git.example.com", "gateway.example"]


def test_allowlist_refuses_ssh_repo():
    with pytest.raises(containers.ContainerError, match="only passes HTTPS"):
        containers.allowlist(HarnessConfig(repo_url="git@github.com:o/r.git"))


def test_run_argv_hardening_and_network():
    argv = containers.run_argv(
        HarnessConfig(image="img", container_args="--memory 2g"), binary="podman", name="n", slug="t",
        network_args=["--network", "n-net"],
    )
    assert argv[:5] == ["podman", "run", "--detach", "--name", "n"]
    assert "--cap-drop=ALL" in argv and "--security-opt=no-new-privileges" in argv
    assert argv[-5:] == ["--network", "n-net", "--memory", "2g", "img"]


def test_log_mode_refuses_ssh_repo():
    with pytest.raises(containers.ContainerError, match="only passes HTTP"):
        containers.check_reachable(HarnessConfig(repo_url="git@github.com:o/r.git", egress="log"))
    containers.check_reachable(HarnessConfig(repo_url="http://git.lan/o/r.git", egress="log"))


def test_events_path_is_under_state_dir(tmp_path):
    cfg = HarnessConfig(state_dir=str(tmp_path))
    assert containers.events_path(cfg, "burst-t-abc") == str(tmp_path / "egress" / "burst-t-abc.jsonl")


def test_routed_network_recreates_with_a_route_to_its_gateway(monkeypatch):
    calls = []

    def fake_run(binary, *args):
        calls.append(args)
        return "10.89.3.0/24" if args[:2] == ("network", "inspect") else ""

    monkeypatch.setattr(containers, "_run", fake_run)
    assert containers._routed_network("podman", "n-net") == ("10.89.3.0/24", "10.89.3.254")
    assert calls[2] == ("network", "rm", "n-net")
    assert calls[3] == ("network", "create", "--internal", "--disable-dns",
                        "--subnet", "10.89.3.0/24", "--route", "0.0.0.0/0,10.89.3.254,500", "n-net")


def test_inject_rules_scope_the_token_to_the_repo(monkeypatch):
    monkeypatch.setenv("HARNESS_GIT_TOKEN", "sekret")
    cfg = HarnessConfig(repo_url="https://github.com/o/r.git", egress="log", egress_inject=("git",))
    rules = containers.inject_rules(cfg)
    assert [(r["host"], r["path"]) for r in rules] == [("github.com", "/o/r.git/*"), ("github.com", "/o/r/*")]
    assert rules[0]["headers"]["Authorization"].startswith("Basic ")
    assert containers.inject_rules(HarnessConfig(repo_url="https://github.com/o/r.git")) == []


def test_inject_needs_a_token_and_an_https_repo(monkeypatch):
    with pytest.raises(containers.ContainerError, match="needs a token"):
        containers.inject_rules(HarnessConfig(repo_url="https://github.com/o/r.git", egress_inject=("git",)))
    monkeypatch.setenv("HARNESS_GIT_TOKEN", "sekret")
    with pytest.raises(containers.ContainerError, match="https://"):
        containers.inject_rules(HarnessConfig(repo_url="git@github.com:o/r.git", egress_inject=("git",)))
    with pytest.raises(containers.ContainerError, match="takes: git"):
        containers.inject_rules(HarnessConfig(repo_url="https://github.com/o/r.git", egress_inject=("model",)))
