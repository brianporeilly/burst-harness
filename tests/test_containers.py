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
    assert "--user" not in argv and not any(a.startswith("--cap-add") for a in argv)
    assert argv[-5:] == ["--network", "n-net", "--memory", "2g", "img"]


def test_log_mode_refuses_ssh_repo():
    with pytest.raises(containers.ContainerError, match="only passes HTTP"):
        containers.check_reachable(HarnessConfig(repo_url="git@github.com:o/r.git", egress="log"))
    containers.check_reachable(HarnessConfig(repo_url="http://git.lan/o/r.git", egress="log"))


def test_events_path_is_under_state_dir(tmp_path):
    cfg = HarnessConfig(state_dir=str(tmp_path))
    assert containers.events_path(cfg, "burst-t-abc") == str(tmp_path / "egress" / "burst-t-abc.jsonl")


@pytest.mark.parametrize("binary,subnets,create,route", [
    ("podman", ".Subnets", ("--internal", "--disable-dns"), ("--route", "0.0.0.0/0,10.89.3.254,500")),
    ("docker", ".IPAM.Config", ("--internal",), ()),  # no routes on docker: only fixed addresses
])
def test_fixed_network_recreates_with_its_subnet(monkeypatch, binary, subnets, create, route):
    calls = []

    def fake_run(binary, *args):
        calls.append(args)
        return "10.89.3.0/24" if args[:2] == ("network", "inspect") else ""

    monkeypatch.setattr(containers, "_run", fake_run)
    assert containers._fixed_network(binary, "n-net", route=binary == "podman") == ("10.89.3.0/24", "10.89.3.254")
    assert subnets in calls[1][-1]
    assert calls[2] == ("network", "rm", "n-net")
    assert calls[3] == ("network", "create", *create, "--subnet", "10.89.3.0/24", *route, "n-net")


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
    with pytest.raises(containers.ContainerError, match="takes: git, model"):
        containers.inject_rules(HarnessConfig(repo_url="https://github.com/o/r.git", egress_inject=("ssh",)))


def test_model_rule_swaps_placeholders_for_the_drivers_credentials(monkeypatch):
    cfg = HarnessConfig(engine="claude-code", egress="log", egress_inject=("model",))
    for name in containers.MODEL_CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    with pytest.raises(containers.ContainerError, match="needs a credential"):
        containers.inject_rules(cfg)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "real-oauth")
    (rule,) = containers.inject_rules(cfg)
    assert rule["host"] == "api.anthropic.com"
    assert {(w["header"], w["value"]) for w in rule["swap"]} == {("x-api-key", "real-key"), ("Authorization", "real-oauth")}
    assert all("placeholder" in w["placeholder"] for w in rule["swap"])
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example/anthropic")
    assert containers.inject_rules(cfg)[0]["host"] == "proxy.example"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://llm.lan:8080")
    with pytest.raises(containers.ContainerError, match="https:// on port 443"):
        containers.inject_rules(cfg)


def test_model_rule_is_refused_for_an_engine_on_the_driver(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-key")
    with pytest.raises(containers.ContainerError, match="never leaves"):
        containers.inject_rules(HarnessConfig(engine="strands", egress_inject=("model",)))


def test_allowlist_takes_extra_entries_once():
    cfg = HarnessConfig(repo_url="https://github.com/o/r.git", egress_allow=("github.com",))
    assert containers.allowlist(cfg, ("llm.lan:8080", "github.com")) == ["github.com", "llm.lan:8080"]


def test_root_gets_only_the_capabilities_to_install_packages():
    argv = containers.run_argv(HarnessConfig(image="img", container_root=True), binary="podman", name="n", slug="t")
    assert argv[argv.index("--user") + 1] == "0"
    assert "--cap-drop=ALL" in argv and "--security-opt=no-new-privileges" in argv
    added = {a.split("=", 1)[1] for a in argv if a.startswith("--cap-add=")}
    assert added == set(containers.ROOT_CAPABILITIES)
    assert not added & {"SYS_ADMIN", "NET_ADMIN", "NET_RAW", "SYS_PTRACE", "MKNOD"}


def test_root_is_refused_unless_podman_is_rootless(monkeypatch):
    cfg = HarnessConfig(container_root=True)
    monkeypatch.setattr(containers, "_run", lambda *a, **k: "false")
    with pytest.raises(containers.ContainerError, match="rootless podman"):
        containers.check_root(cfg, "podman")
    with pytest.raises(containers.ContainerError, match="rootless podman"):
        containers.check_root(cfg, "docker")
    monkeypatch.setattr(containers, "_run", lambda *a, **k: "true")
    containers.check_root(cfg, "podman")
    containers.check_root(HarnessConfig(), "docker")  # not asked for: nothing to check


def test_model_rule_is_allowed_for_a_sessions_opencode(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-key")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    (rule,) = containers.inject_rules(HarnessConfig(engine="opencode", egress_inject=("model",)))
    assert rule["host"] == "api.anthropic.com"


@pytest.mark.parametrize("binary", ["podman", "docker"])
def test_the_gateway_passes_connections_in_on_either_runtime(monkeypatch, binary):
    calls = []

    def fake_run(binary, *args, env=None):
        calls.append(args)
        return "10.89.3.0/24" if args[:2] == ("network", "inspect") else ""

    monkeypatch.setattr(containers, "_run", fake_run)
    _, _, flags = containers.start_egress(
        HarnessConfig(egress="log", gateway_image="gw"), binary=binary, name="b", slug="s",
        inbound=(("127.0.0.1", 5000, 4096),))
    gateway = next(args for args in calls if args[:2] == ("run", "--detach"))
    assert "127.0.0.1:5000:4096" in gateway and "EGRESS_INBOUND=4096=10.89.3.2:4096" in gateway
    assert ("--cap-add=NET_ADMIN" in gateway) == (binary == "podman")  # the redirect: podman only
    assert ("network", "connect", "--ip", "10.89.3.254", "b-net", "b-egress") in calls
    assert flags[flags.index("--ip") + 1] == "10.89.3.2" and containers.gateway_address(flags) == "10.89.3.254"
