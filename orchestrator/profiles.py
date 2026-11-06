"""Profiles set in the orchestrator's page: the other agents a task's lead may hand work to.

A profile here is what an [agents.NAME] table is in a config file
(harness/agents.py): a model (provider, base URL, model id, context window),
what it is good at (use_for, which is all the lead knows of it), and as a
delegate its extra instructions and whether it is read-only. Those set here
are added to the orchestrator's configuration's, and replace one of the same
name. They are not secret: keys a provider needs stay in the Credentials.

The default delegate_to, set here, is who a new task's lead may hand work to
unless the task says otherwise; unset, it is the configuration's. A task keeps
the names it was given when it was made (tasks.py), so continuing it hands
work to the same profiles, as they are defined by then.

Kept in profiles.json in the orchestrator's own state directory
(settings.orchestrator_dir), never in a burst's: a burst is given the
profiles in its environment (HARNESS_AGENTS, HARNESS_DELEGATE_TO), which the
harness reads as it reads a config file's.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness import agents
from harness.config import HarnessConfig

from .backend import SpecError, check_model, task_providers

NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
# What the page may set: a model and its role. Not token_command or token_file, which run a command or read
# a file on the driver; nor a profile's own delegate_to, for when it leads.
TEXT_KEYS = ("use_for", "prompt")
TEXT_CHARS = 4000


class ProfilesError(ValueError):
    """A profile, or a default, that cannot be set as asked."""


class ProfileStore:
    def __init__(self, path: Path, base: HarnessConfig) -> None:
        self.path = path
        self.base = base
        self.lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"profiles": {}}
        return data if isinstance(data, dict) else {"profiles": {}}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        part = self.path.with_suffix(".part")
        part.write_text(json.dumps(data, indent=2) + "\n")
        part.replace(self.path)

    @staticmethod
    def _profile(record: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if key not in ("updated", "updated_by")}

    def profiles(self) -> dict[str, dict[str, Any]]:
        """Every profile a burst may use: the configuration's, with those set here over them."""
        with self.lock:
            stored = self._load()["profiles"]
        return {**self.base.agents, **{name: self._profile(record) for name, record in stored.items()}}

    def default(self) -> tuple[str, ...]:
        """Who a new task's lead may hand work to: the default set here, or else the configuration's."""
        with self.lock:
            data = self._load()
        return tuple(data["delegate_to"]) if isinstance(data.get("delegate_to"), list) else self.base.delegate_to

    def listing(self) -> dict[str, Any]:
        with self.lock:
            data = self._load()
        stored = data["profiles"]
        rows = []
        for name in dict.fromkeys((*self.base.agents, *sorted(stored))):
            record = stored.get(name)
            profile = self._profile(record) if record is not None else self.base.agents[name]
            rows.append({"name": name, **profile, "source": "settings" if record is not None else "configuration",
                         # Set here and in the configuration: removing it here goes back to the configuration's.
                         "configuration_too": record is not None and name in self.base.agents,
                         "updated": (record or {}).get("updated"), "updated_by": (record or {}).get("updated_by", "")})
        here = isinstance(data.get("delegate_to"), list)
        return {"profiles": rows, "delegate_to": list(data["delegate_to"] if here else self.base.delegate_to),
                "delegate_to_source": "settings" if here else "configuration", "providers": task_providers()}

    def set(self, name: str, raw: dict[str, Any], *, user: str = "") -> None:
        if not NAME.fullmatch(name):
            raise ProfilesError(f"{name!r} is not a profile name: lowercase letters, digits, - and _, up to 32.")
        profile = parse(raw)
        with self.lock:
            data = self._load()
            agents_now = {**self.base.agents, **{n: self._profile(r) for n, r in data["profiles"].items()}}
            check(self.base, {**agents_now, name: profile}, ())
            data["profiles"][name] = {**profile, "updated": _now(), "updated_by": user}
            self._save(data)

    def remove(self, name: str) -> str:
        """Forget the profile set here; what is used from now on, for saying so."""
        with self.lock:
            data = self._load()
            if name not in data["profiles"]:
                raise ProfilesError(f"{name} is not set here." + (" It is the configuration's, which only a config "
                                                                   "file changes." if name in self.base.agents else ""))
            in_default = isinstance(data.get("delegate_to"), list) and name in data["delegate_to"]
            if in_default and name not in self.base.agents:
                raise ProfilesError(f"{name} is in the default delegates: take it out of them first.")
            del data["profiles"][name]
            self._save(data)
        return "the configuration's" if name in self.base.agents else "gone"

    def set_default(self, names: Any, *, user: str = "") -> None:
        """The default delegates, or None to go back to the configuration's."""
        with self.lock:
            data = self._load()
            if names is None:
                data.pop("delegate_to", None)
            else:
                chosen = names_given(names)
                check(self.base, {**self.base.agents, **{n: self._profile(r) for n, r in data["profiles"].items()}},
                      chosen)
                data["delegate_to"] = list(chosen)
            data["delegate_to_updated"], data["delegate_to_updated_by"] = _now(), user
            self._save(data)

    def environment(self, delegate_to: list[str] | None) -> dict[str, str]:
        """A burst's HARNESS_* settings for these: every profile, and who it may delegate to (None: the
        configuration's, for a task made before it kept its own)."""
        profiles = self.profiles()
        env = {"HARNESS_AGENTS": json.dumps(profiles, separators=(",", ":"))} if profiles else {}
        if delegate_to is not None:
            env["HARNESS_DELEGATE_TO"] = ",".join(delegate_to)
        return env


def parse(raw: dict[str, Any]) -> dict[str, Any]:
    """A profile as the page or the API gives it, checked; what is not given is left out. Raises ProfilesError."""
    provider, base_url, model = (str(raw.get(key) or "").strip() for key in ("provider", "base_url", "model_id"))
    try:
        check_model(provider, base_url, model, task_providers())
    except SpecError as err:
        raise ProfilesError(str(err)) from err
    if provider in ("llamacpp", "openai") and not base_url:
        raise ProfilesError(f"provider {provider} needs a base URL.")
    profile: dict[str, Any] = {key: value for key, value in
                               (("provider", provider), ("base_url", base_url), ("model_id", model)) if value}
    try:
        window = int(raw.get("context_window_limit") or 0)
    except (TypeError, ValueError):
        window = -1
    if window < 0:
        raise ProfilesError("The context window is a number of tokens.")
    if window:
        profile["context_window_limit"] = window
    for key in TEXT_KEYS:
        text = str(raw.get(key) or "").strip()
        if len(text) > TEXT_CHARS:
            raise ProfilesError(f"{key} is at most {TEXT_CHARS} characters.")
        if text:
            profile[key] = text
    if not profile.get("use_for"):
        raise ProfilesError("Say what it is good at (use_for): it is all a lead knows of it.")
    if not isinstance(raw.get("read_only", False), bool):
        raise ProfilesError("read_only is true or false.")
    if raw.get("read_only"):
        profile["read_only"] = True
    return profile


def names_given(names: Any) -> tuple[str, ...]:
    if isinstance(names, str):
        names = names.split(",")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ProfilesError("delegate_to is a list of profile names.")
    return tuple(dict.fromkeys(n.strip() for n in names if n.strip()))


def check(base: HarnessConfig, profiles: dict[str, dict[str, Any]], delegate_to: tuple[str, ...]) -> None:
    """These profiles, and these delegates, as the harness will check them. Raises ProfilesError."""
    try:
        agents.check(replace(base, agents=profiles, delegate_to=delegate_to))
    except agents.AgentsError as err:
        raise ProfilesError(str(err)) from err


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")

