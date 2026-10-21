#!/usr/bin/env python3
"""burst-gateway: a sandbox's only network peer.

Run next to a sandbox that has no route of its own (a podman `--internal`
network). The sandbox's HTTPS_PROXY / HTTP_PROXY and its resolver point here,
and this is the only way out. Standard library only; it imports nothing from
the harness.

    EGRESS_MODE     allowlist (default): only EGRESS_ALLOW hosts on EGRESS_PORTS
                    log: everything is let through, and recorded
    EGRESS_ALLOW    comma-separated hosts; "*.example.com" matches subdomains
                    (not example.com itself -- list both if you need both).
                    "host:8080" allows that host on that port instead of on
                    EGRESS_PORTS -- how a plain-HTTP service is let through.
    EGRESS_PORTS    comma-separated destination ports (default 443)
    EGRESS_INBOUND  comma-separated "PORT=ADDRESS:PORT": connections to this
                    gateway's PORT are passed to that address in the sandbox
                    (default none). The way in to a UI the sandbox serves.
    EGRESS_LISTEN   proxy port (default 3128)
    EGRESS_DNS      UDP port of the resolver (default 53; 0 turns it off)
    EGRESS_REDIRECT port that redirected connections arrive on (default 0: off)
    EGRESS_SANDBOX  name of the sandbox this gateway serves, stamped on events
    EGRESS_APPROVALS file of approvals added while running (default
                    /run/burst-gateway/approvals.json), re-read when it changes
    EGRESS_WEBHOOK  URL that each refusal is POSTed to as JSON (default none)
    EGRESS_IDLE     exit after this many seconds with no event (default 0: never)
    EGRESS_LIFETIME exit this many seconds after starting (default 0: never)
    EGRESS_INJECT   JSON list of credentials to add on the way out (default none):
                    [{"host": "github.com", "path": "/owner/repo.git/*",
                      "headers": {"Authorization": "Basic ..."}},
                     {"host": "10.0.0.5", "port": 6443, "headers": {...}},
                     {"host": "api.anthropic.com",
                      "swap": [{"header": "x-api-key", "placeholder": "...", "value": "..."}]}]
    EGRESS_CA_DIR   where the CA for that is made (default /run/burst-gateway)
    EGRESS_UPSTREAM_CA JSON list of CAs to verify a host's own certificate with,
                    for a server whose CA is not a public one (default none):
                    [{"host": "10.0.0.5", "ca": "-----BEGIN CERTIFICATE-----..."}]
    EGRESS_READONLY "1": requests may only read -- GET, HEAD, OPTIONS -- except
                    those EGRESS_WRITES lets through (default off)
    EGRESS_WRITES   JSON list of requests that may write anyway (default none):
                    [{"host": "api.anthropic.com", "method": "POST", "path": "/v1/messages*"}]
                    `host` and `path` are shell-style patterns; no `path` matches all

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

Credentials (EGRESS_INJECT). TLS to a host named in a rule (on its `port`,
443 unless it says) is terminated here, with a certificate from a CA this
gateway makes at start-up and whose key never leaves it; the sandbox has to
trust `ca.crt` from EGRESS_CA_DIR. The certificate is chosen by the host the
connection was allowed for, not by what the client names, so a host given as an
IP address (no SNI) works too. Each request is then forwarded over TLS to that
same host and port -- verified against the public CAs, or the host's
EGRESS_UPSTREAM_CA -- changed by the first rule whose
`path` matches (shell-style; no `path` matches all). `headers` are set on every
such request, replacing the request's own. A `swap` acts only on a request that
already carries its `header` with the `placeholder` in it, and puts `value`
there instead: the sandbox is given the placeholder as if it were the
credential, and whichever of several it chooses to send is the one made real.
Either way the sandbox can use a credential it cannot read. A path
with dot segments or encoded separators matches no rule. A request
whose Host header names some other host is refused, so a credential cannot be
steered to a different site behind the same front end. Every other host is
tunnelled untouched (unless EGRESS_READONLY), as before. Each request is one
connection, HTTP/1.1.

Read-only (EGRESS_READONLY). For a job that should only read, whatever text it
reads tells it to do. A method is only visible inside TLS, so every TLS
connection is terminated here, as for credentials (certificates are made per
host on first use), and each request is put to the rule: GET, HEAD and OPTIONS
pass, and anything else must match an EGRESS_WRITES entry or is refused with a
403 the agent can read. Plain HTTP is checked the same way. A connection that
does not speak TLS and HTTP gets nowhere: what cannot be seen cannot be read
as read-only. Approvals do not lift this; they say where, not what.

Approvals (EGRESS_APPROVALS). A refusal is immediate and final for that
request: nothing is held open waiting for an answer. Its event and its 403 carry
an id, and whoever runs the sandbox can approve what was refused by adding to
the approvals file, which is read again whenever it changes:

    {"approvals": [{"allow": "example.com", "id": "a1b2c3-7"},
                   {"allow": "llm.lan:8080", "expires": 1790000000},
                   {"allow": "pypi.org", "once": true, "id": "a1b2c3-9"}]}

`allow` is an EGRESS_ALLOW entry. `expires` is a Unix time after which it no
longer counts; `once` lets one connection through and is then spent. These add
to EGRESS_ALLOW and never take away from it. EGRESS_WEBHOOK, if set, is told of
each refusal -- once per destination every few minutes, not once per retry.

Stopping by itself (EGRESS_IDLE, EGRESS_LIFETIME). The gateway sees everything
that goes in or out, so it is the one place that knows a sandbox has gone quiet.
Any event but the close of a connection counts as activity: a request going
out, a lookup, someone connecting to the UI. When it exits, a sandbox that is
watching for it (burst-sandbox-wait) exits too, and both are left stopped for
whoever started them to clear away.

Events are JSON, one per line, on stdout -- `podman logs` is the audit trail.
Everything else (startup, errors) goes to stderr.

    {"ts": ..., "sandbox": ..., "id": ..., "verdict": "allow" | "deny" | "fail" | "close",
     "kind": "connect" | "http" | "tls" | "tcp" | "dns" | "inbound", "peer": ..., "host": ..., "port": ...}

plus `method` and `path` for http, `qtype` for dns, `detail` on a deny or fail,
and `bytes_up`, `bytes_down`, `ms` on the close of a connection that was allowed
(same `id` as its allow). `via: "redirect"` marks a connection that did not use
the proxy, and `dialled` the address it was aimed at when that was not this
gateway. `tls` and `tcp` only ever arrive that way. `inbound` is a connection
passed in to the sandbox. A `request` is one HTTP
request inside a terminated TLS connection, with `method`, `path`, `status`,
and `injected` (the names of the headers added, never their values); the
connection carrying it has `intercepted: true`.

Shared (EGRESS_CONTROL set). One gateway for every sandbox in a namespace, as
in a cluster, where each sandbox is a pod and Calico lets sandbox pods reach
nothing but this gateway. A sandbox is known by the address its connections
come from. Nothing about any sandbox comes from the environment: the policy
settings above (EGRESS_ALLOW through EGRESS_UPSTREAM_CA) are ignored, and a
driver registers each sandbox once it has claimed it, over a control API on
port EGRESS_CONTROL that wants `Authorization: Bearer` EGRESS_CONTROL_TOKEN
(or the contents of EGRESS_CONTROL_TOKEN_FILE):

    PUT    /sandboxes/NAME   {"address": "10.42.1.7", "ttl": 3600,
                              "allow": ["github.com", "llm.lan:8080"], "ports": [443],
                              "mode": "allowlist", "inject": [...], "readonly": true,
                              "writes": [...], "upstream_ca": [...], "approvals": [...]}
    GET    /sandboxes/NAME/events?after=N
    DELETE /sandboxes/NAME   (answers with the events not yet read, as GET does)
    GET    /sandboxes        (names, addresses and expiry; never credentials)
    GET    /healthz          (no token)

`inject`, `writes` and `upstream_ca` take what EGRESS_INJECT, EGRESS_WRITES and
EGRESS_UPSTREAM_CA do, and `approvals` the entries of an approvals file. A PUT
replaces that sandbox's registration, and any other holding the same address.
An address nobody registered is refused everything; so is one whose `ttl` has
run out, which is how a driver that died without unregistering is cleaned up
after. Registrations live only in memory: after a restart every sandbox is
refused until its driver registers it again.

Its TLS is terminated with the CA in EGRESS_CA_CERT and EGRESS_CA_KEY (files),
so the CA outlives a restart and sandboxes can be told to trust it before any
one is claimed. Events still go to stdout, and each sandbox's are also kept
(the last EGRESS_EVENTS_KEPT, default 5000) for its driver to read.
"""

from __future__ import annotations

import asyncio
import collections
import fnmatch
import ipaddress
import itertools
import json
import os
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MODES = ("allowlist", "log")
MAX_HEAD = 65536
QTYPES = {1: "A", 2: "NS", 5: "CNAME", 12: "PTR", 15: "MX", 16: "TXT", 28: "AAAA", 33: "SRV", 65: "HTTPS"}


class ConfigError(ValueError):
    """A setting that cannot be used: fatal at start-up, a 400 from the control API."""


@dataclass(frozen=True)
class Policy:
    mode: str = "allowlist"
    # (host pattern, port); port 0 means any of `ports`.
    allow: tuple[tuple[str, int], ...] = ()
    ports: frozenset[int] = frozenset({443})

    @classmethod
    def from_env(cls, env: dict[str, str] | os._Environ[str] = os.environ) -> "Policy":
        return cls.build(
            env.get("EGRESS_MODE", "allowlist"),
            [h for h in env.get("EGRESS_ALLOW", "").split(",") if h.strip()],
            [p for p in env.get("EGRESS_PORTS", "443").split(",") if p.strip()],
        )

    @classmethod
    def build(cls, mode: object, allow: object, ports: object) -> "Policy":
        mode = str(mode).strip().lower()
        if mode not in MODES:
            raise ConfigError(f"EGRESS_MODE must be one of {', '.join(MODES)} (got {mode!r}).")
        if not isinstance(allow, list) or not isinstance(ports, list):
            raise ConfigError("allow and ports must be lists.")
        try:
            numbers = frozenset(int(str(p).strip()) for p in ports)
        except ValueError as err:
            raise ConfigError(f"ports must be numbers (got {ports!r}).") from err
        return cls(mode=mode, allow=tuple(allow_entry(str(h)) for h in allow), ports=numbers)

    def host_allowed(self, host: str) -> bool:
        """True if the host is allowed on some port."""
        return self.mode == "log" or any(fnmatch.fnmatchcase(host, pattern) for pattern, _ in self.allow)

    def allowed(self, host: str, port: int) -> bool:
        return self.mode == "log" or any(
            fnmatch.fnmatchcase(host, pattern) and (port == only if only else port in self.ports)
            for pattern, only in self.allow
        )


class Approvals:
    """Allowances added while running: from a file re-read whenever it changes,
    or (no path) the entries a shared gateway's sandbox was registered with."""

    def __init__(self, path: str, entries: object = ()) -> None:
        self.path = path
        self.stamp: tuple[int, int] | None = None
        self.entries: list[dict[str, object]] = self.parse(entries) if entries else []
        self.spent: set[str] = set()

    @staticmethod
    def parse(raw: object) -> list[dict[str, object]]:
        try:
            return [{**item, "match": allow_entry(str(item["allow"])), "key": str(item.get("id") or item["allow"])}
                    for item in raw]  # type: ignore[union-attr]
        except (KeyError, AttributeError, TypeError) as err:
            raise ConfigError(f"approvals must be a list of {{\"allow\": ...}} (got {raw!r}).") from err

    def load(self) -> None:
        if not self.path:
            return
        try:
            info = os.stat(self.path)
        except OSError:
            self.stamp, self.entries = None, []
            return
        stamp = (info.st_mtime_ns, info.st_size)
        if stamp == self.stamp:
            return
        self.stamp = stamp
        try:
            with open(self.path) as handle:
                self.entries = self.parse(json.load(handle).get("approvals", []))
        except (OSError, ValueError, AttributeError) as err:
            # A file that is half-written or wrong approves nothing new; what was read before stands.
            print(f"burst-gateway: ignoring {self.path}: {err}", file=sys.stderr, flush=True)

    def find(self, host: str, port: int | None, ports: frozenset[int]) -> dict[str, object] | None:
        """A live approval covering host (on port; any port when None)."""
        self.load()
        now = time.time()
        for entry in self.entries:
            pattern, only = entry["match"]  # type: ignore[misc]
            if entry.get("expires") and float(str(entry["expires"])) <= now:
                continue
            if entry.get("once") and entry["key"] in self.spent:
                continue
            if fnmatch.fnmatchcase(host, pattern) and (port is None or (port == only if only else port in ports)):
                return entry
        return None


def allow_entry(text: str) -> tuple[str, int]:
    """An EGRESS_ALLOW entry as (host pattern, port), port 0 when it names none."""
    host, sep, port = text.strip().lower().rpartition(":")
    if sep and port.isdigit() and ":" not in host:
        return host.rstrip("."), int(port)
    return text.strip().lower().rstrip("."), 0


@dataclass(frozen=True)
class Inject:
    """What to change on requests to `host` whose path matches `path`."""

    host: str
    # (name, value): set on every matching request.
    headers: tuple[tuple[str, str], ...] = ()
    path: str = "*"
    port: int = 443
    # (header, placeholder, value): where the header carries the placeholder, the value goes instead.
    swap: tuple[tuple[str, str, str], ...] = ()

    @classmethod
    def parse(cls, raw: str) -> tuple["Inject", ...]:
        return cls.from_items(_json_list(raw, "EGRESS_INJECT"))

    @classmethod
    def from_items(cls, items: list) -> tuple["Inject", ...]:
        rules = []
        try:
            for item in items:
                host = str(item["host"]).lower().rstrip(".")
                if not host or any(c in host for c in "*?[/"):
                    raise ConfigError(f"EGRESS_INJECT host must be one exact host name (got {host!r}).")
                headers = tuple((str(k), str(v)) for k, v in (item.get("headers") or {}).items())
                swap = tuple((str(w["header"]), str(w["placeholder"]), str(w["value"])) for w in item.get("swap") or [])
                if any(c in text for parts in (*headers, *swap) for text in parts for c in "\r\n"):
                    raise ConfigError("EGRESS_INJECT headers must be single lines.")
                if any(not placeholder for _, placeholder, _ in swap):
                    raise ConfigError("an EGRESS_INJECT swap needs a placeholder.")
                port = item.get("port") or 443
                if not isinstance(port, int) or not 0 < port < 65536:
                    raise ConfigError(f"EGRESS_INJECT port must be a port number (got {port!r}).")
                rules.append(cls(host=host, headers=headers, path=str(item.get("path") or "*"), swap=swap, port=port))
        except (KeyError, AttributeError, TypeError) as err:
            raise ConfigError(f"an EGRESS_INJECT rule is malformed: {err!r}") from err
        return tuple(rules)


def unclear_path(path: str) -> bool:
    """A path the server might read differently from how it is spelled here
    (dot segments, encoded dots and slashes): it could match a pattern and
    still name something outside it, so it matches no rule."""
    lowered = path.lower()
    return any(part in (".", "..") for part in path.split("/")) or \
        any(c in lowered for c in ("%2e", "%2f", "%5c", "\\", "//"))


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


READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Write:
    """A request that may write in read-only mode."""

    host: str
    method: str
    path: str = "*"

    @classmethod
    def parse(cls, raw: str) -> tuple["Write", ...]:
        return cls.from_items(_json_list(raw, "EGRESS_WRITES"))

    @classmethod
    def from_items(cls, items: list) -> tuple["Write", ...]:
        rules = []
        for item in items:
            if not isinstance(item, dict):
                raise ConfigError(f"an EGRESS_WRITES entry needs a host and a method (got {item!r}).")
            host, method = str(item.get("host") or "").lower().rstrip("."), str(item.get("method") or "").upper()
            if not host or not method or not method.isalpha():
                raise ConfigError(f"an EGRESS_WRITES entry needs a host and a method (got {item!r}).")
            rules.append(cls(host=host, method=method, path=str(item.get("path") or "*")))
        return tuple(rules)


READONLY_MESSAGE = ("This sandbox may only read: {method} is refused here, and approving the host will not "
                    "change that. Use a read-only way to get what you need, or say in your result that the job "
                    "needed to write.")


def upstream_cas(raw: str) -> dict[str, ssl.SSLContext]:
    """EGRESS_UPSTREAM_CA as {host: a context that verifies against that CA}."""
    return upstream_cas_from(_json_list(raw, "EGRESS_UPSTREAM_CA"))


def upstream_cas_from(items: list) -> dict[str, ssl.SSLContext]:
    found = {}
    for item in items:
        if not isinstance(item, dict):
            raise ConfigError(f"an EGRESS_UPSTREAM_CA entry needs a host and a PEM ca (got {item!r}).")
        host, pem = str(item.get("host") or "").lower().rstrip("."), str(item.get("ca") or "")
        if not host or "BEGIN CERTIFICATE" not in pem:
            raise ConfigError(f"an EGRESS_UPSTREAM_CA entry needs a host and a PEM ca (got host {host!r}).")
        try:
            found[host] = ssl.create_default_context(cadata=pem)
        except ssl.SSLError as err:
            raise ConfigError(f"EGRESS_UPSTREAM_CA for {host}: {err}") from err
    return found


def _json_list(raw: str, name: str) -> list:
    """An environment setting holding a JSON list ("" is an empty one)."""
    try:
        value = json.loads(raw) if raw.strip() else []
    except ValueError as err:
        raise ConfigError(f"{name} is not JSON: {err}") from err
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be a JSON list.")
    return value


EVENTS_KEPT = 5000


@dataclass(eq=False)
class Sandbox:
    """One sandbox: what its traffic is held to, and (shared) what it has done."""

    name: str = ""
    policy: Policy = field(default_factory=Policy)
    approvals: Approvals = field(default_factory=lambda: Approvals(""))
    inject: tuple[Inject, ...] = ()
    readonly: bool = False
    writes: tuple[Write, ...] = ()
    # host -> the context that verifies its certificate, for hosts with their own CA.
    upstream_ca: dict[str, ssl.SSLContext] = field(default_factory=dict)
    # Shared: the address its connections come from, and when its registration lapses.
    address: str = ""
    expires: float = 0.0
    # Shared: (sequence number, event), the newest EVENTS_KEPT; `recorded` counts them all.
    events: collections.deque = field(default_factory=lambda: collections.deque(maxlen=EVENTS_KEPT))
    recorded: int = 0

    @classmethod
    def from_env(cls, env: dict[str, str] | os._Environ[str] = os.environ) -> "Sandbox":
        return cls(
            name=env.get("EGRESS_SANDBOX", ""),
            policy=Policy.from_env(env),
            approvals=Approvals(env.get("EGRESS_APPROVALS", "/run/burst-gateway/approvals.json")),
            inject=Inject.parse(env.get("EGRESS_INJECT", "")),
            readonly=env.get("EGRESS_READONLY", "").strip().lower() in ("1", "true", "yes", "on"),
            writes=Write.parse(env.get("EGRESS_WRITES", "")),
            upstream_ca=upstream_cas(env.get("EGRESS_UPSTREAM_CA", "")),
        )

    @classmethod
    def register(cls, name: str, body: object, now: float) -> "Sandbox":
        """A sandbox as a shared gateway's control API is given it."""
        if not isinstance(body, dict):
            raise ConfigError("a registration is a JSON object.")
        if unknown := sorted(set(body) - REGISTRATION_KEYS):
            raise ConfigError(f"unknown registration keys: {', '.join(unknown)}.")
        try:
            address = str(ipaddress.ip_address(str(body.get("address") or "")))
        except ValueError as err:
            raise ConfigError(f"address must be the sandbox's IP address (got {body.get('address')!r}).") from err
        ttl = body.get("ttl", 0)
        if not isinstance(ttl, int) or ttl < 0:
            raise ConfigError(f"ttl must be a number of seconds (got {ttl!r}).")

        def items(key: str) -> list:
            value = body.get(key) or []
            if not isinstance(value, list):
                raise ConfigError(f"{key} must be a list.")
            return value

        return cls(
            name=name,
            policy=Policy.build(body.get("mode", "allowlist"), items("allow"), body.get("ports") or [443]),
            approvals=Approvals("", items("approvals")),
            inject=Inject.from_items(items("inject")),
            readonly=body.get("readonly") is True,
            writes=Write.from_items(items("writes")),
            upstream_ca=upstream_cas_from(items("upstream_ca")),
            address=address,
            expires=now + ttl if ttl else 0.0,
        )

    def permitted(self, host: str, port: int) -> bool:
        """The policy's answer for a connection: the allowlist, or an approval (a `once` is spent by this)."""
        if self.policy.allowed(host, port):
            return True
        if entry := self.approvals.find(host, port, self.policy.ports):
            if entry.get("once"):
                self.approvals.spent.add(str(entry["key"]))
            return True
        return False

    def resolvable(self, host: str) -> bool:
        """Whether a name may resolve at all: allowed, or approved, on some port. Spends nothing."""
        return self.policy.host_allowed(host) or self.approvals.find(host, None, self.policy.ports) is not None

    def inject_for(self, host: str, path: str, port: int = 443) -> Inject | None:
        """The first rule for this host whose path pattern matches (the query string is not part of it)."""
        path = path.split("?", 1)[0]
        if unclear_path(path):
            return None
        return next((r for r in self.inject if r.host == host and r.port == port and fnmatch.fnmatchcase(path, r.path)),
                    None)

    def may_send(self, host: str, method: str, path: str) -> bool:
        """Whether read-only mode lets this request through."""
        method = method.upper()
        if not self.readonly or method in READ_METHODS:
            return True
        path = path.split("?", 1)[0]
        # As for credentials: a path that might be read differently upstream matches no exception.
        if unclear_path(path):
            return False
        return any(w.method == method and fnmatch.fnmatchcase(host, w.host) and fnmatch.fnmatchcase(path, w.path)
                   for w in self.writes)

    def intercepts(self, host: str, port: int) -> bool:
        """Whether TLS to host:port is terminated here: credentials to add there, or read-only mode."""
        return CERTIFICATES is not None and (self.readonly or any(r.host == host and r.port == port for r in self.inject))

    def record(self, event: dict[str, object]) -> None:
        self.recorded += 1
        self.events.append((self.recorded, event))

    def events_after(self, after: int) -> dict[str, object]:
        """Events newer than `after`, and whether some were dropped before they could be read."""
        kept = [event for seq, event in self.events if seq > after]
        oldest = self.events[0][0] if self.events else self.recorded + 1
        return {"events": kept, "next": self.recorded, "dropped": max(0, oldest - after - 1)}


REGISTRATION_KEYS = frozenset({"address", "ttl", "mode", "allow", "ports", "inject", "readonly", "writes",
                               "upstream_ca", "approvals"})
NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-.")

# The one sandbox a gateway serves, when it is not shared.
SINGLE: Sandbox | None = None
# Shared: every registered sandbox, by name.
SANDBOXES: dict[str, Sandbox] = {}


def sandbox_for(peer: str) -> Sandbox | None:
    """Whose a connection from `peer` is: the one sandbox, or (shared) the live registration for that address."""
    if SINGLE is not None:
        return SINGLE
    now = time.time()
    for name, sandbox in list(SANDBOXES.items()):
        if sandbox.address == peer:
            if sandbox.expires and sandbox.expires <= now:
                lapse(name, now)
                return None
            return sandbox
    return None


def lapse(name: str, now: float) -> None:
    """Forget a registration whose ttl has run out (and its credentials with it)."""
    if (sandbox := SANDBOXES.get(name)) is not None and sandbox.expires and sandbox.expires <= now:
        del SANDBOXES[name]
        emit("sandbox", "expire", name=name, address=sandbox.address)


async def intercept_port(host: str) -> int:
    """The loopback port where TLS for `host` is terminated, started if need be."""
    async with _LISTENER_LOCK:
        assert CERTIFICATES is not None
        context = CERTIFICATES.context(host)  # renews a certificate that is due, in place
        if host not in _LISTENERS:
            server = await asyncio.start_server(handle_intercepted, "127.0.0.1", 0, ssl=context)
            _LISTENERS[host] = server.sockets[0].getsockname()[1]
        return _LISTENERS[host]


WEBHOOK = ""
CERTIFICATES: "Certificates | None" = None
REDIRECT = 0
# Where TLS is terminated for each host: a listener on loopback, serving that
# host's certificate, that connections are relayed into whichever way they
# arrived. One per host, made on first use, so the certificate never depends on
# what the client names (no SNI is sent for an IP address). Shared by every
# sandbox: which one a relayed connection belongs to travels in _RELAYED.
_LISTENERS: dict[str, int] = {}
_LISTENER_LOCK = asyncio.Lock()
# What is known about a connection relayed there (whose, which host, how it
# arrived), by the local port it was relayed from.
_RELAYED: dict[int, dict[str, object]] = {}
_RUN = secrets.token_hex(3)
_SEQ = itertools.count(1)


def next_id() -> str:
    return f"{_RUN}-{next(_SEQ)}"


_ACTIVE = time.monotonic()


# When each destination was last reported to the webhook: an agent retrying a
# refused host should not page anyone every time.
_NOTIFIED: dict[tuple[object, object], float] = {}
NOTIFY_EVERY = 300


def should_notify(event: dict[str, object], now: float) -> bool:
    """True for a refusal of a destination not reported in the last few minutes."""
    if event.get("verdict") != "deny" or "id" not in event:
        return False
    key = (event.get("host"), event.get("port"))
    if now - _NOTIFIED.get(key, -NOTIFY_EVERY) < NOTIFY_EVERY:
        return False
    _NOTIFIED[key] = now
    return True


def notify(event: dict[str, object]) -> None:
    """POST a refusal to the webhook, off the event loop and without waiting on it."""
    def post() -> None:
        body = json.dumps({**event, "approve": f"harness egress approve {event['id']}"}).encode()
        request = urllib.request.Request(WEBHOOK, data=body, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=10).close()
        except (OSError, ValueError) as err:
            print(f"burst-gateway: webhook failed: {err}", file=sys.stderr, flush=True)

    threading.Thread(target=post, daemon=True).start()


def emit(kind: str, verdict: str, *, sandbox: Sandbox | None = None, **fields: object) -> None:
    """Record an event: on stdout, and (shared) with the sandbox it belongs to."""
    global _ACTIVE
    if verdict != "close":
        _ACTIVE = time.monotonic()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    event = {"ts": stamp, "sandbox": sandbox.name if sandbox else "", "kind": kind, "verdict": verdict, **fields}
    event = {k: v for k, v in event.items() if v not in ("", None)}
    print(json.dumps(event), flush=True)
    if sandbox is not None and sandbox is not SINGLE:
        sandbox.record(event)
    if WEBHOOK and should_notify(event, time.monotonic()):
        notify(event)


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


UNREGISTERED_MESSAGE = ("This address is not a sandbox this gateway knows (or its registration has run out), so "
                        "nothing is let through for it.")


async def deny(writer: asyncio.StreamWriter, kind: str, detail: str = "", *, message: str = "", error: str = "",
               sandbox: Sandbox | None = None, **fields: object) -> None:
    """Refuse at once -- never hold a connection open -- and say why in a body an agent can read."""
    request_id = next_id()
    emit(kind, "deny", sandbox=sandbox, id=request_id, detail=detail, **fields)
    if kind not in ("connect", "http", "request"):
        return writer.close()  # not speaking HTTP: there is no way to say why
    await refuse(writer, "403 Forbidden", {
        "error": error or ("read_only" if message else "egress_denied"),
        "request_id": request_id,
        "host": fields.get("host"),
        "port": fields.get("port"),
        "message": message or "Blocked by this sandbox's egress policy, and recorded for whoever runs it to approve "
                   f"(`harness egress approve {request_id}`). Retrying will not help until they do: carry on "
                   "with what does not need this host, or say that you are waiting on it.",
    })


async def relay(
    kind: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, sandbox: Sandbox | None,
    to_upstream: bytes = b"", to_client: bytes = b"", **fields: object,
) -> None:
    """Connect to host:port and pipe both ways, recording the allow and the close.

    TLS to a host with credentials to inject goes to the intercept listener
    instead, which makes the onward connection itself.
    """
    request_id, started = next_id(), time.monotonic()
    host, port = str(fields["host"]), int(str(fields["port"]))
    intercept = kind in ("connect", "tls") and sandbox is not None and sandbox.intercepts(host, port)
    if intercept:
        fields["intercepted"] = True
    try:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", await intercept_port(host)) if intercept
            else asyncio.open_connection(host, port), 15
        )
    except (OSError, asyncio.TimeoutError, ValueError, SystemExit) as err:
        emit(kind, "fail", sandbox=sandbox, id=request_id, detail=str(err) or type(err).__name__, **fields)
        if kind not in ("connect", "http"):
            return writer.close()
        return await refuse(writer, "502 Bad Gateway")
    emit(kind, "allow", sandbox=sandbox, id=request_id, **fields)
    local_port = up_writer.get_extra_info("sockname")[1]
    if intercept:
        _RELAYED[local_port] = dict(host=host, port=port, peer=fields.get("peer"), via=fields.get("via"),
                                    connection=request_id, sandbox=sandbox)
    up_writer.write(to_upstream)
    writer.write(to_client)
    await writer.drain()
    try:
        sent, received = await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))
    finally:
        _RELAYED.pop(local_port, None)
    emit(kind, "close", sandbox=sandbox, id=request_id, bytes_up=sent + len(to_upstream), bytes_down=received,
         ms=int((time.monotonic() - started) * 1000), **fields)


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = str((writer.get_extra_info("peername") or ("?",))[0])
    try:
        head = await read_head(reader)
        if not head:
            return writer.close()
        method, target, version = (head[0].decode("latin-1").strip().split(" ", 2) + ["", ""])[:3]
        sandbox = sandbox_for(peer)
        if method.upper() == "CONNECT":
            host, port = parse_connect(target)
            if sandbox is None:
                return await deny(writer, "connect", "unregistered", message=UNREGISTERED_MESSAGE,
                                  error="unregistered", peer=peer, host=host, port=port)
            if not sandbox.permitted(host, port):
                return await deny(writer, "connect", sandbox=sandbox, peer=peer, host=host, port=port)
            return await relay("connect", reader, writer, sandbox=sandbox, peer=peer, host=host, port=port,
                               to_client=b"HTTP/1.1 200 Connection established\r\n\r\n")
        parsed = parse_http(target)
        if parsed is None:
            return await deny(writer, "http", "not a proxy request", sandbox=sandbox, peer=peer, method=method or "-",
                              host=target or "-")
        host, port, path = parsed
        # The query string is not recorded: it is where tokens end up.
        fields = dict(peer=peer, host=host, port=port, method=method, path=path.split("?", 1)[0])
        if sandbox is None:
            return await deny(writer, "http", "unregistered", message=UNREGISTERED_MESSAGE, error="unregistered",
                              **fields)
        if not sandbox.permitted(host, port):
            return await deny(writer, "http", sandbox=sandbox, **fields)
        if not sandbox.may_send(host, method, path):
            return await deny(writer, "http", "read-only", message=READONLY_MESSAGE.format(method=method),
                              sandbox=sandbox, **fields)
        await relay("http", reader, writer, sandbox=sandbox,
                    to_upstream=origin_request(method, path, version, head[1:]), **fields)
    except (asyncio.TimeoutError, ConnectionError, OSError, ValueError):
        writer.close()


# --- inbound ---


def parse_inbound(text: str) -> dict[int, tuple[str, int]]:
    """EGRESS_INBOUND as {listening port: (address, port)}."""
    routes = {}
    for item in filter(None, (part.strip() for part in text.split(","))):
        listen, _, target = item.partition("=")
        address, _, port = target.rpartition(":")
        if not (listen.isdigit() and port.isdigit() and address):
            raise SystemExit(f"burst-gateway: EGRESS_INBOUND entries are PORT=ADDRESS:PORT (got {item!r}).")
        routes[int(listen)] = (address, int(port))
    return routes


def inbound_handler(address: str, port: int):
    async def handle_inbound(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = str((writer.get_extra_info("peername") or ("?",))[0])
        try:
            await relay("inbound", reader, writer, sandbox=SINGLE, peer=peer, host=address, port=port)
        except (asyncio.TimeoutError, ConnectionError, OSError, ValueError):
            writer.close()

    return handle_inbound


# --- terminated TLS: credentials ---


class Certificates:
    """A CA, and a certificate under it for each host (kept in `directory`),
    made when first asked for. The CA is made in `directory` too, unless one
    is given (`ca`: certificate and key files), as a shared gateway's is."""

    def __init__(self, directory: Path, *, name: str = "", ca: tuple[Path, Path] | None = None) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.contexts: dict[str, ssl.SSLContext] = {}
        self.made: dict[str, float] = {}
        if ca is not None:
            self.ca_crt, self.ca_key = ca
            return
        self.ca_key, self.ca_crt = directory / "ca.key", directory / "ca.crt"
        self._openssl("req", "-x509", *self.KEY_ARGS, "-keyout", str(self.ca_key), "-out", str(self.ca_crt),
                      "-days", "30", "-subj", f"/CN=burst-gateway {name or 'sandbox'}",
                      "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
                      "-addext", "keyUsage=critical,keyCertSign")
        self.ca_key.chmod(0o600)

    KEY_ARGS = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes")
    # A host's certificate is signed for 30 days and made again after 20: a
    # shared gateway runs for longer than that.
    RENEW_AFTER = 20 * 86400

    @staticmethod
    def _openssl(*args: str, stdin: bytes | None = None) -> bytes:
        proc = subprocess.run(["openssl", *args], input=stdin, capture_output=True)
        if proc.returncode != 0:
            raise SystemExit(f"burst-gateway: openssl {args[0]} failed: {proc.stderr.decode(errors='replace').strip()}")
        return proc.stdout

    def context(self, host: str) -> ssl.SSLContext:
        """The server context for `host`. Once its certificate is due, the same
        context takes a new one, so a listener already serving it need not change."""
        found = self.contexts.get(host)
        if found is not None and time.time() - self.made[host] < self.RENEW_AFTER:
            return found
        if not host or len(host) > 253 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-." for c in host):
            raise ValueError(f"not a host name: {host!r}")
        try:
            name = f"IP:{ipaddress.ip_address(host)}"
        except ValueError:
            name = f"DNS:{host}"
        key, crt, ext = (self.directory / f"{host}.{suffix}" for suffix in ("key", "crt", "ext"))
        ext.write_text(f"subjectAltName={name}\nextendedKeyUsage=serverAuth\n")
        request = self._openssl("req", "-new", *self.KEY_ARGS, "-keyout", str(key), "-subj", f"/CN={host}")
        self._openssl("x509", "-req", "-CA", str(self.ca_crt), "-CAkey", str(self.ca_key), "-days", "30",
                      "-extfile", str(ext), "-out", str(crt), stdin=request)
        context = found or ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(crt), str(key))
        # One request per connection, parsed as HTTP/1.1: do not offer h2.
        context.set_alpn_protocols(["http/1.1"])
        self.contexts[host], self.made[host] = context, time.time()
        return context


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
    host, port = str(known["host"]), int(str(known.get("port") or 443))
    sandbox: Sandbox = known["sandbox"]  # type: ignore[assignment]
    request_id, started = next_id(), time.monotonic()
    fields: dict[str, object] = dict(id=request_id, peer=known["peer"], host=host, port=port, via=known["via"],
                                     connection=known["connection"])
    try:
        head = await read_head(reader)
        if not head:
            return writer.close()
        method, target, version = (head[0].decode("latin-1").strip().split(" ", 2) + ["", ""])[:3]
        fields.update(method=method, path=target.split("?", 1)[0])
        named = http_host(b"".join(head))
        if named != host:
            emit("request", "deny", sandbox=sandbox, detail=f"Host header names {named or 'nothing'}", **fields)
            return await refuse(writer, "421 Misdirected Request")
        if not sandbox.may_send(host, method, target):
            fields.pop("id")
            return await deny(writer, "request", "read-only", message=READONLY_MESSAGE.format(method=method),
                              sandbox=sandbox, **fields)
        rule = sandbox.inject_for(host, target, port)
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=sandbox.upstream_ca.get(host) or ssl.create_default_context(),
                                        server_hostname=host), 15
            )
        except (OSError, asyncio.TimeoutError) as err:
            emit("request", "fail", sandbox=sandbox, detail=str(err) or type(err).__name__, **fields)
            return await refuse(writer, "502 Bad Gateway")
        headers, injected = with_credentials(head[1:], rule)
        up_writer.write(origin_request(method, target, version, headers))
        sent, (status, received) = await asyncio.gather(pipe(reader, up_writer), pipe_response(up_reader, writer))
        emit("request", "allow", sandbox=sandbox, status=status or None, injected=injected or None,
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
        sandbox = sandbox_for(peer)
        if sandbox is None:
            return await deny(writer, kind, "unregistered", message=UNREGISTERED_MESSAGE, error="unregistered",
                              host=host or "-", **fields)
        if not host:
            return await deny(writer, kind, "no server name to identify the destination", sandbox=sandbox, host="-",
                              **fields)
        if not sandbox.permitted(host, port):
            return await deny(writer, kind, sandbox=sandbox, host=host, **fields)
        if kind == "http" and not sandbox.may_send(host, str(fields["method"]), str(fields["path"])):
            return await deny(writer, kind, "read-only", sandbox=sandbox, host=host, **fields)
        if kind == "tcp" and sandbox.readonly:
            return await deny(writer, kind, "read-only: neither TLS nor HTTP", sandbox=sandbox, host=host, **fields)
        await relay(kind, reader, writer, sandbox=sandbox, to_upstream=opening, host=host, **fields)
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


def dns_answer(packet: bytes, here: str, sandbox: Sandbox | None) -> tuple[bytes, dict[str, object]]:
    """The reply to a query and the event recording it. `here` is this gateway's
    address as the asker sees it, or "" when connections are not redirected."""
    name, qtype, end = dns_question(packet)
    fields: dict[str, object] = dict(host=name, qtype=QTYPES.get(qtype, str(qtype)))
    if not here:
        return dns_reply(packet, end, rcode=3), dict(
            fields, verdict="deny", detail="no DNS in the sandbox; this lookup did not go through the proxy")
    if sandbox is None:
        return dns_reply(packet, end, rcode=3), dict(fields, verdict="deny", detail="unregistered")
    if not sandbox.resolvable(name):
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
            sandbox = sandbox_for(addr[0])
            reply, event = dns_answer(data, address_facing(addr[0]) if REDIRECT else "", sandbox)
        except (ValueError, IndexError, struct.error, OSError):
            return
        emit("dns", str(event.pop("verdict")), sandbox=sandbox, id=next_id(), peer=addr[0], **event)
        self.transport.sendto(reply, addr)  # type: ignore[attr-defined]


# --- control (shared) ---

CONTROL_TOKEN = ""
MAX_BODY = 1 << 20


async def respond(writer: asyncio.StreamWriter, status: str, body: object) -> None:
    payload = json.dumps(body).encode()
    writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
                 "Connection: close\r\n\r\n".encode() + payload)
    await writer.drain()
    writer.close()


def authorized(head: list[bytes]) -> bool:
    for line in head[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"authorization":
            scheme, _, token = value.strip().partition(b" ")
            return scheme.lower() == b"bearer" and secrets.compare_digest(token.strip(), CONTROL_TOKEN.encode())
    return False


def content_length(head: list[bytes]) -> int:
    for line in head[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            return int(value.strip())
    return 0


def control(method: str, path: str, query: dict[str, list[str]], body: object, now: float) -> tuple[str, object]:
    """A control API request's status and answer. Not async: nothing here waits."""
    parts = [part for part in path.split("/") if part]
    if parts == ["sandboxes"] and method == "GET":
        for name in list(SANDBOXES):
            lapse(name, now)
        return "200 OK", {"sandboxes": [{"name": s.name, "address": s.address, "expires": s.expires or None,
                                         "events": s.recorded} for s in SANDBOXES.values()]}
    if len(parts) not in (2, 3) or parts[0] != "sandboxes":
        return "404 Not Found", {"error": "not_found"}
    name = parts[1]
    if not name or len(name) > 253 or set(name) - NAME_CHARS:
        return "400 Bad Request", {"error": "bad_name", "message": "a name is lowercase letters, digits, '-' and '.'"}
    try:
        after = int((query.get("after") or ["0"])[0])
    except ValueError:
        return "400 Bad Request", {"error": "bad_after"}
    if len(parts) == 3:
        if parts[2] != "events" or method != "GET":
            return "404 Not Found", {"error": "not_found"}
        if (sandbox := SANDBOXES.get(name)) is None:
            return "404 Not Found", {"error": "not_registered"}
        return "200 OK", sandbox.events_after(after)
    if method == "PUT":
        try:
            sandbox = Sandbox.register(name, body, now)
        except ConfigError as err:
            return "400 Bad Request", {"error": "bad_registration", "message": str(err)}
        for other in [n for n, s in SANDBOXES.items() if s.address == sandbox.address and n != name]:
            del SANDBOXES[other]
            emit("sandbox", "replace", name=other, address=sandbox.address, detail=f"address now {name}'s")
        SANDBOXES[name] = sandbox
        emit("sandbox", "register", sandbox=sandbox, address=sandbox.address,
             expires=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(sandbox.expires)) if sandbox.expires else None)
        return "200 OK", {"name": name, "address": sandbox.address, "expires": sandbox.expires or None}
    if method == "DELETE":
        if (sandbox := SANDBOXES.pop(name, None)) is None:
            return "404 Not Found", {"error": "not_registered"}
        emit("sandbox", "unregister", name=name, address=sandbox.address)
        return "200 OK", sandbox.events_after(after)
    return "405 Method Not Allowed", {"error": "method_not_allowed"}


async def handle_control(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        head = await read_head(reader)
        if not head:
            return writer.close()
        method, target = (head[0].decode("latin-1").strip().split(" ") + [""])[:2]
        parts = urlsplit(target)
        if parts.path == "/healthz":
            return await respond(writer, "200 OK", {"ok": True, "sandboxes": len(SANDBOXES)})
        if not authorized(head):
            return await respond(writer, "401 Unauthorized", {"error": "unauthorized"})
        length = content_length(head)
        if not 0 <= length <= MAX_BODY:
            return await respond(writer, "413 Content Too Large", {"error": "too_large"})
        raw = await asyncio.wait_for(reader.readexactly(length), 30) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            return await respond(writer, "400 Bad Request", {"error": "bad_json"})
        status, answer = control(method.upper(), parts.path, parse_qs(parts.query), body, time.time())
        await respond(writer, status, answer)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError, ValueError):
        writer.close()


async def prune() -> None:
    """Forget lapsed registrations even when nothing comes from their addresses."""
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for name in list(SANDBOXES):
            lapse(name, now)


def read_token(env: dict[str, str] | os._Environ[str]) -> str:
    if path := env.get("EGRESS_CONTROL_TOKEN_FILE", ""):
        try:
            return Path(path).read_text().strip()
        except OSError as err:
            raise ConfigError(f"EGRESS_CONTROL_TOKEN_FILE: {err}") from err
    return env.get("EGRESS_CONTROL_TOKEN", "").strip()


def expired(now: float, started: float, active: float, idle: int, lifetime: int) -> str:
    """Why the gateway should stop now, or "" if it should not."""
    if lifetime and now - started >= lifetime:
        return f"lifetime of {lifetime}s reached"
    if idle and now - active >= idle:
        return f"idle for {idle}s"
    return ""


async def watch(idle: int, lifetime: int) -> None:
    started = time.monotonic()
    while not (reason := expired(time.monotonic(), started, _ACTIVE, idle, lifetime)):
        await asyncio.sleep(5)
    emit("gateway", "exit", detail=reason)
    print(f"burst-gateway: stopping: {reason}", file=sys.stderr, flush=True)
    os._exit(0)


async def main() -> None:
    global SINGLE, WEBHOOK, REDIRECT, CERTIFICATES, CONTROL_TOKEN, EVENTS_KEPT
    env = os.environ
    control_port = int(env.get("EGRESS_CONTROL", "0") or 0)
    WEBHOOK = env.get("EGRESS_WEBHOOK", "")
    listen = int(env.get("EGRESS_LISTEN", "3128"))
    dns_port = int(env.get("EGRESS_DNS", "53"))
    REDIRECT = int(env.get("EGRESS_REDIRECT", "0"))
    ca_dir = Path(env.get("EGRESS_CA_DIR", "/run/burst-gateway"))
    try:
        if control_port:
            CONTROL_TOKEN = read_token(env)
            if len(CONTROL_TOKEN) < 16:
                raise ConfigError("a shared gateway (EGRESS_CONTROL) needs EGRESS_CONTROL_TOKEN or "
                                  "EGRESS_CONTROL_TOKEN_FILE, of 16 characters or more.")
            ca_cert, ca_key = env.get("EGRESS_CA_CERT", ""), env.get("EGRESS_CA_KEY", "")
            if not (ca_cert and ca_key):
                raise ConfigError("a shared gateway needs EGRESS_CA_CERT and EGRESS_CA_KEY, the CA its sandboxes trust.")
            EVENTS_KEPT = int(env.get("EGRESS_EVENTS_KEPT", str(EVENTS_KEPT)))
            CERTIFICATES = Certificates(ca_dir, ca=(Path(ca_cert), Path(ca_key)))
            CERTIFICATES.context("burst-gateway.invalid")  # a CA that cannot sign fails here, not on first use
            ignored = [k for k in ("EGRESS_ALLOW", "EGRESS_INJECT", "EGRESS_READONLY", "EGRESS_WRITES",
                                   "EGRESS_UPSTREAM_CA", "EGRESS_INBOUND") if env.get(k)]
            if ignored:
                print(f"burst-gateway: shared: ignoring {', '.join(ignored)}; sandboxes are registered",
                      file=sys.stderr, flush=True)
        else:
            SINGLE = Sandbox.from_env(env)
    except ConfigError as err:
        raise SystemExit(f"burst-gateway: {err}") from err
    loop = asyncio.get_running_loop()
    if dns_port:
        await loop.create_datagram_endpoint(Resolver, local_addr=("0.0.0.0", dns_port))
    if REDIRECT:
        await asyncio.start_server(handle_redirected, "0.0.0.0", REDIRECT)
    tasks = []
    if SINGLE is not None:
        if SINGLE.policy.mode == "allowlist" and not SINGLE.policy.allow:
            print("burst-gateway: EGRESS_ALLOW is empty; everything will be refused.", file=sys.stderr)
        for port, (address, target_port) in parse_inbound(env.get("EGRESS_INBOUND", "")).items():
            await asyncio.start_server(inbound_handler(address, target_port), "0.0.0.0", port)
            print(f"burst-gateway: inbound :{port} -> {address}:{target_port}", file=sys.stderr, flush=True)
        if SINGLE.inject or SINGLE.readonly:
            try:
                CERTIFICATES = Certificates(ca_dir, name=SINGLE.name)
                hosts = sorted({rule.host for rule in SINGLE.inject})
                for host in hosts:  # made now, so a bad host name fails at start-up
                    CERTIFICATES.context(host)
            except ValueError as err:
                raise SystemExit(f"burst-gateway: {err}") from err
            if hosts:
                print(f"burst-gateway: adding credentials for {', '.join(hosts)}", file=sys.stderr, flush=True)
            if SINGLE.readonly:
                writes = ", ".join(f"{w.method} {w.host}{w.path}" for w in SINGLE.writes) or "none"
                print(f"burst-gateway: read-only; writes allowed: {writes}", file=sys.stderr, flush=True)
        policy = SINGLE.policy
        what = "everything (log only)" if policy.mode == "log" else \
            f"ports {sorted(policy.ports)}; allow {', '.join(h + (f':{p}' if p else '') for h, p in policy.allow) or '(nothing)'}"
    else:
        await asyncio.start_server(handle_control, "0.0.0.0", control_port)
        tasks.append(asyncio.create_task(prune()))
        what = f"shared: sandboxes registered on control :{control_port}"
    server = await asyncio.start_server(handle, "0.0.0.0", listen)
    print(f"burst-gateway: proxy :{listen}, dns :{dns_port or 'off'}, redirected :{REDIRECT or 'off'}; {what}",
          file=sys.stderr, flush=True)
    idle, lifetime = int(env.get("EGRESS_IDLE", "0")), int(env.get("EGRESS_LIFETIME", "0"))
    if idle or lifetime:
        tasks.append(asyncio.create_task(watch(idle, lifetime)))
    async with server:  # `tasks` is held here so they are not collected while the server runs
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
