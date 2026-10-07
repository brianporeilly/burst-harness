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
    EGRESS_DNS      UDP port of the DNS sink (default 53; 0 turns it off)
    EGRESS_SANDBOX  name of the sandbox this gateway serves, stamped on events

The DNS sink answers every query NXDOMAIN and records it. Software that uses
the proxy never resolves a name itself, so a query here is software that
ignored the proxy: it had no route anyway, and now it leaves a trace.

Events are JSON, one per line, on stdout -- `podman logs` is the audit trail.
Everything else (startup, errors) goes to stderr.

    {"ts": ..., "sandbox": ..., "id": ..., "kind": "connect" | "http" | "dns",
     "verdict": "allow" | "deny" | "fail" | "close", "peer": ..., "host": ...,
     "port": ...}

plus `method` and `path` for http, `qtype` for dns, `detail` on a deny or fail,
and `bytes_up`, `bytes_down`, `ms` on the close of a connection that was allowed
(same `id` as its allow).
"""

from __future__ import annotations

import asyncio
import fnmatch
import itertools
import json
import os
import secrets
import struct
import sys
import time
from dataclasses import dataclass
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

    def allowed(self, host: str, port: int) -> bool:
        if self.mode == "log":
            return True
        return port in self.ports and any(fnmatch.fnmatchcase(host, pattern) for pattern in self.allow)


POLICY = Policy()
SANDBOX = ""
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
    """Connect to host:port and pipe both ways, recording the allow and the close."""
    request_id, started = next_id(), time.monotonic()
    try:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(str(fields["host"]), int(str(fields["port"]))), 15
        )
    except (OSError, asyncio.TimeoutError) as err:
        emit(kind, "fail", id=request_id, detail=str(err) or type(err).__name__, **fields)
        return await refuse(writer, "502 Bad Gateway")
    emit(kind, "allow", id=request_id, **fields)
    up_writer.write(to_upstream)
    writer.write(to_client)
    await writer.drain()
    sent, received = await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
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


# --- DNS sink ---


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


def dns_nxdomain(packet: bytes, end: int) -> bytes:
    """A response to `packet` saying the name does not exist, echoing its question."""
    flags = struct.unpack("!H", packet[2:4])[0]
    # QR, the query's opcode and RD, RA, rcode 3.
    reply_flags = 0x8000 | (flags & 0x7900) | 0x0080 | 3
    return packet[:2] + struct.pack("!HHHHH", reply_flags, 1, 0, 0, 0) + packet[12:end]


class DnsSink(asyncio.DatagramProtocol):
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            name, qtype, end = dns_question(data)
        except (ValueError, IndexError, struct.error):
            return
        emit("dns", "deny", id=next_id(), peer=addr[0], host=name, qtype=QTYPES.get(qtype, str(qtype)),
             detail="no DNS in the sandbox; this lookup did not go through the proxy")
        self.transport.sendto(dns_nxdomain(data, end), addr)  # type: ignore[attr-defined]


async def main() -> None:
    global POLICY, SANDBOX
    POLICY = Policy.from_env()
    SANDBOX = os.environ.get("EGRESS_SANDBOX", "")
    listen = int(os.environ.get("EGRESS_LISTEN", "3128"))
    dns_port = int(os.environ.get("EGRESS_DNS", "53"))
    if POLICY.mode == "allowlist" and not POLICY.allow:
        print("burst-gateway: EGRESS_ALLOW is empty; everything will be refused.", file=sys.stderr)
    loop = asyncio.get_running_loop()
    if dns_port:
        await loop.create_datagram_endpoint(DnsSink, local_addr=("0.0.0.0", dns_port))
    server = await asyncio.start_server(handle, "0.0.0.0", listen)
    what = "everything (log only)" if POLICY.mode == "log" else \
        f"ports {sorted(POLICY.ports)}; allow {', '.join(POLICY.allow) or '(nothing)'}"
    print(f"burst-gateway listening on :{listen}, dns sink on :{dns_port or 'off'}; {what}", file=sys.stderr, flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
