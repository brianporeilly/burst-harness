import importlib.util
import json
import shutil
import ssl
import struct
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "burst_gateway", Path(__file__).resolve().parent.parent / "gateway" / "burst_gateway.py"
)
gw = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gw  # dataclasses look the module up while it loads
_spec.loader.exec_module(gw)


def test_allowlist_matches_hosts_and_ports():
    policy = gw.Policy.from_env({"EGRESS_ALLOW": "github.com, *.Example.com."})
    assert policy.allowed("github.com", 443)
    assert policy.allowed("a.example.com", 443)
    assert not policy.allowed("example.com", 443)  # the wildcard is subdomains only
    assert not policy.allowed("github.com", 22)
    assert not policy.allowed("evil.com", 443)


def test_allowlist_entry_with_a_port_allows_only_that_port():
    policy = gw.Policy.from_env({"EGRESS_ALLOW": "github.com,llm.lan:8080"})
    assert policy.allowed("llm.lan", 8080)
    assert not policy.allowed("llm.lan", 443)
    assert not policy.allowed("github.com", 8080)
    assert policy.host_allowed("llm.lan")


def test_parse_inbound():
    assert gw.parse_inbound("4096=10.89.1.2:4096, 8080=10.89.1.2:80") == {4096: ("10.89.1.2", 4096), 8080: ("10.89.1.2", 80)}
    assert gw.parse_inbound("") == {}
    with pytest.raises(SystemExit, match="PORT=ADDRESS:PORT"):
        gw.parse_inbound("4096")


def test_log_mode_allows_everything():
    policy = gw.Policy.from_env({"EGRESS_MODE": "log"})
    assert policy.allowed("anything.example", 8080)


def test_unknown_mode_is_refused():
    with pytest.raises(SystemExit, match="EGRESS_MODE"):
        gw.Policy.from_env({"EGRESS_MODE": "open"})


def test_parse_connect():
    assert gw.parse_connect("GitHub.com.:443") == ("github.com", 443)
    assert gw.parse_connect("[::1]:8443") == ("::1", 8443)
    assert gw.parse_connect("github.com")[1] == -1


def test_parse_http():
    assert gw.parse_http("http://Example.com/a/b?x=1") == ("example.com", 80, "/a/b?x=1")
    assert gw.parse_http("http://example.com:8080") == ("example.com", 8080, "/")
    assert gw.parse_http("https://example.com/") is None
    assert gw.parse_http("/just/a/path") is None


def test_origin_request_drops_hop_by_hop_headers():
    sent = gw.origin_request("GET", "/a?x=1", "HTTP/1.1", [
        b"Host: example.com\r\n", b"Proxy-Authorization: Basic abc\r\n",
        b"Proxy-Connection: keep-alive\r\n", b"Connection: keep-alive\r\n", b"Accept: */*\r\n",
    ])
    assert sent == b"GET /a?x=1 HTTP/1.1\r\nHost: example.com\r\nAccept: */*\r\nConnection: close\r\n\r\n"


def _query(name: str, qtype: int = 1) -> bytes:
    labels = b"".join(bytes([len(part)]) + part.encode() for part in name.split("."))
    return struct.pack("!HHHHHH", 0xBEEF, 0x0100, 1, 0, 0, 0) + labels + b"\x00" + struct.pack("!HH", qtype, 1)


def test_dns_without_redirect_refuses_and_records():
    packet = _query("Direct.Example.com", 28)
    name, qtype, end = gw.dns_question(packet)
    assert (name, qtype, end) == ("direct.example.com", 28, len(packet))
    reply, event = gw.dns_answer(packet, "")
    ident, flags, questions, answers = struct.unpack("!HHHH", reply[:8])
    assert ident == 0xBEEF and questions == 1 and answers == 0
    assert flags & 0x8000 and flags & 0x0100 and flags & 0x000F == 3  # a response, RD echoed, NXDOMAIN
    assert reply[12:] == packet[12:]
    assert event["verdict"] == "deny" and event["host"] == "direct.example.com" and event["qtype"] == "AAAA"


def test_dns_with_redirect_answers_allowed_names_with_the_gateway(monkeypatch):
    monkeypatch.setattr(gw, "POLICY", gw.Policy(allow=(("github.com", 0),)))
    packet = _query("github.com")
    reply, event = gw.dns_answer(packet, "10.89.1.254")
    flags, _, answers = struct.unpack("!HHH", reply[2:8])
    assert flags & 0x000F == 0 and answers == 1 and reply.endswith(bytes([10, 89, 1, 254]))
    assert event["verdict"] == "allow"
    # No IPv6 route: the name exists, with no AAAA.
    reply, _ = gw.dns_answer(_query("github.com", 28), "10.89.1.254")
    assert struct.unpack("!HHH", reply[2:8])[0] & 0x000F == 0 and struct.unpack("!HHH", reply[2:8])[2] == 0
    # A name the policy would refuse does not resolve at all.
    reply, event = gw.dns_answer(_query("evil.com"), "10.89.1.254")
    assert struct.unpack("!H", reply[2:4])[0] & 0x000F == 3 and event["verdict"] == "deny"


def _client_hello(server_name: str | None) -> bytes:
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ssl.create_default_context().wrap_bio(incoming, outgoing, server_hostname=server_name)
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


def test_tls_server_name():
    assert gw.tls_server_name(_client_hello("API.Example.com")) == "api.example.com"
    hello = _client_hello("api.example.com")
    assert gw.tls_server_name(hello[:60]) == ""  # truncated: no name, no exception
    assert gw.tls_server_name(b"GET / HTTP/1.1\r\n\r\n") == ""


def test_http_host():
    assert gw.http_host(b"GET / HTTP/1.1\r\nAccept: */*\r\nHOST: Example.com:8080\r\n\r\n") == "example.com"
    assert gw.http_host(b"GET / HTTP/1.1\r\nHost: [::1]:80\r\n\r\n") == "::1"
    assert gw.http_host(b"GET / HTTP/1.0\r\n\r\n") == ""


def test_dns_question_rejects_garbage():
    with pytest.raises((ValueError, IndexError, struct.error)):
        gw.dns_question(b"\x00" * 5)


def test_events_are_json_lines_without_empty_fields(capsys, monkeypatch):
    monkeypatch.setattr(gw, "SANDBOX", "burst-t")
    gw.emit("connect", "deny", id="x-1", host="evil.com", port=443, detail="")
    event = json.loads(capsys.readouterr().out)
    assert event["sandbox"] == "burst-t" and event["verdict"] == "deny" and event["port"] == 443
    assert "detail" not in event


def test_inject_rules_match_host_and_path(monkeypatch):
    rules = gw.Inject.parse(
        '[{"host": "GitHub.com", "path": "/o/r.git/*", "headers": {"Authorization": "Basic abc"}}]'
    )
    monkeypatch.setattr(gw, "INJECT", rules)
    assert gw.inject_for("github.com", "/o/r.git/info/refs?service=git-upload-pack") is rules[0]
    assert gw.inject_for("github.com", "/o/r-other.git/info/refs") is None
    assert gw.inject_for("api.github.com", "/o/r.git/info/refs") is None


@pytest.mark.parametrize("path", [
    "/o/r.git/../other.git/info/refs", "/o/r.git/%2e%2e/other.git/info/refs",
    "/o/r.git/..%2Fother.git/info/refs", "/o/r.git//info/refs", "/o/r.git/./info/refs",
])
def test_inject_skips_paths_that_could_mean_something_else(monkeypatch, path):
    monkeypatch.setattr(gw, "INJECT", gw.Inject.parse('[{"host": "github.com", "path": "/o/r.git/*", "headers": {"A": "b"}}]'))
    assert gw.inject_for("github.com", path) is None


def test_inject_rules_are_validated():
    with pytest.raises(SystemExit, match="exact host"):
        gw.Inject.parse('[{"host": "*.github.com", "headers": {}}]')
    with pytest.raises(SystemExit, match="single lines"):
        gw.Inject.parse('[{"host": "github.com", "headers": {"A": "b\\r\\nX: y"}}]')
    assert gw.Inject.parse("") == ()


def test_headers_replace_the_requests_own():
    rule = gw.Inject(host="github.com", headers=(("Authorization", "Basic real"),))
    sent, changed = gw.with_credentials([b"Host: github.com\r\n", b"authorization: Basic guess\r\n"], rule)
    assert sent == [b"Host: github.com\r\n", b"Authorization: Basic real\r\n"]
    assert changed == ["Authorization"]
    assert gw.with_credentials([b"Host: github.com\r\n"], None) == ([b"Host: github.com\r\n"], [])


def test_swap_only_touches_the_header_carrying_its_placeholder():
    rule = gw.Inject.parse(json.dumps([{"host": "api.anthropic.com", "swap": [
        {"header": "x-api-key", "placeholder": "PH-KEY", "value": "real-key"},
        {"header": "Authorization", "placeholder": "PH-OAUTH", "value": "real-oauth"},
    ]}]))[0]
    sent, changed = gw.with_credentials([b"X-Api-Key: PH-KEY\r\n", b"User-Agent: PH-KEY PH-OAUTH\r\n"], rule)
    assert sent == [b"X-Api-Key: real-key\r\n", b"User-Agent: PH-KEY PH-OAUTH\r\n"]
    assert changed == ["x-api-key"]
    sent, changed = gw.with_credentials([b"authorization: Bearer PH-OAUTH\r\n"], rule)
    assert sent == [b"authorization: Bearer real-oauth\r\n"] and changed == ["Authorization"]
    # A request that brings its own credential, or none, is left alone.
    assert gw.with_credentials([b"x-api-key: something-else\r\n"], rule) == ([b"x-api-key: something-else\r\n"], [])


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")
def test_certificates_chain_to_the_gateways_ca(tmp_path):
    served = gw.make_certificates(tmp_path, ["github.com"])
    client = ssl.create_default_context(cafile=str(tmp_path / "ca.crt"))
    c_in, c_out, s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    c = client.wrap_bio(c_in, c_out, server_hostname="github.com")
    srv = served.wrap_bio(s_in, s_out, server_side=True)
    for _ in range(10):  # pump the handshake between the two ends
        for side in (c, srv):
            try:
                side.do_handshake()
            except ssl.SSLWantReadError:
                pass
        s_in.write(c_out.read())
        c_in.write(s_out.read())
    assert c.getpeercert()["subjectAltName"] == (("DNS", "github.com"),)
    assert c.selected_alpn_protocol() is None  # the client offered none; the server only speaks http/1.1
    assert (tmp_path / "ca.key").stat().st_mode & 0o077 == 0


def test_expired_on_idle_or_lifetime():
    assert gw.expired(now=100, started=0, active=95, idle=10, lifetime=0) == ""
    assert "idle" in gw.expired(now=100, started=0, active=80, idle=10, lifetime=0)
    assert "lifetime" in gw.expired(now=100, started=0, active=99, idle=10, lifetime=60)
    assert gw.expired(now=10**6, started=0, active=0, idle=0, lifetime=0) == ""


def test_closing_a_connection_is_not_activity(capsys):
    gw.emit("connect", "allow", id="x-1", host="a.com", port=443)
    stamp = gw._ACTIVE
    gw.emit("connect", "close", id="x-1", host="a.com", port=443)
    assert gw._ACTIVE == stamp
    capsys.readouterr()
