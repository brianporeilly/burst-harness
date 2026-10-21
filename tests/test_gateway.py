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
    with pytest.raises(gw.ConfigError, match="EGRESS_MODE"):
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
    reply, event = gw.dns_answer(packet, "", gw.Sandbox())
    ident, flags, questions, answers = struct.unpack("!HHHH", reply[:8])
    assert ident == 0xBEEF and questions == 1 and answers == 0
    assert flags & 0x8000 and flags & 0x0100 and flags & 0x000F == 3  # a response, RD echoed, NXDOMAIN
    assert reply[12:] == packet[12:]
    assert event["verdict"] == "deny" and event["host"] == "direct.example.com" and event["qtype"] == "AAAA"


def test_dns_with_redirect_answers_allowed_names_with_the_gateway():
    sandbox = gw.Sandbox(policy=gw.Policy(allow=(("github.com", 0),)))
    packet = _query("github.com")
    reply, event = gw.dns_answer(packet, "10.89.1.254", sandbox)
    flags, _, answers = struct.unpack("!HHH", reply[2:8])
    assert flags & 0x000F == 0 and answers == 1 and reply.endswith(bytes([10, 89, 1, 254]))
    assert event["verdict"] == "allow"
    # No IPv6 route: the name exists, with no AAAA.
    reply, _ = gw.dns_answer(_query("github.com", 28), "10.89.1.254", sandbox)
    assert struct.unpack("!HHH", reply[2:8])[0] & 0x000F == 0 and struct.unpack("!HHH", reply[2:8])[2] == 0
    # A name the policy would refuse does not resolve at all.
    reply, event = gw.dns_answer(_query("evil.com"), "10.89.1.254", sandbox)
    assert struct.unpack("!H", reply[2:4])[0] & 0x000F == 3 and event["verdict"] == "deny"
    # An address no sandbox is registered at resolves nothing.
    reply, event = gw.dns_answer(packet, "10.89.1.254", None)
    assert struct.unpack("!H", reply[2:4])[0] & 0x000F == 3 and event["detail"] == "unregistered"


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


def test_events_are_json_lines_without_empty_fields(capsys):
    gw.emit("connect", "deny", sandbox=gw.Sandbox(name="burst-t"), id="x-1", host="evil.com", port=443, detail="")
    event = json.loads(capsys.readouterr().out)
    assert event["sandbox"] == "burst-t" and event["verdict"] == "deny" and event["port"] == 443
    assert "detail" not in event


def test_inject_rules_match_host_and_path():
    rules = gw.Inject.parse(
        '[{"host": "GitHub.com", "path": "/o/r.git/*", "headers": {"Authorization": "Basic abc"}}]'
    )
    sandbox = gw.Sandbox(inject=rules)
    assert sandbox.inject_for("github.com", "/o/r.git/info/refs?service=git-upload-pack") is rules[0]
    assert sandbox.inject_for("github.com", "/o/r-other.git/info/refs") is None
    assert sandbox.inject_for("api.github.com", "/o/r.git/info/refs") is None


@pytest.mark.parametrize("path", [
    "/o/r.git/../other.git/info/refs", "/o/r.git/%2e%2e/other.git/info/refs",
    "/o/r.git/..%2Fother.git/info/refs", "/o/r.git//info/refs", "/o/r.git/./info/refs",
])
def test_inject_skips_paths_that_could_mean_something_else(path):
    sandbox = gw.Sandbox(inject=gw.Inject.parse('[{"host": "github.com", "path": "/o/r.git/*", "headers": {"A": "b"}}]'))
    assert sandbox.inject_for("github.com", path) is None


def test_inject_rules_are_validated():
    with pytest.raises(gw.ConfigError, match="exact host"):
        gw.Inject.parse('[{"host": "*.github.com", "headers": {}}]')
    with pytest.raises(gw.ConfigError, match="single lines"):
        gw.Inject.parse('[{"host": "github.com", "headers": {"A": "b\\r\\nX: y"}}]')
    with pytest.raises(gw.ConfigError, match="malformed"):
        gw.Inject.from_items([{"headers": {}}])
    with pytest.raises(gw.ConfigError, match="JSON list"):
        gw.Inject.parse('{"host": "github.com"}')
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


def _handshake(served, host, cafile):
    client = ssl.create_default_context(cafile=cafile)
    c_in, c_out, s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    c = client.wrap_bio(c_in, c_out, server_hostname=host)
    srv = served.wrap_bio(s_in, s_out, server_side=True)
    for _ in range(10):  # pump the handshake between the two ends
        for side in (c, srv):
            try:
                side.do_handshake()
            except ssl.SSLWantReadError:
                pass
        s_in.write(c_out.read())
        c_in.write(s_out.read())
    return c


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")
def test_certificates_chain_to_the_gateways_ca_and_are_made_on_demand(tmp_path):
    certificates = gw.Certificates(tmp_path)
    assert certificates.contexts == {}
    c = _handshake(certificates.context("api.github.com"), "api.github.com", str(tmp_path / "ca.crt"))
    assert c.getpeercert()["subjectAltName"] == (("DNS", "api.github.com"),)
    assert c.selected_alpn_protocol() is None  # the client offered none; the server only speaks http/1.1
    assert (tmp_path / "ca.key").stat().st_mode & 0o077 == 0
    assert certificates.context("api.github.com") is certificates.contexts["api.github.com"]
    # An IP address gets an IP SAN, which is what a client checks it against.
    c = _handshake(certificates.context("192.0.2.100"), "192.0.2.100", str(tmp_path / "ca.crt"))
    assert c.getpeercert()["subjectAltName"] == (("IP Address", "192.0.2.100"),)
    with pytest.raises(ValueError):
        certificates.context("bad/name")


def test_credentials_can_be_for_another_port(monkeypatch):
    rules = gw.Inject.parse('[{"host": "10.0.0.5", "port": 6443, "headers": {"Authorization": "Bearer t"}}]')
    sandbox = gw.Sandbox(inject=rules)
    monkeypatch.setattr(gw, "CERTIFICATES", object())
    assert sandbox.inject_for("10.0.0.5", "/api", 6443) is rules[0]
    assert sandbox.inject_for("10.0.0.5", "/api", 443) is None and sandbox.inject_for("10.0.0.5", "/api") is None
    assert sandbox.intercepts("10.0.0.5", 6443) and not sandbox.intercepts("10.0.0.5", 443)
    with pytest.raises(gw.ConfigError, match="port"):
        gw.Inject.parse('[{"host": "a.com", "port": "x", "headers": {"A": "b"}}]')


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")
def test_upstream_ca_is_a_context_per_host(tmp_path):
    gw.Certificates(tmp_path)
    found = gw.upstream_cas(json.dumps([{"host": "10.0.0.5", "ca": (tmp_path / "ca.crt").read_text()}]))
    assert set(found) == {"10.0.0.5"} and found["10.0.0.5"].verify_mode == ssl.CERT_REQUIRED
    assert gw.upstream_cas("") == {}
    with pytest.raises(gw.ConfigError, match="PEM"):
        gw.upstream_cas('[{"host": "a.com", "ca": "nope"}]')


def test_read_only_lets_reads_and_listed_writes_through():
    sandbox = gw.Sandbox(readonly=True, writes=gw.Write.parse(json.dumps([
        {"host": "api.anthropic.com", "method": "post", "path": "/v1/messages*"},
        {"host": "*", "method": "POST", "path": "*/git-upload-pack"},
    ])))
    assert sandbox.may_send("api.github.com", "GET", "/repos/o/r/pulls")
    assert sandbox.may_send("api.github.com", "head", "/")
    assert not sandbox.may_send("api.github.com", "POST", "/repos/o/r/issues")
    assert not sandbox.may_send("api.github.com", "PUT", "/repos/o/r/pulls/1/merge")
    assert not sandbox.may_send("api.github.com", "DELETE", "/repos/o/r")
    assert sandbox.may_send("api.anthropic.com", "POST", "/v1/messages?beta=true")
    assert sandbox.may_send("api.anthropic.com", "POST", "/v1/messages/count_tokens")
    assert not sandbox.may_send("api.anthropic.com", "POST", "/v1/files")
    assert not sandbox.may_send("api.anthropic.com", "PUT", "/v1/messages")
    assert sandbox.may_send("github.com", "POST", "/o/r.git/git-upload-pack")
    assert not sandbox.may_send("github.com", "POST", "/o/r.git/git-receive-pack")
    # A path that could be read differently upstream matches no exception.
    assert not sandbox.may_send("api.anthropic.com", "POST", "/v1/messages/../files")
    assert not sandbox.may_send("api.anthropic.com", "POST", "/v1/messages%2f..%2ffiles")
    sandbox.readonly = False
    assert sandbox.may_send("api.github.com", "DELETE", "/repos/o/r")


def test_write_rules_need_a_host_and_a_method():
    with pytest.raises(gw.ConfigError, match="host and a method"):
        gw.Write.parse('[{"host": "a.com"}]')
    with pytest.raises(gw.ConfigError, match="host and a method"):
        gw.Write.parse('[{"method": "POST"}]')
    with pytest.raises(gw.ConfigError, match="host and a method"):
        gw.Write.from_items(["POST"])
    assert gw.Write.parse("") == ()


def test_read_only_intercepts_every_tls_host_on_any_port(monkeypatch):
    monkeypatch.setattr(gw, "CERTIFICATES", object())
    sandbox = gw.Sandbox(inject=gw.Inject.parse('[{"host": "github.com", "headers": {"A": "b"}}]'))
    assert sandbox.intercepts("github.com", 443) and not sandbox.intercepts("github.com", 8443)
    assert not sandbox.intercepts("example.com", 443)
    sandbox.readonly = True
    assert sandbox.intercepts("example.com", 443) and sandbox.intercepts("kube.lan", 6443)
    monkeypatch.setattr(gw, "CERTIFICATES", None)
    assert not sandbox.intercepts("example.com", 443)


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


def test_approvals_add_to_the_allowlist_and_are_reread(tmp_path):
    path = tmp_path / "approvals.json"
    sandbox = gw.Sandbox(policy=gw.Policy(allow=(("github.com", 0),)), approvals=gw.Approvals(str(path)))
    assert sandbox.permitted("github.com", 443) and not sandbox.permitted("example.com", 443)
    assert not sandbox.resolvable("example.com")
    path.write_text(json.dumps({"approvals": [{"allow": "example.com", "id": "a-1"}, {"allow": "llm.lan:8080"}]}))
    assert sandbox.permitted("example.com", 443) and sandbox.resolvable("example.com")
    assert not sandbox.permitted("example.com", 8443)
    assert sandbox.permitted("llm.lan", 8080) and not sandbox.permitted("llm.lan", 443) and sandbox.resolvable("llm.lan")
    path.write_text(json.dumps({"approvals": []}) + " ")
    assert not sandbox.permitted("example.com", 443)


def test_a_once_approval_is_spent_by_one_connection_but_not_by_a_lookup(tmp_path):
    path = tmp_path / "approvals.json"
    path.write_text(json.dumps({"approvals": [{"allow": "example.com", "once": True, "id": "a-1"}]}))
    sandbox = gw.Sandbox(approvals=gw.Approvals(str(path)))
    assert sandbox.resolvable("example.com") and sandbox.resolvable("example.com")
    assert sandbox.permitted("example.com", 443)
    assert not sandbox.permitted("example.com", 443) and not sandbox.resolvable("example.com")


def test_expired_and_malformed_approvals_allow_nothing(tmp_path, capsys):
    path = tmp_path / "approvals.json"
    path.write_text(json.dumps({"approvals": [{"allow": "old.com", "expires": 1}, {"allow": "new.com", "expires": 4102444800}]}))
    sandbox = gw.Sandbox(approvals=gw.Approvals(str(path)))
    assert not sandbox.permitted("old.com", 443) and sandbox.permitted("new.com", 443)
    path.write_text("{ not json")
    assert sandbox.permitted("new.com", 443)  # what was read before stands
    assert "ignoring" in capsys.readouterr().err


def test_webhook_is_told_of_a_destination_once_in_a_while(monkeypatch):
    monkeypatch.setattr(gw, "_NOTIFIED", {})
    event = {"verdict": "deny", "id": "a-1", "host": "example.com", "port": 443}
    assert gw.should_notify(event, 1000)
    assert not gw.should_notify(event, 1100)
    assert gw.should_notify({**event, "host": "other.com"}, 1100)
    assert gw.should_notify(event, 1000 + gw.NOTIFY_EVERY)
    assert not gw.should_notify({"verdict": "allow", "id": "a-2", "host": "x.com"}, 5000)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")
def test_a_certificate_that_is_due_is_renewed_in_the_same_context(tmp_path, monkeypatch):
    certificates = gw.Certificates(tmp_path)
    context = certificates.context("api.github.com")
    first = (tmp_path / "api.github.com.crt").read_text()
    assert certificates.context("api.github.com") is context and (tmp_path / "api.github.com.crt").read_text() == first
    clock = gw.time.time() + certificates.RENEW_AFTER + 1
    monkeypatch.setattr(gw.time, "time", lambda: clock)
    assert certificates.context("api.github.com") is context  # a listener holding it serves the new one
    assert (tmp_path / "api.github.com.crt").read_text() != first
    c = _handshake(context, "api.github.com", str(tmp_path / "ca.crt"))
    assert c.getpeercert()["subjectAltName"] == (("DNS", "api.github.com"),)
