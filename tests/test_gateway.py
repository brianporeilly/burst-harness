import importlib.util
import json
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
    monkeypatch.setattr(gw, "POLICY", gw.Policy(allow=("github.com",)))
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
