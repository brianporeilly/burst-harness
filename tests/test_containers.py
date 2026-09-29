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
