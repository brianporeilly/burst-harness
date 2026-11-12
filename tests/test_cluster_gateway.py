"""The driver's side of a cluster's shared gateway: a claimed sandbox is registered
with it, its traffic held to what it was registered with, and its events saved on
release.

A fake kubectl stands in for the cluster (the "pod" is this machine, at
127.0.0.2), and the real gateway runs in shared mode on loopback.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from harness import agent_sandbox
from harness.config import HarnessConfig
from harness.gateway_control import Gateway, GatewayError, registration

TOKEN = "k" * 32
GATEWAY = Path(__file__).resolve().parent.parent / "gateway" / "burst_gateway.py"
linux_only = pytest.mark.skipif(sys.platform != "linux", reason="needs 127.0.0.0/8 routed to loopback")
needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")

FAKE_KUBECTL = textwrap.dedent('''\
    #!{python}
    """kubectl, as much of it as agent_sandbox uses. Every call is logged."""
    import json, subprocess, sys
    args = sys.argv[1:]
    with open({log!r}, "a") as log:
        log.write(json.dumps(args) + "\\n")
    while args and args[0] in ("-n", "--context"):
        args = args[2:]
    verb = args[0]
    if verb == "create":
        sys.stdin.read()
        print("pool-abc12")
    elif verb == "wait":
        pass
    elif verb == "get" and args[1] == "sandboxclaim" and "sandbox.name" in args[-1]:
        print("pod-abc12")
    elif verb == "get" and args[1] == "sandboxclaim":
        print("2099-01-01T00:00:00Z")
    elif verb == "get" and args[1] == "pod":
        print({address!r})
    elif verb == "exec":
        sys.exit(subprocess.run(args[args.index("--") + 1:]).returncode)
    elif verb == "delete":
        pass
    else:
        sys.exit("fake kubectl: " + " ".join(args))
''')


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def kubectl(tmp_path):
    log = tmp_path / "kubectl.log"
    path = tmp_path / "kubectl"
    path.write_text(FAKE_KUBECTL.format(python=sys.executable, log=str(log), address="127.0.0.2"))
    path.chmod(0o755)
    return path, log


@pytest.fixture
def gateway(tmp_path):
    """The real gateway, shared, on loopback: (proxy port, control URL)."""
    if shutil.which("openssl") is None:
        pytest.skip("needs the openssl CLI")
    ca = tmp_path / "ca"
    ca.mkdir()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", str(ca / "tls.key"), "-out", str(ca / "tls.crt"), "-days", "1", "-subj", "/CN=t",
                    "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign"],
                   check=True, capture_output=True)
    listen, control = _free_port(), _free_port()
    proc = subprocess.Popen([sys.executable, str(GATEWAY)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={
        **os.environ, "EGRESS_DNS": "0", "EGRESS_LISTEN": str(listen), "EGRESS_CONTROL": str(control),
        "EGRESS_CONTROL_TOKEN": TOKEN, "EGRESS_CA_CERT": str(ca / "tls.crt"), "EGRESS_CA_KEY": str(ca / "tls.key"),
        "EGRESS_CA_DIR": str(tmp_path / "hosts"),
    })
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", control), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    try:
        yield listen, f"http://127.0.0.1:{control}"
    finally:
        proc.terminate()
        proc.communicate(timeout=20)


@pytest.fixture
def origin():
    """A plain-HTTP server for the sandbox to reach: (port, the paths it was asked for)."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_port, seen
    server.shutdown()


def _cfg(tmp_path, kubectl_path, control_url="", **settings) -> HarnessConfig:
    return HarnessConfig(target="agent-sandbox", pool="pool", namespace="sandboxes", kubectl=str(kubectl_path),
                         gateway_control=control_url, state_dir=str(tmp_path / "state"), **settings)


def _through(proxy_port: int, url: str) -> int:
    """GET url through the gateway's proxy, from the "pod's" address."""
    with socket.create_connection(("127.0.0.1", proxy_port), source_address=("127.0.0.2", 0), timeout=10) as conn:
        conn.sendall(f"GET {url} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        data = b""
        while chunk := conn.recv(65536):
            data += chunk
    return int(data.split(b" ", 2)[1])


# --- settings ---


def test_the_token_comes_from_the_environment_or_a_file(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_GATEWAY_TOKEN", raising=False)
    assert Gateway.from_config(HarnessConfig()) is None
    with pytest.raises(GatewayError, match="no token"):
        Gateway.from_config(HarnessConfig(gateway_control="http://gw:8081"))
    (tmp_path / "token").write_text(" from-file \n")
    gateway = Gateway.from_config(HarnessConfig(gateway_control="http://gw:8081/", gateway_token_file=str(tmp_path / "token")))
    assert gateway.url == "http://gw:8081" and gateway.token == "from-file"
    assert "from-file" not in repr(gateway)
    monkeypatch.setenv("HARNESS_GATEWAY_TOKEN", "from-env")
    assert Gateway.from_config(HarnessConfig(gateway_control="http://gw:8081")).token == "from-env"


def test_registration_is_what_a_podman_gateway_would_be_given(monkeypatch):
    monkeypatch.setattr("harness.containers.approved", lambda cfg: [])
    body, summary = registration(
        HarnessConfig(egress="allowlist", egress_allow=("pypi.org",)), address="10.42.0.7", ttl=600,
        allow=("10.0.0.5:6443",), inject=({"host": "10.0.0.5", "port": 6443, "headers": {"Authorization": "Bearer s"}},),
        read_only=({"host": "*", "method": "POST", "path": "*/git-upload-pack"},),
        upstream_ca=({"host": "10.0.0.5", "ca": "-----BEGIN CERTIFICATE-----"},),
    )
    assert body["address"] == "10.42.0.7" and body["ttl"] == 600 and body["mode"] == "allowlist"
    assert body["allow"] == ["pypi.org", "10.0.0.5:6443"] and body["readonly"] is True
    assert body["inject"][0]["host"] == "10.0.0.5" and body["upstream_ca"][0]["host"] == "10.0.0.5"
    assert "Bearer s" not in summary and "read-only except POST *" in summary

    # "open" behind a gateway is recorded, not unrecorded.
    body, summary = registration(HarnessConfig(egress="open"), address="10.42.0.7", ttl=60)
    assert body == {"address": "10.42.0.7", "ttl": 60, "mode": "log"} and "recorded" in summary


def test_credentials_without_a_gateway_are_refused_before_claiming(tmp_path, kubectl):
    path, log = kubectl
    with pytest.raises(agent_sandbox.AgentSandboxError, match="gateway_control"):
        agent_sandbox.acquire(_cfg(tmp_path, path), repo="/workspace", slug="t", branch="",
                              read_only=())
    assert not log.exists()


# --- end to end ---


@linux_only
@needs_openssl
def test_a_claimed_sandbox_is_registered_held_to_its_allowlist_and_unregistered(tmp_path, kubectl, gateway, origin,
                                                                               monkeypatch, capsys):
    monkeypatch.setenv("HARNESS_GATEWAY_TOKEN", TOKEN)
    path, log = kubectl
    proxy_port, control = gateway
    origin_port, seen = origin
    cfg = _cfg(tmp_path, path, control, egress="allowlist", egress_allow=())
    monkeypatch.setattr("harness.containers.approved", lambda cfg: [])

    target = agent_sandbox.acquire(cfg, repo="/workspace", slug="job-x", branch="",
                                   allow=(f"127.0.0.1:{origin_port}",))
    assert target.gateway is not None and target.events.endswith("egress/pool-abc12.jsonl")
    # For the orchestrator, which shows what the gateway refuses this sandbox and approves it there.
    said = [line for line in capsys.readouterr().out.splitlines() if line.startswith("HARNESS_SANDBOX ")]
    assert [json.loads(line.split(" ", 1)[1]) for line in said] == [
        {"claim": "pool-abc12", "mode": "allowlist", "events": target.events}]
    listing = target.gateway._call("GET", "/sandboxes")["sandboxes"]
    assert [(s["name"], s["address"]) for s in listing] == [("pool-abc12", "127.0.0.2")]

    assert _through(proxy_port, f"http://127.0.0.1:{origin_port}/allowed") == 200
    assert _through(proxy_port, f"http://127.0.0.1:{_free_port()}/elsewhere") == 403
    assert seen == ["/allowed"]
    time.sleep(0.2)  # a request is recorded once both directions have closed

    message = target.release()
    assert "Released pool-abc12." in message and "Egress events:" in message
    events = [json.loads(line) for line in Path(target.events).read_text().splitlines()]
    assert [(e["kind"], e["verdict"]) for e in events] == [
        ("sandbox", "register"), ("http", "allow"), ("http", "close"), ("http", "deny")]
    assert target.gateway._call("GET", "/sandboxes")["sandboxes"] == []
    assert _through(proxy_port, f"http://127.0.0.1:{origin_port}/after") == 403
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert ["get", "pod", "pod-abc12", "-o", "jsonpath={.status.podIP}"] in [c[2:] for c in calls]
    assert any(c[2:4] == ["delete", "sandboxclaim"] for c in calls)


@linux_only
@needs_openssl
def test_a_refused_registration_releases_the_claim(tmp_path, kubectl, gateway, monkeypatch):
    monkeypatch.setenv("HARNESS_GATEWAY_TOKEN", "wrong-token-but-long-enough")
    path, log = kubectl
    with pytest.raises(GatewayError, match="401"):
        agent_sandbox.acquire(_cfg(tmp_path, path, gateway[1]), repo="/workspace", slug="t", branch="")
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[-1][2:4] == ["delete", "sandboxclaim"]


def test_an_attached_claim_is_registered_until_its_deadline(tmp_path, kubectl, monkeypatch):
    path, _ = kubectl
    registered = {}
    monkeypatch.setenv("HARNESS_GATEWAY_TOKEN", TOKEN)
    monkeypatch.setattr(Gateway, "register", lambda self, name, body: registered.update(name=name, body=body))
    cfg = _cfg(tmp_path, path, "http://gw:8081", claim_name="pool-abc12")
    target = agent_sandbox.acquire(cfg, repo="/workspace", slug="t", branch="")
    assert registered["name"] == "pool-abc12" and registered["body"]["address"] == "127.0.0.2"
    assert registered["body"]["ttl"] > 10 * 365 * 86400  # the fake claim's deadline is in 2099
    assert target.release() == ""  # not ours to release, or to unregister


def test_a_kubectl_url_reaches_the_gateway_by_port_forward(tmp_path, monkeypatch):
    from harness import gateway_control

    monkeypatch.setattr(gateway_control, "_FORWARDS", {})
    log = tmp_path / "pf.log"
    fake = tmp_path / "kubectl"
    fake.write_text(f"#!{sys.executable}\nimport sys, time\nopen({str(log)!r}, 'w').write(' '.join(sys.argv[1:]))\n"
                    "print('Forwarding from 127.0.0.1:41234 -> 8081', flush=True)\ntime.sleep(30)\n")
    fake.chmod(0o755)
    monkeypatch.setenv("HARNESS_GATEWAY_TOKEN", TOKEN)
    cfg = HarnessConfig(kubectl=str(fake), kube_context="home",
                        gateway_control="kubectl://agent-sandbox-system/burst-gateway:8081")
    assert Gateway.from_config(cfg).url == "http://127.0.0.1:41234"
    assert log.read_text() == "--context home -n agent-sandbox-system port-forward svc/burst-gateway :8081"
    assert Gateway.from_config(cfg).url == "http://127.0.0.1:41234"  # the same forward, not a second one

    with pytest.raises(GatewayError, match="kubectl://NAMESPACE/SERVICE:PORT"):
        Gateway.from_config(HarnessConfig(gateway_control="kubectl://burst-gateway"))
    fake.write_text(f"#!{sys.executable}\nimport sys\nsys.exit('error: services \"nope\" not found')\n")
    with pytest.raises(GatewayError, match="not found"):
        Gateway.from_config(HarnessConfig(kubectl=str(fake), gateway_control="kubectl://ns/nope:8081"))
