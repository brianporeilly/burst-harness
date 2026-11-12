"""MCP servers saved in the orchestrator's page, for sessions and tasks to choose from.

A saved server is a name and an https:// URL (harness/mcp.py), and whether it is on by default: a new session
or task that does not say which servers it wants gets the default ones, and the page's forms show every saved
one as a checkbox, the defaults ticked. The configuration's servers (`mcp`, HARNESS_MCP) are listed too, on by
default; here they can be turned off by default, or replaced by one of the same name, but not removed (only
the configuration changes them). They are not secret: a server's key stays in the Credentials, as
<NAME>_API_KEY, and the gateway adds it.

A session or task keeps the servers it was given as NAME=URL, so a session restarted or a task continued uses
the same ones, whatever is saved here by then.

Kept in mcp.json in the orchestrator's own state directory (settings.orchestrator_dir).
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness import mcp
from harness.config import HarnessConfig

URL_CHARS = 500


class McpServersError(ValueError):
    """A server, or a choice of servers, that cannot be used as asked."""


def _check(entries: tuple[str, ...], engine: str) -> dict[str, str]:
    """NAME=URL entries as the harness will check them, for that engine: {name: url}."""
    try:
        return mcp.servers(replace(HarnessConfig(), engine=engine, mcp=entries))
    except mcp.MCPError as err:
        raise McpServersError(str(err)) from err


class McpServerStore:
    def __init__(self, path: Path, base: HarnessConfig) -> None:
        self.path = path
        self.base = base
        self.lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        servers = data.get("servers") if isinstance(data, dict) else None
        return servers if isinstance(servers, dict) else {}

    def _save(self, servers: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        part = self.path.with_suffix(".part")
        part.write_text(json.dumps({"servers": servers}, indent=2) + "\n")
        part.replace(self.path)

    def _configured(self) -> dict[str, str]:
        return mcp.servers(self.base) if self.base.mcp else {}

    def _merged(self, stored: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """name -> {url, default, source}: the configuration's, with what is saved here over them."""
        found = {name: {"url": url, "default": True, "source": "configuration"}
                 for name, url in self._configured().items()}
        for name, record in sorted(stored.items()):
            server = {**found.get(name, {"default": False, "source": "settings"}),
                      **{key: record[key] for key in ("url", "default") if key in record}}
            if "url" in record:  # its own URL: saved here, over the configuration's of that name if any
                server["source"] = "settings"
            found[name] = server
        # A record of only `default`, for a server the configuration no longer has, is nothing to list.
        return {name: server for name, server in found.items() if server.get("url")}

    def servers(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            return self._merged(self._load())

    def listing(self) -> dict[str, Any]:
        with self.lock:
            stored = self._load()
        configured = self._configured()
        rows = []
        for name, server in self._merged(stored).items():
            record = stored.get(name, {})
            rows.append({"name": name, "url": server["url"], "default": server["default"], "source": server["source"],
                         "configuration_too": name in configured,
                         # Whether the gateway has a key to add for it (never the key).
                         "key": mcp.basic_var(name) if mcp.auth(name) == "basic" else mcp.key_var(name),
                         "basic_key": mcp.basic_var(name), "auth": mcp.auth(name), "key_set": bool(mcp.auth(name)),
                         "key_problem": mcp.problem(name),
                         "updated": record.get("updated"), "updated_by": record.get("updated_by", "")})
        return {"servers": rows}

    def set(self, name: str, raw: dict[str, Any], *, user: str = "") -> None:
        """Save a server (url, default), or change whether one is on by default (default alone)."""
        url = raw.get("url")
        default = raw.get("default")
        if default is not None and not isinstance(default, bool):
            raise McpServersError("default is true or false.")
        with self.lock:
            stored = self._load()
            known = self._merged(stored)
            record = dict(stored.get(name, {}))
            if url is not None:
                url = str(url).strip()
                if len(url) > URL_CHARS:
                    raise McpServersError(f"The URL is at most {URL_CHARS} characters.")
                _check((f"{name}={url}",), "opencode")  # what the sandboxes' engines may use
                record["url"] = url
            elif name not in known:
                raise McpServersError(f"{name} is not saved yet: give its URL.")
            if default is not None:
                record["default"] = default
            elif name not in known:
                record["default"] = True
            stored[name] = {**record, "updated": _now(), "updated_by": user}
            self._save(stored)

    def remove(self, name: str) -> str:
        """Forget what is saved here of a server; what it is from now on, for saying so."""
        with self.lock:
            stored = self._load()
            if name not in stored:
                raise McpServersError(f"{name} is not saved here." + (
                    " It is the configuration's (HARNESS_MCP): turn it off by default instead."
                    if name in self._configured() else ""))
            del stored[name]
            self._save(stored)
        return "the configuration's" if name in self._configured() else "gone"

    def choose(self, value: Any, *, engine: str = "opencode") -> tuple[str, ...]:
        """The servers a new session or task asked for, as NAME=URL: None for the default ones; else a list (or
        lines) of saved names and NAME=URL entries of its own."""
        servers = self.servers()
        if value is None:
            entries = tuple(f"{name}={server['url']}" for name, server in servers.items() if server["default"])
            # A default that this engine cannot use (http:// on loopback, for strands only) is left out.
            return tuple(e for e in entries if _usable(e, engine))
        items = value.split() if isinstance(value, str) else value
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise McpServersError("mcp is a list of saved server names, or NAME=URL.")
        entries = []
        for item in (item.strip() for item in items if item.strip()):
            if "," in item:
                raise McpServersError(f"{item!r}: a server's URL has no commas (a run is given them as a list).")
            if "=" in item:
                entries.append(item)
            elif item in servers:
                entries.append(f"{item}={servers[item]['url']}")
            else:
                raise McpServersError(f"No MCP server named {item} is saved (saved: {', '.join(servers) or 'none'}).")
        found = _check(tuple(entries), engine)
        return tuple(f"{name}={url}" for name, url in found.items())


def _usable(entry: str, engine: str) -> bool:
    try:
        _check((entry,), engine)
    except McpServersError:
        return False
    return True


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
