"""A shared gateway: many sandboxes, told apart by address, registered over the control API.

The end-to-end tests stand two "pods" up as two loopback addresses (127.0.0.2
and 127.0.0.3), which Linux routes without any setup.
"""

import asyncio
import importlib.util
import json
import shutil
import ssl
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "burst_gateway_shared", Path(__file__).resolve().parent.parent / "gateway" / "burst_gateway.py"
)
gw = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gw  # dataclasses look the module up while it loads
_spec.loader.exec_module(gw)

TOKEN = "t" * 32
linux_only = pytest.mark.skipif(sys.platform != "linux", reason="needs 127.0.0.0/8 routed to loopback")
needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")


@pytest.fixture
def shared(monkeypatch):
    monkeypatch.setattr(gw, "SINGLE", None)
    monkeypatch.setattr(gw, "SANDBOXES", {})
    monkeypatch.setattr(gw, "CONTROL_TOKEN", TOKEN)
    monkeypatch.setattr(gw, "CERTIFICATES", None)
    monkeypatch.setattr(gw, "_LISTENERS", {})
    monkeypatch.setattr(gw, "_LISTENER_LOCK", asyncio.Lock())
    return gw


# --- registration ---


def test_a_registration_is_checked(shared):
    sandbox = gw.Sandbox.register("burst-a", {"address": "10.42.1.7", "allow": ["github.com", "llm.lan:8080"],
                                              "readonly": True, "ttl": 60}, now=1000)
    assert sandbox.address == "10.42.1.7" and sandbox.expires == 1060 and sandbox.readonly
    assert sandbox.permitted("github.com", 443) and sandbox.permitted("llm.lan", 8080)
    assert not sandbox.permitted("llm.lan", 443)
    assert gw.Sandbox.register("b", {"address": "10.42.1.8"}, now=1000).expires == 0
    for body, match in [
        ({"address": "pod-a"}, "IP address"),
        ({"address": "10.42.1.7", "alow": []}, "unknown registration keys: alow"),
        ({"address": "10.42.1.7", "ttl": "60"}, "ttl"),
        ({"address": "10.42.1.7", "allow": "github.com"}, "allow must be a list"),
        ({"address": "10.42.1.7", "inject": [{"host": "*.x.com"}]}, "exact host"),
        ({"address": "10.42.1.7", "approvals": [{"id": "x"}]}, "approvals"),
        ([], "JSON object"),
    ]:
        with pytest.raises(gw.ConfigError, match=match):
            gw.Sandbox.register("a", body, now=0)


def test_readonly_must_be_true_not_truthy(shared):
    assert not gw.Sandbox.register("a", {"address": "10.0.0.1", "readonly": "no"}, now=0).readonly


def test_control_registers_lists_reads_and_unregisters(shared, capsys):
    status, body = gw.control("PUT", "/sandboxes/burst-a", {}, {"address": "10.42.1.7", "allow": ["a.com"]}, 0)
    assert status == "200 OK" and body["address"] == "10.42.1.7"
    assert gw.sandbox_for("10.42.1.7") is gw.SANDBOXES["burst-a"]
    assert gw.sandbox_for("10.42.1.8") is None

    sandbox = gw.SANDBOXES["burst-a"]
    for n in range(3):
        gw.emit("connect", "allow", sandbox=sandbox, id=f"x-{n}", host="a.com", port=443)
    # Event 1 is the registration itself.
    status, body = gw.control("GET", "/sandboxes/burst-a/events", {"after": ["2"]}, None, 0)
    assert [e["id"] for e in body["events"]] == ["x-1", "x-2"] and body["next"] == 4 and body["dropped"] == 0
    assert all(e["sandbox"] == "burst-a" for e in body["events"])

    status, body = gw.control("GET", "/sandboxes", {}, None, 0)
    assert body == {"sandboxes": [{"name": "burst-a", "address": "10.42.1.7", "expires": None, "events": 4}]}
    assert "inject" not in json.dumps(body)

    status, body = gw.control("DELETE", "/sandboxes/burst-a", {"after": ["4"]}, None, 0)
    assert status == "200 OK" and body["events"] == []
    assert gw.sandbox_for("10.42.1.7") is None
    assert gw.control("DELETE", "/sandboxes/burst-a", {}, None, 0)[0] == "404 Not Found"
    assert gw.control("GET", "/sandboxes/burst-a/events", {}, None, 0)[0] == "404 Not Found"
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["verdict"] for e in events if e["kind"] == "sandbox"] == ["register", "unregister"]


def test_control_refuses_what_it_cannot_use(shared):
    assert gw.control("PUT", "/sandboxes/Bad_Name", {}, {"address": "10.0.0.1"}, 0)[0] == "400 Bad Request"
    status, body = gw.control("PUT", "/sandboxes/a", {}, {"address": "10.0.0.1", "ports": ["x"]}, 0)
    assert status == "400 Bad Request" and "ports" in body["message"]
    assert gw.control("POST", "/sandboxes/a", {}, None, 0)[0] == "405 Method Not Allowed"
    assert gw.control("GET", "/elsewhere", {}, None, 0)[0] == "404 Not Found"
    assert gw.control("GET", "/sandboxes/a/events", {"after": ["x"]}, None, 0)[0] == "400 Bad Request"
    assert gw.SANDBOXES == {}


def test_an_address_belongs_to_its_latest_registration(shared, capsys):
    gw.control("PUT", "/sandboxes/old", {}, {"address": "10.42.1.7"}, 0)
    gw.control("PUT", "/sandboxes/new", {}, {"address": "10.42.1.7"}, 0)
    assert list(gw.SANDBOXES) == ["new"] and gw.sandbox_for("10.42.1.7").name == "new"
    assert '"verdict": "replace"' in capsys.readouterr().out


def test_a_lapsed_registration_lets_nothing_through_and_is_forgotten(shared, monkeypatch, capsys):
    monkeypatch.setattr(gw.time, "time", lambda: 1000.0)
    gw.control("PUT", "/sandboxes/a", {}, {"address": "10.42.1.7", "ttl": 10}, 1000.0)
    assert gw.sandbox_for("10.42.1.7") is not None
    monkeypatch.setattr(gw.time, "time", lambda: 1010.0)
    assert gw.sandbox_for("10.42.1.7") is None and gw.SANDBOXES == {}
    assert '"verdict": "expire"' in capsys.readouterr().out


def test_the_events_kept_are_bounded_and_dropping_is_reported(shared, monkeypatch):
    monkeypatch.setattr(gw, "EVENTS_KEPT", 3)
    sandbox = gw.Sandbox.register("a", {"address": "10.0.0.1"}, now=0)
    for n in range(5):
        sandbox.record({"id": n})
    got = sandbox.events_after(0)
    assert [e["id"] for e in got["events"]] == [2, 3, 4] and got["dropped"] == 2 and got["next"] == 5
    assert sandbox.events_after(5) == {"events": [], "next": 5, "dropped": 0}


def test_the_token_is_a_bearer_token(shared):
    assert gw.authorized([b"PUT / HTTP/1.1\r\n", f"Authorization: Bearer {TOKEN}\r\n".encode()])
    assert not gw.authorized([b"PUT / HTTP/1.1\r\n", b"Authorization: Bearer nope\r\n"])
    assert not gw.authorized([b"PUT / HTTP/1.1\r\n", f"Authorization: Basic {TOKEN}\r\n".encode()])
    assert not gw.authorized([b"PUT / HTTP/1.1\r\n"])


def test_a_single_gateway_is_every_peers_sandbox(monkeypatch):
    single = gw.Sandbox(name="burst-x")
    monkeypatch.setattr(gw, "SINGLE", single)
    assert gw.sandbox_for("10.0.0.1") is single and gw.sandbox_for("10.0.0.2") is single


# --- end to end ---


async def _serve(handler, host="127.0.0.1", **kwargs):
    server = await asyncio.start_server(handler, host, 0, **kwargs)
    return server, server.sockets[0].getsockname()[1]


async def _upstream(reader, writer):
    """An origin that says which method and path reached it."""
    head = await gw.read_head(reader)
    method, path = head[0].decode().split(" ")[:2]
    body = json.dumps({"method": method, "path": path}).encode()
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
    await writer.drain()
    writer.close()


async def _http(port, request: bytes, *, source="127.0.0.1") -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port, local_addr=(source, 0))
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    return int(data.split(b" ", 2)[1]), data.split(b"\r\n\r\n", 1)[1]


async def _control(port, method, path, body=None, token=TOKEN):
    payload = json.dumps(body).encode() if body is not None else b""
    head = f"{method} {path} HTTP/1.1\r\nHost: gw\r\nContent-Length: {len(payload)}\r\n"
    if token:
        head += f"Authorization: Bearer {token}\r\n"
    status, data = await _http(port, head.encode() + b"\r\n" + payload)
    return status, json.loads(data)


@linux_only
def test_two_sandboxes_one_gateway(shared):
    async def scenario():
        origin, origin_port = await _serve(_upstream)
        proxy, proxy_port = await _serve(gw.handle)
        control, control_port = await _serve(gw.handle_control)
        async with origin, proxy, control:
            assert (await _control(control_port, "GET", "/healthz", token=""))[0] == 200
            assert (await _control(control_port, "GET", "/sandboxes", token="wrong"))[0] == 401
            assert (await _control(control_port, "PUT", "/sandboxes/a", {
                "address": "127.0.0.2", "allow": [f"127.0.0.1:{origin_port}"]}))[0] == 200
            assert (await _control(control_port, "PUT", "/sandboxes/b", {"address": "127.0.0.3", "allow": []}))[0] == 200

            get = f"GET http://127.0.0.1:{origin_port}/docs?q=1 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode()
            status, body = await _http(proxy_port, get, source="127.0.0.2")
            assert status == 200 and json.loads(body) == {"method": "GET", "path": "/docs?q=1"}
            status, body = await _http(proxy_port, get, source="127.0.0.3")
            assert status == 403 and json.loads(body)["error"] == "egress_denied"
            status, body = await _http(proxy_port, get, source="127.0.0.4")
            assert status == 403 and json.loads(body)["error"] == "unregistered"

            await asyncio.sleep(0.2)
            _, a = await _control(control_port, "GET", "/sandboxes/a/events")
            assert [(e["kind"], e["verdict"]) for e in a["events"]] == [("sandbox", "register"), ("http", "allow"),
                                                                       ("http", "close")]
            assert a["events"][1]["path"] == "/docs" and a["events"][2]["bytes_down"] > 0
            _, b = await _control(control_port, "DELETE", "/sandboxes/b")
            assert [e["verdict"] for e in b["events"]] == ["register", "deny"]
            status, _ = await _http(proxy_port, get, source="127.0.0.3")
            assert status == 403

    asyncio.run(scenario())


@linux_only
@needs_openssl
def test_a_read_only_sandbox_beside_one_that_may_write(shared, tmp_path):
    """TLS for both is terminated with the CA from files; only the read-only one is held to reads."""
    ca = gw.Certificates(tmp_path / "ca")  # stands in for cert-manager's CA
    gw.CERTIFICATES = gw.Certificates(tmp_path / "hosts", ca=(ca.ca_crt, ca.ca_key))
    origin_tls = gw.Certificates(tmp_path / "origin")  # the origin's own CA, not a public one

    async def scenario():
        origin, origin_port = await _serve(_upstream, ssl=origin_tls.context("127.0.0.1"))
        proxy, proxy_port = await _serve(gw.handle)
        async with origin, proxy:
            upstream_ca = [{"host": "127.0.0.1", "ca": origin_tls.ca_crt.read_text()}]
            for name, address, readonly in (("ro", "127.0.0.2", True), ("rw", "127.0.0.3", False)):
                status, body = gw.control("PUT", f"/sandboxes/{name}", {}, {
                    "address": address, "allow": [f"127.0.0.1:{origin_port}"], "readonly": readonly,
                    "upstream_ca": upstream_ca,
                    # The read-write sandbox's TLS is only terminated because it has a credential to add.
                    "inject": [] if readonly else [{"host": "127.0.0.1", "port": origin_port,
                                                    "headers": {"Authorization": "Bearer s"}}],
                }, 0)
                assert status == "200 OK", body

            async def through(source, method):
                reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port, local_addr=(source, 0))
                writer.write(f"CONNECT 127.0.0.1:{origin_port} HTTP/1.1\r\n\r\n".encode())
                assert b" 200 " in await reader.readuntil(b"\r\n\r\n")
                await writer.start_tls(ssl.create_default_context(cafile=str(ca.ca_crt)), server_hostname="127.0.0.1")
                writer.write(f"{method} /x HTTP/1.1\r\nHost: 127.0.0.1:{origin_port}\r\nContent-Length: 0\r\n\r\n".encode())
                data = await asyncio.wait_for(reader.read(), 10)
                writer.close()
                return int(data.split(b" ", 2)[1]), data.split(b"\r\n\r\n", 1)[1]

            assert (await through("127.0.0.2", "GET"))[0] == 200
            status, body = await through("127.0.0.2", "POST")
            assert status == 403 and json.loads(body)["error"] == "read_only"
            status, body = await through("127.0.0.3", "POST")
            assert status == 200 and json.loads(body)["method"] == "POST"
            await asyncio.sleep(0.2)  # a request is recorded once both directions have closed
            requests = [e for e in gw.SANDBOXES["rw"].events_after(0)["events"] if e["kind"] == "request"]
            assert requests[0]["injected"] == ["Authorization"] and requests[0]["status"] == 200

    asyncio.run(scenario())


# --- start-up ---

GATEWAY = Path(__file__).resolve().parent.parent / "gateway" / "burst_gateway.py"


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _start(env: dict[str, str]):
    import os
    import subprocess

    return subprocess.Popen([sys.executable, str(GATEWAY)], env={**os.environ, "EGRESS_DNS": "0", **env},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_listening(port: int, proc) -> None:
    import socket
    import time

    for _ in range(100):
        if proc.poll() is not None:
            raise AssertionError(proc.stderr.read())
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("gateway did not start")


def test_a_shared_gateway_needs_a_token_and_a_ca(tmp_path):
    for env, match in [
        ({"EGRESS_CONTROL": "9", "EGRESS_CONTROL_TOKEN": "short"}, "16 characters"),
        ({"EGRESS_CONTROL": "9", "EGRESS_CONTROL_TOKEN": TOKEN}, "EGRESS_CA_CERT"),
        ({"EGRESS_CONTROL": "9", "EGRESS_CONTROL_TOKEN_FILE": str(tmp_path / "none")}, "TOKEN_FILE"),
    ]:
        proc = _start({**env, "EGRESS_CA_DIR": str(tmp_path / "hosts")})
        _, err = proc.communicate(timeout=20)
        assert proc.returncode != 0 and match in err


@needs_openssl
def test_a_shared_gateway_starts_and_registers(tmp_path):
    ca = gw.Certificates(tmp_path / "ca")
    (tmp_path / "token").write_text(TOKEN + "\n")
    listen, control_port, inbound_port = _free_port(), _free_port(), _free_port()
    proc = _start({"EGRESS_LISTEN": str(listen), "EGRESS_CONTROL": str(control_port),
                   "EGRESS_INBOUND_HTTP": str(inbound_port),
                   "EGRESS_CONTROL_TOKEN_FILE": str(tmp_path / "token"), "EGRESS_ALLOW": "ignored.com",
                   "EGRESS_CA_CERT": str(ca.ca_crt), "EGRESS_CA_KEY": str(ca.ca_key),
                   "EGRESS_CA_DIR": str(tmp_path / "hosts")})
    try:
        _wait_listening(listen, proc)

        async def scenario():
            assert (await _control(control_port, "PUT", "/sandboxes/a", {"address": "127.0.0.2"}))[0] == 200
            _, listing = await _control(control_port, "GET", "/sandboxes")
            assert [s["name"] for s in listing["sandboxes"]] == ["a"]
            # This test's own address was never registered.
            status, body = await _http(listen, b"CONNECT ignored.com:443 HTTP/1.1\r\n\r\n")
            assert status == 403 and json.loads(body)["error"] == "unregistered"
            status, body = await _http(inbound_port, b"GET / HTTP/1.1\r\nHost: nobody.test\r\n\r\n")
            assert status == 404 and json.loads(body)["error"] == "no_such_sandbox"

        asyncio.run(scenario())
    finally:
        proc.terminate()
        _, err = proc.communicate(timeout=20)
    assert "ignoring EGRESS_ALLOW" in err and "shared" in err and f"their UIs on :{inbound_port}" in err


def test_a_single_gateway_still_starts_from_its_environment(tmp_path):
    listen = _free_port()
    proc = _start({"EGRESS_LISTEN": str(listen), "EGRESS_ALLOW": "github.com", "EGRESS_CA_DIR": str(tmp_path)})
    try:
        _wait_listening(listen, proc)
    finally:
        proc.terminate()
        _, err = proc.communicate(timeout=20)
    assert "allow github.com" in err


# --- inbound: sandboxes' UIs, by Host name ---


def test_inbound_names_are_checked_and_held_by_one_sandbox(shared):
    sandbox = gw.Sandbox.register("s", {"address": "10.42.1.7", "inbound": {"ui.example.com": 4096}}, now=0)
    assert sandbox.inbound == {"ui.example.com": 4096}
    for inbound, match in [([], "object"), ({"UI.example.com": 1}, "lowercase"), ({"ui.example.com:80": 1}, "lowercase"),
                           ({"ui.example.com": "4096"}, "numbers"), ({"ui.example.com": 0}, "numbers")]:
        with pytest.raises(gw.ConfigError, match=match):
            gw.Sandbox.register("s", {"address": "10.42.1.7", "inbound": inbound}, now=0)

    assert gw.control("PUT", "/sandboxes/a", {}, {"address": "10.42.1.7", "inbound": {"a.example.com": 1}}, 0)[0] \
        == "200 OK"
    status, answer = gw.control("PUT", "/sandboxes/b", {}, {"address": "10.42.1.8", "inbound": {"a.example.com": 1}}, 0)
    assert status == "409 Conflict" and "a.example.com is a's" in answer["message"]
    # Its own name again, from a new address (a resumed pod), is fine.
    assert gw.control("PUT", "/sandboxes/a", {}, {"address": "10.42.1.9", "inbound": {"a.example.com": 1}}, 0)[0] \
        == "200 OK"


async def _ui(reader, writer):
    """A sandbox's UI: answers with what reached it, or echoes once upgraded to a WebSocket."""
    head = await gw.read_head(reader)
    if gw.header(head, b"upgrade"):
        writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        await writer.drain()
        writer.write(await reader.readexactly(5))
        await writer.drain()
        writer.close()
        return
    body = json.dumps({"path": head[0].decode().split(" ")[1], "host": gw.header(head, b"host"),
                       "connection": gw.header(head, b"connection"),
                       "server": writer.get_extra_info("sockname")[0]}).encode()
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
    await writer.drain()
    writer.close()


@linux_only
def test_inbound_reaches_the_sandbox_holding_the_name(shared):
    async def scenario():
        ui_a, port_a = await _serve(_ui, host="127.0.0.2")
        ui_b, port_b = await _serve(_ui, host="127.0.0.3")
        inbound, inbound_port = await _serve(gw.handle_inbound_http)
        control, control_port = await _serve(gw.handle_control)
        async with ui_a, ui_b, inbound, control:
            for name, address, port in (("a", "127.0.0.2", port_a), ("b", "127.0.0.3", port_b)):
                status, _ = await _control(control_port, "PUT", f"/sandboxes/{name}", {
                    "address": address, "inbound": {f"{name}.sessions.test": port}})
                assert status == 200

            def get(host, extra=""):
                return f"GET /app?x=1 HTTP/1.1\r\nHost: {host}\r\nConnection: keep-alive\r\n{extra}\r\n".encode()

            status, body = await _http(inbound_port, get("a.sessions.test"))
            seen = json.loads(body)
            assert status == 200 and seen["server"] == "127.0.0.2" and seen["path"] == "/app?x=1"
            assert seen["connection"] == "close"  # never a second request on this connection
            status, body = await _http(inbound_port, get("B.Sessions.Test:443"))
            assert status == 200 and json.loads(body)["server"] == "127.0.0.3"
            status, body = await _http(inbound_port, get("c.sessions.test"))
            assert status == 404 and json.loads(body)["error"] == "no_such_sandbox"

            # A WebSocket keeps its upgrade, and then carries whatever either side sends.
            reader, writer = await asyncio.open_connection("127.0.0.1", inbound_port)
            writer.write(get("a.sessions.test", "Upgrade: websocket\r\n").replace(b"keep-alive", b"Upgrade"))
            await writer.drain()
            assert (await gw.read_head(reader))[0].startswith(b"HTTP/1.1 101")
            writer.write(b"hello")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(5), 5) == b"hello"
            writer.close()

            await asyncio.sleep(0.2)
            _, a = await _control(control_port, "GET", "/sandboxes/a/events")
            kinds = [(e["kind"], e["verdict"], e.get("name")) for e in a["events"]]
            assert kinds.count(("inbound", "allow", "a.sessions.test")) == 2
            await _control(control_port, "DELETE", "/sandboxes/b")
            status, _ = await _http(inbound_port, get("b.sessions.test"))
            assert status == 404  # unregistered: its name goes with it

    asyncio.run(scenario())
