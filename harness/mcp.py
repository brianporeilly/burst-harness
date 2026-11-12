"""MCP servers the agent connects to: remote ones, over streamable HTTP, such as
Context7's (https://mcp.context7.com/mcp).

`mcp` lists them as NAME=URL. Each engine is told about them its own way:

- opencode (sessions and jobs): an `mcp` section in the opencode.json the
  harness writes.
- Claude Code: `--mcp-config` on every `claude -p`, with the servers' tools
  allowed (`--allowedTools mcp__NAME`) since nobody is there to approve them;
  a session's is added to ~/.claude.json, and asks as usual.
- strands: Strands' MCPClient, from the driver, where it calls the model too.

The first two connect from the sandbox, so each server's host goes on the
gateway's allowlist, and in a report-only job its POSTs (MCP's requests) are let
through to the server's URL: a server is configured by the user, like the
model.

A server's key is one of the driver's credentials, and which one says how it is
sent (headers(), the one place they are made, for every engine):

- <NAME>_API_KEY (CONTEXT7_API_KEY): `Authorization: Bearer <key>`.
- <NAME>_BASIC_AUTH, as user:secret (ATLASSIAN_BASIC_AUTH=you@example.com:token):
  `Authorization: Basic <base64(user:secret)>`, encoded here. Split at the
  first colon, as servers do, so the secret may hold more.

Both for one server is refused, as is a BASIC_AUTH without a colon. The key is
added by the gateway on the way out, so the sandbox never holds it, or sent by
MCPClient itself for strands. A sandbox engine with no gateway (a container
with egress open, a claimed sandbox with no gateway_control, the local and
kubectl targets) is given the header itself instead (direct()): it is in the
engine's MCP config, in a file only the sandbox's user can read, for the run.

For strands alone, a server on this machine's loopback may also be plain
http:// (127.0.0.1, ::1, localhost): MCPClient reaches it from the driver, with
no gateway between, so there is nothing for HTTPS to pass. A local test server,
for one. The sandbox engines keep to https://.
"""

from __future__ import annotations

import base64
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from . import credentials

if TYPE_CHECKING:  # config checks the entries with servers(), so this module cannot import it
    from .config import HarnessConfig

NAME = re.compile(r"[A-Za-z0-9_-]+")
LOOPBACK = ("127.0.0.1", "::1", "localhost")


class MCPError(ValueError):
    """An `mcp` entry that cannot be used."""


def servers(cfg: HarnessConfig) -> dict[str, str]:
    """`mcp` as {name: url}, checked: a plain name, an https:// URL (or for strands, http:// on loopback),
    each name once."""
    found: dict[str, str] = {}
    for entry in cfg.mcp:
        name, sep, url = entry.partition("=")
        name, url = name.strip(), url.strip()
        if not sep or not NAME.fullmatch(name):
            raise MCPError(f"An mcp entry is NAME=URL, with a name of letters, digits, - and _ (got {entry!r}).")
        parts = urlsplit(url)
        local = not in_sandbox(cfg) and parts.scheme == "http" and parts.hostname in LOOPBACK
        if not (parts.scheme == "https" and parts.hostname or local):
            raise MCPError(f"mcp {name}: the gateway only passes HTTPS; {url!r} is not an https:// URL "
                           "(http:// is for engine=strands, to 127.0.0.1, ::1 or localhost).")
        if name in found:
            raise MCPError(f"mcp {name} is listed twice.")
        found[name] = url
    return found


def key_var(name: str) -> str:
    """The driver's variable holding a server's key: CONTEXT7_API_KEY for context7."""
    return re.sub(r"[^A-Z0-9_]", "_", name.upper()) + "_API_KEY"


def basic_var(name: str) -> str:
    """The driver's variable holding a server's user:secret for Basic auth: ATLASSIAN_BASIC_AUTH for atlassian."""
    return re.sub(r"[^A-Z0-9_]", "_", name.upper()) + "_BASIC_AUTH"


def key_vars(name: str) -> tuple[str, str]:
    """Both of a server's credentials, for granting a run whichever is set."""
    return key_var(name), basic_var(name)


def auth(name: str) -> str:
    """How a server's key is sent: "bearer", "basic", or "" with none. Never raises: for status lines, where
    a key that cannot be used is said by problem()."""
    if credentials.get(key_var(name)):
        return "bearer"
    return "basic" if credentials.get(basic_var(name)) else ""


def problem(name: str) -> str:
    """Why a server's key cannot be used, or ""."""
    bearer, basic = credentials.get(key_var(name)), credentials.get(basic_var(name))
    if bearer and basic:
        return (f"mcp {name}: both {key_var(name)} and {basic_var(name)} are set; keep the one the server takes "
                "(a bearer token, or Basic's user:secret).")
    if basic and ":" not in basic:
        return f"mcp {name}: {basic_var(name)} is user:secret (say you@example.com:token), not encoded: it has no colon."
    return ""


def headers(name: str) -> dict[str, str]:
    """The headers that carry a server's key, if the driver has one. Raises MCPError for one that cannot be used."""
    if found := problem(name):
        raise MCPError(found)
    if key := credentials.get(key_var(name)):
        return {"Authorization": f"Bearer {key}"}
    if pair := credentials.get(basic_var(name)):
        return {"Authorization": "Basic " + base64.b64encode(pair.encode()).decode("ascii")}
    return {}


def check_keys(cfg: HarnessConfig) -> None:
    """Every server's key usable, before a run starts anything. Raises MCPError."""
    for name in servers(cfg):
        headers(name)


def through_gateway(cfg: HarnessConfig) -> bool:
    """Whether a sandbox engine's requests pass a gateway that adds the keys: a container with egress log or
    allowlist, or a claimed sandbox registered with a shared gateway."""
    if cfg.target in ("podman", "docker"):
        return cfg.egress != "open"
    return cfg.target == "agent-sandbox" and bool(cfg.gateway_control)


def direct(cfg: HarnessConfig) -> dict[str, dict[str, str]]:
    """{name: headers} a sandbox engine sends itself, with no gateway to add them; {} otherwise."""
    if not in_sandbox(cfg) or through_gateway(cfg):
        return {}
    return {name: found for name in servers(cfg) if (found := headers(name))}


def in_sandbox(cfg: HarnessConfig) -> bool:
    """Whether the engine connects from the sandbox (everything but strands, which is on the driver)."""
    return cfg.engine != "strands"


def _host(url: str) -> str:
    parts = urlsplit(url)
    port = parts.port or 443
    return parts.hostname if port == 443 else f"{parts.hostname}:{port}"  # type: ignore[return-value]


def _path(url: str) -> str:
    # Any query on the server's own URL, and nothing else on the host.
    return (urlsplit(url).path or "/") + "*"


def hosts(cfg: HarnessConfig) -> tuple[str, ...]:
    """Allowlist entries for the servers, when the engine reaches them from the sandbox."""
    return tuple(dict.fromkeys(_host(url) for url in servers(cfg).values())) if in_sandbox(cfg) else ()


def inject_rules(cfg: HarnessConfig) -> list[dict[str, object]]:
    """Gateway rules adding each server's key on requests to its URL (sandbox engines only)."""
    if not in_sandbox(cfg):
        return []
    return [{"host": urlsplit(url).hostname, **({"port": port} if (port := urlsplit(url).port) else {}),
             "path": _path(url), "headers": found}
            for name, url in servers(cfg).items() if (found := headers(name))]


def writes(cfg: HarnessConfig) -> tuple[dict[str, str], ...]:
    """What a report-only sandbox may send the servers: MCP's requests are POSTs to their URL."""
    if not in_sandbox(cfg):
        return ()
    return tuple({"host": urlsplit(url).hostname or "", "method": method, "path": _path(url)}
                 for url in servers(cfg).values() for method in ("POST", "DELETE"))


def opencode(cfg: HarnessConfig) -> dict[str, Any]:
    """opencode.json's `mcp` section, with the keys' headers when there is no gateway to add them (direct())."""
    keys = direct(cfg)
    return {name: {"type": "remote", "url": url, "enabled": True, **({"headers": keys[name]} if name in keys else {})}
            for name, url in servers(cfg).items()}


def claude(cfg: HarnessConfig, *, keys: bool = True) -> dict[str, Any]:
    """Claude Code's `mcpServers`, as --mcp-config and ~/.claude.json take it, with the keys' headers when there
    is no gateway to add them (direct()); never with keys=False, for a command line."""
    keys = direct(cfg) if keys else {}
    return {"mcpServers": {name: {"type": "http", "url": url, **({"headers": keys[name]} if name in keys else {})}
                           for name, url in servers(cfg).items()}}


def claude_tools(cfg: HarnessConfig) -> list[str]:
    """--allowedTools entries for every tool of every server."""
    return [f"mcp__{name}" for name in servers(cfg)]


def clients(cfg: HarnessConfig) -> list[Any]:
    """Strands MCPClients for the servers, for engine=strands. A server that is down
    leaves its tools out rather than ending the run."""
    if not (found := servers(cfg)):
        return []
    from strands.tools.mcp import MCPClient

    return [MCPClient(url=url, headers=headers(name) or None, prefix=name, continue_on_error=True,
                      application_name="burst-harness")
            for name, url in found.items()]
