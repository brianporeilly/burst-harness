#!/usr/bin/env python3
"""burst-gateway: a sandbox's only network peer.

Run next to a sandbox that has no route of its own (a podman `--internal`
network). The sandbox's HTTPS_PROXY / HTTP_PROXY and its resolver point here,
and this is the only way out. Standard library only; it imports nothing from
the harness.

    EGRESS_MODE     allowlist (default): only EGRESS_ALLOW hosts on EGRESS_PORTS
                    log: everything is let through, and recorded
    EGRESS_ALLOW    comma-separated hosts; "*.example.com" matches subdomains
                    (not example.com itself -- list both if you need both)
    EGRESS_PORTS    comma-separated destination ports (default 443)
    EGRESS_LISTEN   proxy port (default 3128)
    EGRESS_DNS      UDP port of the resolver (default 53; 0 turns it off)
    EGRESS_REDIRECT port that redirected connections arrive on (default 0: off)
    EGRESS_SANDBOX  name of the sandbox this gateway serves, stamped on events
    EGRESS_INJECT   JSON list of credentials to add on the way out (default none):
                    [{"host": "github.com", "path": "/owner/repo.git/*",
                      "headers": {"Authorization": "Basic ..."}},
                     {"host": "api.anthropic.com",
                      "swap": [{"header": "x-api-key", "placeholder": "...", "value": "..."}]}]
    EGRESS_CA_DIR   where the CA for that is made (default /run/burst-gateway)

Software that uses the proxy never resolves a name itself, so a DNS query here
is software that ignored the proxy. What happens to it depends on whether this
gateway is also the sandbox's default route:

  Not (EGRESS_REDIRECT=0). The resolver answers NXDOMAIN and records the query.
  The software had no route anyway; now it leaves a trace.

  It is (EGRESS_REDIRECT set, and burst-gateway-netinit has installed the rules
  that redirect the sandbox's TCP to that port). The resolver answers every
  name the policy could allow with this gateway's own address, and nothing is
  resolved for the sandbox for real. A redirected connection is identified by
  its TLS server name or HTTP Host header, falling back to the address it was
  dialled to, put to the same policy, and connected to *by that name* -- so a
  claimed name cannot be used to reach some other address. Software that
  ignores the proxy then works, under the policy and on the record, and a
  connection to a bare IP address is seen too.

Credentials (EGRESS_INJECT). TLS to a host named in a rule is terminated here,
with a certificate from a CA this gateway makes at start-up and whose key never
leaves it; the sandbox has to trust `ca.crt` from EGRESS_CA_DIR. Each request
is then forwarded over TLS to that same host, changed by the first rule whose
`path` matches (shell-style; no `path` matches all). `headers` are set on every
such request, replacing the request's own. A `swap` acts only on a request that
already carries its `header` with the `placeholder` in it, and puts `value`
there instead: the sandbox is given the placeholder as if it were the
credential, and whichever of several it chooses to send is the one made real.
Either way the sandbox can use a credential it cannot read. A path
with dot segments or encoded separators matches no rule. A request
whose Host header names some other host is refused, so a credential cannot be
steered to a different site behind the same front end. Every other host is
tunnelled untouched, as before. Each request is one connection, HTTP/1.1.

Events are JSON, one per line, on stdout -- `podman logs` is the audit trail.
Everything else (startup, errors) goes to stderr.

    {"ts": ..., "sandbox": ..., "id": ..., "verdict": "allow" | "deny" | "fail" | "close",
     "kind": "connect" | "http" | "tls" | "tcp" | "dns", "peer": ..., "host": ..., "port": ...}

plus `method` and `path` for http, `qtype` for dns, `detail` on a deny or fail,
and `bytes_up`, `bytes_down`, `ms` on the close of a connection that was allowed
(same `id` as its allow). `via: "redirect"` marks a connection that did not use
the proxy, and `dialled` the address it was aimed at when that was not this
gateway. `tls` and `tcp` only ever arrive that way. A `request` is one HTTP
request inside a terminated TLS connection, with `method`, `path`, `status`,
and `injected` (the names of the headers added, never their values); the
connection carrying it has `intercepted: true`.
"""

from __future__ import annotations

import asyncio
import fnmatch
import itertools
import json
import os
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

MODES = ("allowlist", "log")
MAX_HEAD = 65536
QTYPES = {1: "A", 2: "NS", 5: "CNAME", 12: "PTR", 15: "MX", 16: "TXT", 28: "AAAA", 33: "SRV", 65: "HTTPS"}


@dataclass(frozen=True)
class Policy:
    mode: str = "allowlist"
    allow: tuple[str, ...] = ()
    ports: frozenset[int] = frozenset({443})

    @classmethod
    def from_env(cls, env: dict[str, str] | os._Environ[str] = os.environ) -> "Policy":
        mode = env.get("EGRESS_MODE", "allowlist").strip().lower()
        if mode not in MODES:
            raise SystemExit(f"burst-gateway: EGRESS_MODE must be one of {', '.join(MODES)} (got {mode!r}).")
        return cls(
            mode=mode,
            allow=tuple(h.strip().lower().rstrip(".") for h in env.get("EGRESS_ALLOW", "").split(",") if h.strip()),
            ports=frozenset(int(p) for p in env.get("EGRESS_PORTS", "443").split(",") if p.strip()),
        )

    def host_allowed(self, host: str) -> bool:
        return self.mode == "log" or any(fnmatch.fnmatchcase(host, pattern) for pattern in self.allow)

    def allowed(self, host: str, port: int) -> bool:
        return self.mode == "log" or (port in self.ports and self.host_allowed(host))


@dataclass(frozen=True)
class Inject:
    """What to change on requests to `host` whose path matches `path`."""

    host: str
    # (name, value): set on every matching request.
    headers: tuple[tuple[str, str], ...] = ()
    path: str = "*"
    # (header, placeholder, value): where the header carries the placeholder, the value goes instead.
    swap: tuple[tuple[str, str, str], ...] = ()

    @classmethod
    def parse(cls, raw: str) -> tuple["Inject", ...]:
        rules = []
        for item in json.loads(raw) if raw.strip() else []:
            host = str(item["host"]).lower().rstrip(".")
            if not host or any(c in host for c in "*?[/"):
                raise SystemExit(f"burst-gateway: EGRESS_INJECT host must be one exact host name (got {host!r}).")
            headers = tuple((str(k), str(v)) for k, v in (item.get("headers") or {}).items())
            swap = tuple((str(w["header"]), str(w["placeholder"]), str(w["value"])) for w in item.get("swap") or [])
            if any(c in text for parts in (*headers, *swap) for text in parts for c in "\r\n"):
                raise SystemExit("burst-gateway: EGRESS_INJECT headers must be single lines.")
            if any(not placeholder for _, placeholder, _ in swap):
                raise SystemExit("burst-gateway: an EGRESS_INJECT swap needs a placeholder.")
            rules.append(cls(host=host, headers=headers, path=str(item.get("path") or "*"), swap=swap))
        return tuple(rules)


def inject_for(host: str, path: str) -> Inject | None:
    """The first rule for this host whose path pattern matches (the query string is not part of it)."""
    path = path.split("?", 1)[0]
    # A path the server might read differently from how it is spelled here
    # (dot segments, encoded dots and slashes) gets no credential: it could
    # match a rule's pattern and still name something outside it.
    lowered = path.lower()
    if any(part in (".", "..") for part in path.split("/")) or any(c in lowered for c in ("%2e", "%2f", "%5c", "\\", "//")):
        return None
    return next((r for r in INJECT if r.host == host and fnmatch.fnmatchcase(path, r.path)), None)


def with_credentials(headers: list[bytes], rule: Inject | None) -> tuple[list[bytes], list[str]]:
    """The request's header lines as the rule changes them, and the names of the headers it changed."""
    if rule is None:
        return headers, []
    changed: list[str] = []
    out = []
    for line in headers:
        name, _, value = line.partition(b":")
        for header, placeholder, secret in rule.swap:
            if name.strip().lower() == header.lower().encode("latin-1") and placeholder.encode("latin-1") in value:
                line = name + b":" + value.replace(placeholder.encode("latin-1"), secret.encode("latin-1"))
                changed.append(header)
        out.append(line)
    names = {name.lower().encode("latin-1") for name, _ in rule.headers}
    out = [h for h in out if h.split(b":", 1)[0].strip().lower() not in names]
    out += [f"{name}: {value}\r\n".encode("latin-1") for name, value in rule.headers]
    return out, changed + [name for name, _ in rule.headers]


POLICY = Policy()
INJECT: tuple[Inject, ...] = ()
SANDBOX = ""
REDIRECT = 0
# Where TLS for intercepted hosts is terminated: a listener on loopback that
# connections are relayed into, whichever way they arrived. 0 when there are
# no rules.
INTERCEPT = 0
# What is known about a connection relayed there (who, which host, how it
# arrived), by the local port it was relayed from.
_RELAYED: dict[int, dict[str, object]] = {}
_RUN = secrets.token_hex(3)
_SEQ = itertools.count(1)


def next_id() -> str:
    return f"{_RUN}-{next(_SEQ)}"


def emit(kind: str, verdict: str, **fields: object) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    event = {"ts": stamp, "sandbox": SANDBOX, "kind": kind, "verdict": verdict, **fields}
    print(json.dumps({k: v for k, v in event.items() if v not in ("", None)}), flush=True)


# --- proxy ---


def parse_connect(target: str) -> tuple[str, int]:
    """host and port of a CONNECT target; port -1 if it is not a number."""
    host, _, port_text = target.rpartition(":")
    return host.strip("[]").lower().rstrip("."), int(port_text) if port_text.isdigit() else -1


def parse_http(target: str) -> tuple[str, int, str] | None:
    """host, port and origin-form path of an absolute http:// request target."""
    parts = urlsplit(target)
    if parts.scheme.lower() != "http" or not parts.hostname:
        return None
    try:
        port = parts.port or 80
    except ValueError:
        return None
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return parts.hostname.lower().rstrip("."), port, path


def origin_request(method: str, path: str, version: str, headers: list[bytes]) -> bytes:
    """The request to send upstream: origin-form, hop-by-hop headers dropped, one request per connection."""
    kept = [h for h in headers if not h.lower().startswith((b"proxy-", b"connection:", b"keep-alive:"))]
    return f"{method} {path} {version}\r\n".encode("latin-1") + b"".join(kept) + b"Connection: close\r\n\r\n"


async def read_head(reader: asyncio.StreamReader) -> list[bytes]:
    """Request line and headers, as raw lines, up to the blank line."""
    lines: list[bytes] = []
    size = 0
    while (line := await asyncio.wait_for(reader.readline(), 30)) not in (b"\r\n", b"\n", b""):
        size += len(line)
        if size > MAX_HEAD:
            raise ValueError("request head too large")
        lines.append(line)
    return lines


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    total = 0
    try:
        while data := await reader.read(65536):
            total += len(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()
    return total


async def refuse(writer: asyncio.StreamWriter, status: str, body: dict[str, object] | None = None) -> None:
    payload = json.dumps(body).encode() if body else b""
    head = f"HTTP/1.1 {status}\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n"
    if payload:
        head += "Content-Type: application/json\r\n"
    writer.write(head.encode() + b"\r\n" + payload)
    await writer.drain()
    writer.close()


async def deny(writer: asyncio.StreamWriter, kind: str, detail: str = "", **fields: object) -> None:
    """Refuse at once -- never hold a connection open -- and say why in a body an agent can read."""
    request_id = next_id()
    emit(kind, "deny", id=request_id, detail=detail, **fields)
    if kind not in ("connect", "http"):
        return writer.close()  # not speaking HTTP: there is no way to say why
    await refuse(writer, "403 Forbidden", {
        "error": "egress_denied",
        "request_id": request_id,
        "host": fields.get("host"),
        "port": fields.get("port"),
        "message": "Blocked by this sandbox's egress policy. Retrying will not help; "
                   "report the host you need instead.",
    })


async def relay(
    kind: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
    to_upstream: bytes = b"", to_client: bytes = b"", **fields: object,
) -> None:
    """Connect to host:port and pipe both ways, recording the allow and the close.

    TLS to a host with credentials to inject goes to the intercept listener
    instead, which makes the onward connection itself.
    """
    request_id, started = next_id(), time.monotonic()
    host, port = str(fields["host"]), int(str(fields["port"]))
    intercept = bool(INTERCEPT) and kind in ("connect", "tls") and port == 443 and any(r.host == host for r in INJECT)
    if intercept:
        fields["intercepted"] = True
    try:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", INTERCEPT) if intercept else asyncio.open_connection(host, port), 15
        )
    except (OSError, asyncio.TimeoutError) as err:
        emit(kind, "fail", id=request_id, detail=str(err) or type(err).__name__, **fields)
        if kind not in ("connect", "http"):
            return writer.close()
        return await refuse(writer, "502 Bad Gateway")
    emit(kind, "allow", id=request_id, **fields)
    local_port = up_writer.get_extra_info("sockname")[1]
    if intercept:
        _RELAYED[local_port] = dict(host=host, peer=fields.get("peer"), via=fields.get("via"), connection=request_id)
    up_writer.write(to_upstream)
    writer.write(to_client)
    await writer.drain()
    try:
        sent, received = await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
    finally:
        _RELAYED.pop(local_port, None)
    emit(kind, "close", id=request_id, bytes_up=sent + len(to_upstream), bytes_down=received,
         ms=int((time.monotonic() - started) * 1000), **fields)


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = str((writer.get_extra_info("peername") or ("?",))[0])
    try:
        head = await read_head(reader)
        if not head:
            return writer.close()
        method, target, version = (head[0].decode("latin-1").strip().split(" ", 2) + ["", ""])[:3]
        if method.upper() == "CONNECT":
            host, port = parse_connect(target)
            if not POLICY.allowed(host, port):
                return await deny(writer, "connect", peer=peer, host=host, port=port)
            return await relay("connect", reader, writer, peer=peer, host=host, port=port,
                               to_client=b"HTTP/1.1 200 Connection established\r\n\r\n")
        parsed = parse_http(target)
        if parsed is None:
            return await deny(writer, "http", "not a proxy request", peer=peer, method=method or "-", host=target or "-")
        host, port, path = parsed
        # The query string is not recorded: it is where tokens end up.
        fields = dict(peer=peer, host=host, port=port, method=method, path=path.split("?", 1)[0])
        if not POLICY.allowed(host, port):
            return await deny(writer, "http", **fields)
        await relay("http", reader, writer, to_upstream=origin_request(method, path, version, head[1:]), **fields)
    except (asyncio.TimeoutError, ConnectionError, OSError, ValueError):
        writer.close()


# --- terminated TLS: credentials ---


def make_certificates(directory: Path, hosts: list[str]) -> ssl.SSLContext:
    """A CA in `directory` and a certificate under it for each host; the context that serves them."""
    directory.mkdir(parents=True, exist_ok=True)

    def openssl(*args: str, stdin: bytes | None = None) -> bytes:
        proc = subprocess.run(["openssl", *args], input=stdin, capture_output=True)
        if proc.returncode != 0:
            raise SystemExit(f"burst-gateway: openssl {args[0]} failed: {proc.stderr.decode(errors='replace').strip()}")
        return proc.stdout

    key_args = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes")
    ca_key, ca_crt = directory / "ca.key", directory / "ca.crt"
    openssl("req", "-x509", *key_args, "-keyout", str(ca_key), "-out", str(ca_crt), "-days", "30",
            "-subj", f"/CN=burst-gateway {SANDBOX or 'sandbox'}",
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0", "-addext", "keyUsage=critical,keyCertSign")
    ca_key.chmod(0o600)
    contexts: dict[str, ssl.SSLContext] = {}
    for host in hosts:
        key, crt, ext = directory / f"{host}.key", directory / f"{host}.crt", directory / f"{host}.ext"
        ext.write_text(f"subjectAltName=DNS:{host}\nextendedKeyUsage=serverAuth\n")
        request = openssl("req", "-new", *key_args, "-keyout", str(key), "-subj", f"/CN={host}")
        openssl("x509", "-req", "-CA", str(ca_crt), "-CAkey", str(ca_key), "-days", "30",
                "-extfile", str(ext), "-out", str(crt), stdin=request)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(crt), str(key))
        # One request per connection, parsed as HTTP/1.1: do not offer h2.
        context.set_alpn_protocols(["http/1.1"])
        contexts[host] = context

    def choose(sslobj: ssl.SSLObject, name: str | None, _context: ssl.SSLContext) -> None:
        if name and (chosen := contexts.get(name.lower().rstrip("."))):
            sslobj.context = chosen

    # Served when the client names no host we hold a certificate for: the handshake then fails on its side.
    default = next(iter(contexts.values()))
    default.sni_callback = choose
    return default


async def pipe_response(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> tuple[int, int]:
    """pipe(), also reading the status code off the front of what passes."""
    status, total, front = 0, 0, b""
    try:
        while data := await reader.read(65536):
            if not status and len(front) < 64:
                front += data
                parts = front.split(b" ", 2)
                if len(parts) > 2 and parts[1].isdigit():
                    status = int(parts[1])
            total += len(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()
    return status, total


async def handle_intercepted(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """One HTTP request from a client whose TLS we terminated, forwarded with credentials added."""
    known = _RELAYED.get((writer.get_extra_info("peername") or ("", 0))[1])
    if known is None:  # only this gateway's own relays may speak here
        return writer.close()
    host = str(known["host"])
    request_id, started = next_id(), time.monotonic()
    fields: dict[str, object] = dict(id=request_id, peer=known["peer"], host=host, port=443, via=known["via"],
                                     connection=known["connection"])
    try:
        head = await read_head(reader)
        if not head:
            return writer.close()
        method, target, version = (head[0].decode("latin-1").strip().split(" ", 2) + ["", ""])[:3]
        fields.update(method=method, path=target.split("?", 1)[0])
        named = http_host(b"".join(head))
        if named != host:
            emit("request", "deny", detail=f"Host header names {named or 'nothing'}", **fields)
            return await refuse(writer, "421 Misdirected Request")
        rule = inject_for(host, target)
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(host, 443, ssl=ssl.create_default_context(), server_hostname=host), 15
            )
        except (OSError, asyncio.TimeoutError) as err:
            emit("request", "fail", detail=str(err) or type(err).__name__, **fields)
            return await refuse(writer, "502 Bad Gateway")
        headers, injected = with_credentials(head[1:], rule)
        up_writer.write(origin_request(method, target, version, headers))
        sent, (status, received) = await asyncio.gather(pipe(reader, up_writer), pipe_response(up_reader, writer))
        emit("request", "allow", status=status or None, injected=injected or None,
             bytes_up=sent, bytes_down=received, ms=int((time.monotonic() - started) * 1000), **fields)
    except (asyncio.TimeoutError, ConnectionError, OSError, ValueError):
        writer.close()


# --- redirected connections ---

SO_ORIGINAL_DST = 80
HTTP_METHODS = (b"GET ", b"POST ", b"PUT ", b"HEAD ", b"DELETE ", b"PATCH ", b"OPTIONS ")


def original_destination(sock: socket.socket) -> tuple[str, int]:
    """Where a redirected connection was aimed before the redirect."""
    raw = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
    return socket.inet_ntoa(raw[4:8]), struct.unpack("!H", raw[2:4])[0]


def tls_server_name(hello: bytes) -> str:
    """The SNI host name in a TLS ClientHello record, or "" if there is none."""
    try:
        if hello[0] != 0x16 or hello[5] != 0x01:
            return ""
        pos = 5 + 4 + 2 + 32                                   # record, handshake, version, random
        pos += 1 + hello[pos]                                  # session id
        pos += 2 + struct.unpack("!H", hello[pos:pos + 2])[0]  # cipher suites
        pos += 1 + hello[pos]                                  # compression methods
        end = pos + 2 + struct.unpack("!H", hello[pos:pos + 2])[0]
        pos += 2
        while pos + 4 <= end:
            kind, length = struct.unpack("!HH", hello[pos:pos + 4])
            if kind == 0 and hello[pos + 6] == 0:              # server_name, type host_name
                size = struct.unpack("!H", hello[pos + 7:pos + 9])[0]
                return hello[pos + 9:pos + 9 + size].decode("ascii").lower().rstrip(".")
            pos += 4 + length
    except (IndexError, struct.error, UnicodeDecodeError):
        pass
    return ""


def http_host(head: bytes) -> str:
    """The Host header's name in a request head, or ""."""
    for line in head.split(b"\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"host":
            host = value.decode("latin-1").strip()
            # Drop a port, but leave a bracketed IPv6 literal's colons alone.
            return (host.rsplit(":", 1)[0] if host.rsplit(":", 1)[-1].isdigit() else host).strip("[]").lower().rstrip(".")
    return ""


async def read_opening(reader: asyncio.StreamReader) -> bytes:
    """What the client says first, far enough to name the server: a whole
    ClientHello or a whole HTTP head. Empty if it waits for the server to speak."""
    try:
        data = await asyncio.wait_for(reader.read(MAX_HEAD), 3)
        while data and len(data) < MAX_HEAD:
            if data[0] == 0x16:
                if len(data) >= 5 and len(data) >= 5 + struct.unpack("!H", data[3:5])[0]:
                    break
            elif not data.startswith(HTTP_METHODS) or b"\r\n\r\n" in data:
                break
            more = await asyncio.wait_for(reader.read(MAX_HEAD), 10)
            if not more:
                break
            data += more
        return data
    except asyncio.TimeoutError:
        return b""


async def handle_redirected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    peer = str((writer.get_extra_info("peername") or ("?",))[0])
    try:
        dialled, port = original_destination(sock)
        here = sock.getsockname()[0]
        opening = await read_opening(reader)
        fields: dict[str, object] = dict(peer=peer, port=port, via="redirect")
        if opening[:1] == b"\x16":
            kind, name = "tls", tls_server_name(opening)
        elif opening.startswith(HTTP_METHODS):
            kind, name = "http", http_host(opening)
            method, target = (opening.split(b"\r\n", 1)[0].decode("latin-1").split(" ") + [""])[:2]
            fields.update(method=method, path=target.split("?", 1)[0])
        else:
            kind, name = "tcp", ""
        if dialled != here:
            fields["dialled"] = dialled
        # A name the client gave is what gets checked and connected to; the
        # address it dialled is only believed when there is no name.
        host = name or (dialled if dialled != here else "")
        if not host:
            return await deny(writer, kind, "no server name to identify the destination", host="-", **fields)
        if not POLICY.allowed(host, port):
            return await deny(writer, kind, host=host, **fields)
        await relay(kind, reader, writer, to_upstream=opening, host=host, **fields)
    except (asyncio.TimeoutError, ConnectionError, OSError, ValueError):
        writer.close()


# --- DNS ---


def dns_question(packet: bytes) -> tuple[str, int, int]:
    """The first question's name and type, and the offset just past it."""
    if len(packet) < 12 or struct.unpack("!H", packet[4:6])[0] < 1:
        raise ValueError("no question")
    labels, pos = [], 12
    while (length := packet[pos]) != 0:
        if length > 63:
            raise ValueError("compressed or malformed name")
        labels.append(packet[pos + 1:pos + 1 + length].decode("ascii", "replace"))
        pos += 1 + length
    qtype = struct.unpack("!H", packet[pos + 1:pos + 3])[0]
    if len(packet) < pos + 5:
        raise ValueError("truncated question")
    return ".".join(labels).lower(), qtype, pos + 5


def dns_reply(packet: bytes, end: int, *, rcode: int = 0, address: str = "") -> bytes:
    """A response to `packet` echoing its question: an A record for `address`, or none."""
    flags = struct.unpack("!H", packet[2:4])[0]
    # QR, the query's opcode and RD, RA, the rcode.
    reply_flags = 0x8000 | (flags & 0x7900) | 0x0080 | rcode
    answer = b""
    if address:
        # A pointer to the question's name; class IN, a short TTL so nothing caches it for long.
        answer = struct.pack("!HHHIH", 0xC00C, 1, 1, 5, 4) + socket.inet_aton(address)
    return packet[:2] + struct.pack("!HHHHH", reply_flags, 1, 1 if answer else 0, 0, 0) + packet[12:end] + answer


def dns_answer(packet: bytes, here: str) -> tuple[bytes, dict[str, object]]:
    """The reply to a query and the event recording it. `here` is this gateway's
    address as the asker sees it, or "" when connections are not redirected."""
    name, qtype, end = dns_question(packet)
    fields: dict[str, object] = dict(host=name, qtype=QTYPES.get(qtype, str(qtype)))
    if not here:
        return dns_reply(packet, end, rcode=3), dict(
            fields, verdict="deny", detail="no DNS in the sandbox; this lookup did not go through the proxy")
    if not POLICY.host_allowed(name):
        return dns_reply(packet, end, rcode=3), dict(fields, verdict="deny")
    if qtype == 1:
        return dns_reply(packet, end, address=here), dict(fields, verdict="allow", detail="answered with the gateway")
    # Other types of an allowed name: it exists, with nothing of that type (no IPv6 route, for one).
    return dns_reply(packet, end), dict(fields, verdict="allow", detail="no record of this type")


def address_facing(peer: str) -> str:
    """This host's address on the route to `peer`."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((peer, 9))
        return probe.getsockname()[0]


class Resolver(asyncio.DatagramProtocol):
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            reply, event = dns_answer(data, address_facing(addr[0]) if REDIRECT else "")
        except (ValueError, IndexError, struct.error, OSError):
            return
        emit("dns", str(event.pop("verdict")), id=next_id(), peer=addr[0], **event)
        self.transport.sendto(reply, addr)  # type: ignore[attr-defined]


async def main() -> None:
    global POLICY, INJECT, SANDBOX, REDIRECT, INTERCEPT
    POLICY = Policy.from_env()
    SANDBOX = os.environ.get("EGRESS_SANDBOX", "")
    INJECT = Inject.parse(os.environ.get("EGRESS_INJECT", ""))
    listen = int(os.environ.get("EGRESS_LISTEN", "3128"))
    dns_port = int(os.environ.get("EGRESS_DNS", "53"))
    REDIRECT = int(os.environ.get("EGRESS_REDIRECT", "0"))
    if POLICY.mode == "allowlist" and not POLICY.allow:
        print("burst-gateway: EGRESS_ALLOW is empty; everything will be refused.", file=sys.stderr)
    loop = asyncio.get_running_loop()
    if dns_port:
        await loop.create_datagram_endpoint(Resolver, local_addr=("0.0.0.0", dns_port))
    if REDIRECT:
        await asyncio.start_server(handle_redirected, "0.0.0.0", REDIRECT)
    if INJECT:
        hosts = sorted({rule.host for rule in INJECT})
        context = make_certificates(Path(os.environ.get("EGRESS_CA_DIR", "/run/burst-gateway")), hosts)
        terminator = await asyncio.start_server(handle_intercepted, "127.0.0.1", 0, ssl=context)
        INTERCEPT = terminator.sockets[0].getsockname()[1]
        print(f"burst-gateway: adding credentials for {', '.join(hosts)}", file=sys.stderr, flush=True)
    server = await asyncio.start_server(handle, "0.0.0.0", listen)
    what = "everything (log only)" if POLICY.mode == "log" else \
        f"ports {sorted(POLICY.ports)}; allow {', '.join(POLICY.allow) or '(nothing)'}"
    print(f"burst-gateway: proxy :{listen}, dns :{dns_port or 'off'}, redirected :{REDIRECT or 'off'}; {what}",
          file=sys.stderr, flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
