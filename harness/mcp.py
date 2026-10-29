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
model. A key the driver holds as <NAME>_API_KEY (CONTEXT7_API_KEY) is sent as a
bearer token: added by the gateway on the way out, so the sandbox never holds
it, or sent by MCPClient itself for strands. Without a gateway (egress open) the
server is used without it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from . import credentials

if TYPE_CHECKING:  # config checks the entries with servers(), so this module cannot import it
    from .config import HarnessConfig

NAME = re.compile(r"[A-Za-z0-9_-]+")


class MCPError(ValueError):
    """An `mcp` entry that cannot be used."""


def servers(cfg: HarnessConfig) -> dict[str, str]:
    """`mcp` as {name: url}, checked: a plain name, an https:// URL, each name once."""
    found: dict[str, str] = {}
    for entry in cfg.mcp:
        name, sep, url = entry.partition("=")
        name, url = name.strip(), url.strip()
        if not sep or not NAME.fullmatch(name):
            raise MCPError(f"An mcp entry is NAME=URL, with a name of letters, digits, - and _ (got {entry!r}).")
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise MCPError(f"mcp {name}: the gateway only passes HTTPS; {url!r} is not an https:// URL.")
        if name in found:
            raise MCPError(f"mcp {name} is listed twice.")
        found[name] = url
    return found


def key_var(name: str) -> str:
    """The driver's variable holding a server's key: CONTEXT7_API_KEY for context7."""
    return re.sub(r"[^A-Z0-9_]", "_", name.upper()) + "_API_KEY"


def headers(name: str) -> dict[str, str]:
    """The headers that carry a server's key, if the driver has one."""
    key = credentials.get(key_var(name))
    return {"Authorization": f"Bearer {key}"} if key else {}


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
    """opencode.json's `mcp` section."""
    return {name: {"type": "remote", "url": url, "enabled": True} for name, url in servers(cfg).items()}


def claude(cfg: HarnessConfig) -> dict[str, Any]:
    """Claude Code's `mcpServers`, as --mcp-config and ~/.claude.json take it."""
    return {"mcpServers": {name: {"type": "http", "url": url} for name, url in servers(cfg).items()}}


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
