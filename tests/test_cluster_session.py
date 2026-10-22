"""`harness session` on agent-sandbox: claim or resume, register with the shared
gateway (inbound names included), serve the UI, route to it, suspend, delete.

A fake kubectl keeps the cluster's claims, sandboxes and routes in a JSON file;
what would run in the pod is stubbed, except in the test of `_exec` itself,
which runs its script here.
"""

import json
import subprocess
import sys
import textwrap
from dataclasses import dataclass

import pytest

from harness import cluster_session
from harness.config import HarnessConfig
from harness.exec_target import KubectlTarget
from harness.gateway_control import Gateway
from harness.session import SessionError

FAKE_KUBECTL = textwrap.dedent('''\
    #!{python}
    """kubectl, as much of it as cluster_session uses, over a JSON file."""
    import json, sys
    path = {state!r}
    args = sys.argv[1:]
    with open(path) as f:
        state = json.load(f)
    state["calls"].append(args)
    while args and args[0] in ("-n", "--context"):
        args = args[2:]
    verb = args[0]

    def save():
        with open(path, "w") as f:
            json.dump(state, f)

    def get(kind, name):
        return state.setdefault(kind, {{}}).get(name)

    if verb == "get" and args[1] in ("sandboxclaim", "sandbox"):
        found = get(args[1], args[2])
        print(json.dumps(found) if found else "")
    elif verb == "get" and args[1] == "sandboxclaims":
        print(json.dumps({{"items": list(state.get("sandboxclaim", {{}}).values())}}))
    elif verb == "get" and args[1] == "sandboxes":
        print(json.dumps({{"items": list(state.get("sandbox", {{}}).values())}}))
    elif verb == "get" and args[1] == "pod":
        state["pods"] = state.get("pods", 0)
        print("10.246.0.%d" % (10 + state["pods"]))
    elif verb == "create":
        claim = json.loads(sys.stdin.read())
        name = claim["metadata"]["name"]
        claim["metadata"]["uid"] = "uid-" + name
        claim["status"] = {{"sandbox": {{"name": "pool-abc12"}}}}
        state.setdefault("sandboxclaim", {{}})[name] = claim
        state.setdefault("sandbox", {{}})["pool-abc12"] = {{"metadata": {{"name": "pool-abc12"}}, "spec": {{}}}}
        print(json.dumps(claim))
    elif verb == "patch":
        mode = json.loads(args[args.index("-p") + 1])["spec"]["operatingMode"]
        get("sandbox", args[2])["spec"]["operatingMode"] = mode
        if mode == "Running":
            state["pods"] = state.get("pods", 0) + 1  # a new pod, at a new address
    elif verb == "wait":
        pass
    elif verb == "apply":
        route = json.loads(sys.stdin.read())
        state.setdefault("httproute", {{}})[route["metadata"]["name"]] = route
    elif verb == "delete":
        claim = state.get("sandboxclaim", {{}}).pop(args[2], None)
        if claim:  # the routes are the claim's
            state["httproute"] = {{k: r for k, r in state.get("httproute", {{}}).items()
                                  if r["metadata"]["ownerReferences"][0]["name"] != args[2]}}
    else:
        sys.exit("fake kubectl: " + " ".join(args))
    save()
''')


@dataclass
class Cluster:
    path: object

    @property
    def state(self) -> dict:
        return json.loads(self.path.read_text())


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    state = tmp_path / "cluster.json"
    state.write_text(json.dumps({"calls": []}))
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(FAKE_KUBECTL.format(python=sys.executable, state=str(state)))
    kubectl.chmod(0o755)
    monkeypatch.setenv("HARNESS_GATEWAY_TOKEN", "t" * 32)
    return Cluster(state), str(kubectl)


@pytest.fixture
def pod(monkeypatch):
    """What runs in the pod: recorded, and answered as a working sandbox would."""
    ran: list[dict] = []
    registered: list[tuple[str, dict]] = []
    unregistered: list[str] = []

    def fake_exec(target, argv, *, env=None, detach=False, workdir="/workspace"):
        ran.append({"argv": argv, "env": env or {}, "detach": detach, "workdir": workdir})
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(cluster_session, "_exec", fake_exec)
    monkeypatch.setattr(cluster_session, "_wait_for", lambda target, port, timeout=60: True)
    monkeypatch.setattr(cluster_session, "terminal_in", lambda run, claude=False: "token-1")
    monkeypatch.setattr(KubectlTarget, "missing_commands", lambda self, needed: [])
    monkeypatch.setattr(KubectlTarget, "run_shell", lambda self, command, **kw: subprocess.CompletedProcess(command, 0, "", ""))
    monkeypatch.setattr(Gateway, "register", lambda self, name, body: registered.append((name, body)) or {})
    monkeypatch.setattr(Gateway, "unregister", lambda self, name: unregistered.append(name) or [])
    monkeypatch.setattr("harness.containers.approved", lambda cfg: [])
    return ran, registered, unregistered


def _cfg(tmp_path, kubectl, **settings) -> HarnessConfig:
    return HarnessConfig(**{
        "target": "agent-sandbox", "pool": "coding-agent", "namespace": "agent-sandbox-system", "kubectl": kubectl,
        "gateway_control": "http://gw:8081", "session_domain": "internal.example.com",
        "session_route_parent": "network/envoy-internal/https", "state_dir": str(tmp_path / "state"),
        "provider": "llamacpp", "base_url": "http://192.0.2.11:8080", "model_id": "qwen", "egress": "allowlist",
        **settings,
    })


def test_a_session_is_claimed_registered_served_and_routed(tmp_path, cluster, pod):
    state, kubectl = cluster
    ran, registered, _ = pod
    out = cluster_session.start(_cfg(tmp_path, kubectl), "demo", terminal=True)

    claim = state.state["sandboxclaim"]["session-demo"]
    assert claim["spec"] == {"warmPoolRef": {"name": "coding-agent"}}  # no deadline: it lasts until removed
    assert claim["metadata"]["labels"]["burst-harness/session"] == "demo"

    name, body = registered[-1]
    assert name == "session-demo" and body["address"] == "10.246.0.10" and body["ttl"] == 0
    assert body["inbound"] == {"session-demo.internal.example.com": 4096, "session-demo-term.internal.example.com": 8083}
    assert "192.0.2.11:8080" in body["allow"]  # the model, from the sandbox

    serve = next(r for r in ran if r["detach"] and "opencode web" in r["argv"][-1])
    assert serve["env"]["OPENCODE_SERVER_PASSWORD"] and serve["env"]["OPENCODE_SERVER_PASSWORD"] not in serve["argv"][-1]

    routes = state.state["httproute"]
    assert sorted(routes) == ["session-demo", "session-demo-term"]
    route = routes["session-demo"]
    assert route["spec"]["hostnames"] == ["session-demo.internal.example.com"]
    assert route["spec"]["parentRefs"][0] == {"group": "gateway.networking.k8s.io", "kind": "Gateway",
                                              "name": "envoy-internal", "namespace": "network", "sectionName": "https"}
    assert route["spec"]["rules"][0]["backendRefs"] == [{"name": "burst-gateway", "port": 8080}]
    assert route["metadata"]["ownerReferences"][0] == {"apiVersion": "extensions.agents.x-k8s.io/v1beta1",
                                                       "kind": "SandboxClaim", "name": "session-demo",
                                                       "uid": "uid-session-demo"}
    assert "is up" in out and "https://session-demo.internal.example.com/" in out and "token-1" in out
    assert "https://session-demo-term.internal.example.com/shell" in out


def test_stop_suspends_and_start_resumes_at_a_new_address(tmp_path, cluster, pod):
    state, kubectl = cluster
    _, registered, unregistered = pod
    cfg = _cfg(tmp_path, kubectl)
    cluster_session.start(cfg, "demo")
    password = json.loads((tmp_path / "state" / "sessions" / "demo.json").read_text())["password"]

    assert "workspace is kept" in cluster_session.stop(cfg, "demo")
    assert unregistered == ["session-demo"]
    assert state.state["sandbox"]["pool-abc12"]["spec"]["operatingMode"] == "Suspended"
    assert cluster_session.list_sessions(cfg)[0]["status"] == "stopped"

    out = cluster_session.start(cfg, "demo")
    assert "is resumed" in out and password in out  # the same password
    assert state.state["sandbox"]["pool-abc12"]["spec"]["operatingMode"] == "Running"
    assert registered[-1][1]["address"] == "10.246.0.11"
    assert len(state.state["sandboxclaim"]) == 1
    assert cluster_session.list_sessions(cfg) == [{"name": "demo", "status": "running", "agent": "opencode",
                                                   "ui": "https://session-demo.internal.example.com/", "repo": "-"}]


def test_rm_deletes_the_claim_and_its_routes(tmp_path, cluster, pod):
    state, kubectl = cluster
    cfg = _cfg(tmp_path, kubectl)
    cluster_session.start(cfg, "demo")
    assert "deleted" in cluster_session.remove(cfg, "demo")
    assert state.state["sandboxclaim"] == {} and state.state["httproute"] == {}
    assert not (tmp_path / "state" / "sessions" / "demo.json").exists()


def test_claude_code_is_its_terminal_and_a_lifetime_is_a_deadline(tmp_path, cluster, pod):
    state, kubectl = cluster
    ran, registered, _ = pod
    out = cluster_session.start(_cfg(tmp_path, kubectl), "cc", agent="claude-code", lifetime="2h")
    assert registered[-1][1]["inbound"] == {"session-cc.internal.example.com": 8083}
    assert 7000 < registered[-1][1]["ttl"] <= 7200
    lifecycle = state.state["sandboxclaim"]["session-cc"]["spec"]["lifecycle"]
    assert lifecycle["shutdownPolicy"] == "Delete" and lifecycle["shutdownTime"].endswith("Z")
    assert any(r["argv"] == ("burst-sandbox-init",) for r in ran)
    assert "Claude Code, in a terminal" in out and "https://session-cc.internal.example.com/claude" in out


def test_what_a_cluster_session_cannot_do_is_refused_before_claiming(tmp_path, cluster, pod):
    state, kubectl = cluster
    for options, match in [({"idle": "2h"}, "--idle"), ({"port": 8080}, "--port"), ({"bind": "0.0.0.0"}, "--bind")]:
        with pytest.raises(SessionError, match=match):
            cluster_session.start(_cfg(tmp_path, kubectl), "demo", **options)
    with pytest.raises(SessionError, match="--root"):
        cluster_session.start(_cfg(tmp_path, kubectl, container_root=True), "demo")
    with pytest.raises(SessionError, match="session_domain"):
        cluster_session.start(_cfg(tmp_path, kubectl, session_domain=""), "demo")
    with pytest.raises(SessionError, match="lowercase"):
        cluster_session.start(_cfg(tmp_path, kubectl), "Demo")
    assert "sandboxclaim" not in state.state


class LocalPod(KubectlTarget):
    """A KubectlTarget whose commands run here: for testing what `_exec` sends."""

    def exec_prefix(self, *, stdin=False, tty=False):
        return []


def test_exec_passes_variables_on_stdin_never_on_the_command_line(tmp_path, monkeypatch):
    monkeypatch.setattr(cluster_session, "HOME", str(tmp_path / "home"))
    work = tmp_path / "work"
    work.mkdir()
    target = LocalPod(repo=str(work), pod="p")
    secret = "s3cr=t with spaces\nand a newline"
    proc = cluster_session._exec(target, ("sh", "-c", 'printf "%s|%s|%s" "$PASSWORD" "$HOME" "$PWD"'),
                                 env={"PASSWORD": secret}, workdir=str(work))
    assert proc.returncode == 0
    assert proc.stdout == f"{secret}|{tmp_path / 'home'}|{work}"
    assert (tmp_path / "home").is_dir()

    proc = cluster_session._exec(target, ("sh", "-c", f'sleep 0.2; echo done > {tmp_path}/flag'), detach=True,
                                 workdir=str(work))
    assert proc.returncode == 0 and not (tmp_path / "flag").exists()  # it returned without waiting
