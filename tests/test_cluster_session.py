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
        pod = {{"metadata": {{}}, "status": {{"podIP": "10.246.0.%d" % (10 + state["pods"]),
                                         "conditions": [{{"type": "Ready", "status": "True"}}]}}}}
        if state.get("terminating"):  # a suspended pod, in its grace period
            pod["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
        print(json.dumps(pod))
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
        else:
            state["terminating"] = True
    elif verb == "wait":
        if "--for=delete" in args:
            state["terminating"] = False
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
    sessions: list[dict] = []  # opencode's, kept in its home on the workspace

    def fake_exec(target, argv, *, env=None, detach=False, workdir="/workspace"):
        ran.append({"argv": argv, "env": env or {}, "detach": detach, "workdir": workdir})
        out = ""
        if len(argv) > 2 and "curl" in argv[2]:  # opencode's API, as _opencode_api calls it
            method, path = argv[3], argv[4]
            if method == "POST":
                sessions.append({"id": f"ses_{len(sessions) + 1}", "directory": path.split("directory=")[1],
                                 "time": {"updated": len(sessions)}, **json.loads(env["BODY"])})
                out = json.dumps(sessions[-1])
            else:
                out = json.dumps(sessions)
        return subprocess.CompletedProcess(argv, 0, out, "")

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
    assert "models.opencode.ai" in body["allow"]  # opencode's catalog of models

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
    # The UI opens on an opencode session in the workspace, created for it.
    created = next(r for r in ran if r["argv"][3:4] == ("POST",))
    assert created["argv"][4] == "/session?directory=/workspace" and json.loads(created["env"]["BODY"]) == {"title": "demo"}
    assert created["env"]["OPENCODE_SERVER_PASSWORD"] not in " ".join(created["argv"])
    assert "is up" in out and "https://session-demo.internal.example.com/L3dvcmtzcGFjZQ/session/ses_1" in out
    assert "token-1" in out
    assert "https://session-demo-term.internal.example.com/shell" in out


def test_stop_suspends_and_start_resumes_at_a_new_address(tmp_path, cluster, pod):
    state, kubectl = cluster
    ran, registered, unregistered = pod
    cfg = _cfg(tmp_path, kubectl)
    cluster_session.start(cfg, "demo")
    password = json.loads((tmp_path / "state" / "sessions" / "demo.json").read_text())["password"]

    assert "workspace is kept" in cluster_session.stop(cfg, "demo")
    assert not state.state.get("terminating")  # stop waited for the pod to go
    assert unregistered == ["session-demo"]
    assert state.state["sandbox"]["pool-abc12"]["spec"]["operatingMode"] == "Suspended"
    assert cluster_session.list_sessions(cfg)[0]["status"] == "stopped"

    out = cluster_session.start(cfg, "demo")
    assert "is resumed" in out and password in out  # the same password
    assert "is running" in cluster_session.start(cfg, "demo")  # started again while running
    assert "/session/ses_1" in out  # and the same opencode session
    assert sum(1 for r in ran if r["argv"][3:4] == ("POST",)) == 1
    assert state.state["sandbox"]["pool-abc12"]["spec"]["operatingMode"] == "Running"
    assert registered[-1][1]["address"] == "10.246.0.11"
    assert len(state.state["sandboxclaim"]) == 1
    assert cluster_session.list_sessions(cfg) == [{"name": "demo", "status": "running", "agent": "opencode",
                                                   "ui": "https://session-demo.internal.example.com/L3dvcmtzcGFjZQ/"
                                                         "session/ses_1", "repo": "-"}]


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
    assert "models.opencode.ai" not in registered[-1][1]["allow"]
    assert not any("curl" in r["argv"][-1] for r in ran if len(r["argv"]) > 2)
    assert 7000 < registered[-1][1]["ttl"] <= 7200
    lifecycle = state.state["sandboxclaim"]["session-cc"]["spec"]["lifecycle"]
    assert lifecycle["shutdownPolicy"] == "Delete" and lifecycle["shutdownTime"].endswith("Z")
    assert any(r["argv"] == ("burst-sandbox-init",) for r in ran)
    assert "Claude Code, in a terminal" in out and "https://session-cc.internal.example.com/claude" in out


def test_what_a_cluster_session_cannot_do_is_refused_before_claiming(tmp_path, cluster, pod):
    state, kubectl = cluster
    for options, match in [({"port": 8080}, "--port"), ({"bind": "0.0.0.0"}, "--bind")]:
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


def test_opencode_api_runs_curl_in_the_pod_with_the_password_on_stdin(tmp_path, monkeypatch):
    """What `_opencode_api` sends, through `_exec`, to a server standing in for opencode's."""
    import base64
    import http.server
    import threading

    seen: list[dict] = []

    class Opencode(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            seen.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                         "body": self.rfile.read(length).decode() if length else None})
            data = json.dumps({"id": "ses_9"} if self.command == "POST" else []).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        do_GET = do_POST = _answer

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Opencode)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(cluster_session, "UI_PORT", server.server_address[1])
    monkeypatch.setattr(cluster_session, "HOME", str(tmp_path / "home"))
    try:
        api = cluster_session._opencode_api(LocalPod(repo=str(tmp_path), pod="p"), "pw-123")
        assert cluster_session.open_in(api, "/workspace/repo", "demo", notes := []) == \
            "L3dvcmtzcGFjZS9yZXBv/session/ses_9" and notes == []
    finally:
        server.shutdown()
    auth = "Basic " + base64.b64encode(b"opencode:pw-123").decode()
    assert seen == [
        {"method": "GET", "path": "/session?directory=/workspace/repo&roots=true", "auth": auth, "body": None},
        {"method": "POST", "path": "/session?directory=/workspace/repo", "auth": auth, "body": '{"title": "demo"}'},
    ]


def test_an_opencode_that_will_not_answer_leaves_a_note_not_a_failure(tmp_path, cluster, pod, monkeypatch):
    _, kubectl = cluster
    answering = cluster_session._exec

    def refusing(target, argv, **kw):
        if len(argv) > 2 and "curl" in argv[2]:
            return subprocess.CompletedProcess(argv, 22, "", "curl: (22) The requested URL returned error: 500")
        return answering(target, argv, **kw)

    monkeypatch.setattr(cluster_session, "_exec", refusing)
    out = cluster_session.start(_cfg(tmp_path, kubectl), "demo")
    assert "Could not open an opencode session in /workspace (curl: (22)" in out
    assert "https://session-demo.internal.example.com/\n" in out + "\n"


def test_start_waits_for_a_pod_still_going_rather_than_using_it(tmp_path, cluster, pod):
    """The Sandbox says Ready while its suspended pod is in its grace period: a start
    then must not register, or serve on, the pod that is going."""
    state, kubectl = cluster
    _, registered, _ = pod
    cfg = _cfg(tmp_path, kubectl)
    cluster_session.start(cfg, "demo")
    cluster_session._set_mode(cfg, "pool-abc12", "Suspended")  # a stop that has not finished
    assert state.state["terminating"]

    cluster_session.start(cfg, "demo")
    calls = state.state["calls"]
    waited = next(i for i, call in enumerate(calls) if "--for=delete" in call)
    resumed = next(i for i, call in enumerate(calls) if "patch" in call and "Running" in call[-1])
    assert waited < resumed
    assert registered[-1][1]["address"] == "10.246.0.11"


def test_idle_is_registered_and_kept_for_the_next_start(tmp_path, cluster, pod):
    _, kubectl = cluster
    _, registered, _ = pod
    cfg = _cfg(tmp_path, kubectl)
    out = cluster_session.start(cfg, "demo", idle="2h")
    assert registered[-1][1]["idle"] == 7200 and "suspended: after 7200s" in out
    cluster_session.start(cfg, "demo")  # as last started
    assert registered[-1][1]["idle"] == 7200
    cluster_session.start(cfg, "demo", idle="0")  # 0: never
    assert "idle" not in registered[-1][1]


def test_reap_suspends_the_sessions_idle_for_too_long_and_nothing_else(tmp_path, cluster, pod, monkeypatch):
    state, kubectl = cluster
    _, _, unregistered = pod
    cfg = _cfg(tmp_path, kubectl)
    cluster_session.start(cfg, "demo", idle="1h")
    listing = [
        {"name": "session-demo", "active": 1000.0, "idle": 3600},  # quiet for 3601s
        {"name": "session-busy", "active": 4000.0, "idle": 3600},  # not long enough
        {"name": "session-forever", "active": 0.0, "idle": None},  # no --idle
        {"name": "burst-job-1", "active": 0.0, "idle": 60},  # not a session
    ]
    monkeypatch.setattr(Gateway, "sandboxes", lambda self: listing)
    out = cluster_session.reap(cfg, now=4601.0)
    assert "Stopped session demo" in out and "idle 3601s, --idle 3600s" in out
    assert unregistered == ["session-demo"]
    assert state.state["sandbox"]["pool-abc12"]["spec"]["operatingMode"] == "Suspended"

    listing[0]["active"] = 4600.0
    assert cluster_session.reap(cfg, now=4601.0) == "No session has been idle too long."


def test_reap_leaves_a_claim_that_is_not_a_session_of_that_name(tmp_path, cluster, pod, monkeypatch):
    state, kubectl = cluster
    _, _, unregistered = pod
    cfg = _cfg(tmp_path, kubectl)
    cluster_session.start(cfg, "demo")
    data = state.state
    data["sandboxclaim"]["session-demo"]["metadata"]["labels"] = {}  # someone else's claim
    state.path.write_text(json.dumps(data))
    monkeypatch.setattr(Gateway, "sandboxes", lambda self: [{"name": "session-demo", "active": 0.0, "idle": 60}])
    assert cluster_session.reap(cfg, now=1000.0) == "No session has been idle too long." and unregistered == []
