"""Runners saved in the orchestrator's page: hosts of your own that tasks and jobs run on (harness/runner.py).

A saved runner is a name and a URL (ssh://, podman+ssh://, docker+ssh://, podman+unix://, docker+unix://), as
the harness's `runners` setting takes them. The configuration's (`runners`, HARNESS_RUNNERS) are listed too;
one saved here of the same name replaces it, and removing that goes back to the configuration's. At most one
runner is the default: a new task that does not pick where it runs goes there. With none, a task runs where the
orchestrator's own target puts it (agent-sandbox on a cluster, podman or docker here).

The orchestrator's environment gets the whole list as HARNESS_RUNNERS, whenever it changes, so whatever runs from
here reads the same ones: tasks' bursts, jobs (a job.toml names its runner with `runner = "NAME"`), and the
releases of sandboxes a burst kept. A runner's SSH key and known_hosts are credentials (Settings, Credentials):
<NAME>_SSH_KEY and <NAME>_SSH_KNOWN_HOSTS, granted only to the runs that use that runner.

Kept in runners.json in the orchestrator's own state directory (settings.orchestrator_dir).
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness import credentials, runner
from harness.config import HarnessConfig

ENV = "HARNESS_RUNNERS"
URL_CHARS = 500
# What a task's form says of each kind.
KIND_NOTES = {"ssh": "a plain host: no gateway, its network is its own",
              "podman": "its podman: a container, with its gateway", "docker": "its docker: a container, with its gateway"}


class RunnersError(ValueError):
    """A runner, or a choice of one, that cannot be used as asked."""


class RunnerStore:
    def __init__(self, path: Path, base: HarnessConfig) -> None:
        self.path = path
        self.lock = threading.Lock()
        try:  # the configuration's, as the orchestrator started: HARNESS_RUNNERS is ours to set from here on
            self.configured = {runner.parse(entry).name: entry for entry in base.runners}
        except runner.RunnerError as err:
            raise RunnersError(f"runners (the configuration's): {err}") from err
        self._export(self._load())

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        part = self.path.with_suffix(".part")
        part.write_text(json.dumps(data, indent=2) + "\n")
        part.replace(self.path)
        self._export(data)

    def _merged(self, data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """name -> {entry, source}: the configuration's, with those saved here over them."""
        found = {name: {"entry": entry, "source": "configuration"} for name, entry in self.configured.items()}
        saved = data.get("runners") if isinstance(data.get("runners"), dict) else {}
        for name, record in sorted(saved.items()):
            if isinstance(record, dict) and record.get("url"):
                found[name] = {"entry": f"{name}={record['url']}", "source": "settings"}
        return found

    def _default(self, data: dict[str, Any], merged: dict[str, dict[str, Any]]) -> str:
        name = data.get("default")
        return name if isinstance(name, str) and name in merged else ""

    def _export(self, data: dict[str, Any]) -> None:
        entries = [r["entry"] for r in self._merged(data).values()]
        if entries or ENV in os.environ:
            os.environ[ENV] = ",".join(entries)

    def entries(self) -> tuple[str, ...]:
        with self.lock:
            return tuple(r["entry"] for r in self._merged(self._load()).values())

    def find(self, name: str) -> runner.Runner:
        try:
            return runner.find(replace(HarnessConfig(), runners=self.entries()), name)
        except runner.RunnerError as err:
            raise RunnersError(str(err)) from err

    def default(self) -> str:
        with self.lock:
            data = self._load()
            return self._default(data, self._merged(data))

    def choose(self, value: Any) -> str:
        """Where a new task runs: None for the default runner ("" with none), "" for the orchestrator's own
        target, or a runner's name."""
        if value is None:
            return self.default()
        name = str(value).strip()
        if name:
            self.find(name)
        return name

    def listing(self) -> dict[str, Any]:
        with self.lock:
            data = self._load()
        merged = self._merged(data)
        saved = data.get("runners") if isinstance(data.get("runners"), dict) else {}
        rows = []
        for name, found in merged.items():
            r = runner.parse(found["entry"])
            record = saved.get(name, {}) if found["source"] == "settings" else {}
            rows.append({"name": name, "url": found["entry"].partition("=")[2], "kind": r.kind,
                         "note": KIND_NOTES[r.kind], "over_ssh": r.over_ssh, "source": found["source"],
                         "configuration_too": name in self.configured,
                         # Whether ssh has a key and known hosts of ours to use (never the values).
                         "key": r.key_var, "key_set": bool(credentials.get(r.key_var)),
                         "known_hosts": r.known_hosts_var, "known_hosts_set": bool(credentials.get(r.known_hosts_var)),
                         "updated": record.get("updated"), "updated_by": record.get("updated_by", "")})
        return {"runners": rows, "default": self._default(data, merged)}

    def set(self, name: str, raw: dict[str, Any], *, user: str = "") -> None:
        """Save a runner (url), and make it the default or not (default: true or false)."""
        url, default = raw.get("url"), raw.get("default")
        if default is not None and not isinstance(default, bool):
            raise RunnersError("default is true or false.")
        with self.lock:
            data = self._load()
            saved = dict(data.get("runners") or {})
            if url is not None:
                url = str(url).strip()
                if len(url) > URL_CHARS or "," in url:
                    raise RunnersError(f"The URL is one, of at most {URL_CHARS} characters, with no commas.")
                try:
                    runner.parse(f"{name}={url}")
                except runner.RunnerError as err:
                    raise RunnersError(str(err)) from err
                saved[name] = {"url": url, "updated": _now(), "updated_by": user}
            elif name not in self._merged(data):
                raise RunnersError(f"{name} is not saved yet: give its URL.")
            data["runners"] = saved
            if default:
                data["default"] = name
            elif default is False and data.get("default") == name:
                data.pop("default")
            self._save(data)

    def remove(self, name: str) -> str:
        """Forget what is saved here of a runner; what it is from now on, for saying so."""
        with self.lock:
            data = self._load()
            saved = dict(data.get("runners") or {})
            if name not in saved:
                raise RunnersError(f"{name} is not saved here." + (
                    " It is the configuration's (HARNESS_RUNNERS)." if name in self.configured else ""))
            del saved[name]
            data["runners"] = saved
            if data.get("default") == name and name not in self.configured:
                data.pop("default")
            self._save(data)
        return "the configuration's" if name in self.configured else "gone"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
