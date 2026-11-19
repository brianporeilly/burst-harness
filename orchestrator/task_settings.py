"""Settings for every task, set in the orchestrator's page:

  claude_attribution   whether Claude Code adds its attribution to what a task commits and the PRs it opens
  review_with          the profile that reviews a change before its PR (harness/review.py); "" for no review
  fallback_to          what leads a burst whose llama.cpp server can't be reached as it starts (harness cli
                       _fall_back): "claude-code", a profile's name, or "" for no fallback
  fallback_model       Claude Code's model when it is the fallback; "" for its own default

Unset, a task has the orchestrator's configuration's (its config file, or HARNESS_* in its environment, which a
burst inherits). Set here, a burst is given that HARNESS_* over it: set to "" turns a review or a fallback off,
whatever the configuration says. It takes effect for the bursts that start from now on: of new tasks, and of
ones continued.

Kept in task-settings.json in the orchestrator's own state directory (settings.orchestrator_dir).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.config import HarnessConfig

# Each setting, and the type its value has.
KEYS: dict[str, type] = {"claude_attribution": bool, "review_with": str, "fallback_to": str, "fallback_model": str}
CLAUDE_CODE = "claude-code"  # fallback_to's value for the Claude Code engine, rather than a profile
MODEL_CHARS = 100


class TaskSettingsError(ValueError):
    """A setting that cannot be set as asked."""


class TaskSettings:
    def __init__(self, path: Path, base: HarnessConfig,
                 profiles: Callable[[], dict[str, Any]] | None = None) -> None:
        self.path = path
        self.base = base
        # The profiles a review or a fallback may name (profiles.py's, the configuration's with Settings' over
        # them); None: the configuration's.
        self.profiles = profiles or (lambda: dict(base.agents))
        self.lock = threading.Lock()

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

    @staticmethod
    def _here(data: dict[str, Any], key: str) -> bool:
        return isinstance(data.get(key), KEYS[key])

    def listing(self) -> dict[str, Any]:
        """Each setting as it stands (set here, else the configuration's), where it comes from, and the
        configuration's; with the profiles a review or a fallback may name."""
        with self.lock:
            data = self._load()
        found: dict[str, Any] = {}
        for key in KEYS:
            here = self._here(data, key)
            found[key] = data[key] if here else getattr(self.base, key)
            found[f"{key}_source"] = "settings" if here else "configuration"
        return {**found, "configuration": {key: getattr(self.base, key) for key in KEYS},
                "profiles": sorted(self.profiles()), "updated": data.get("updated"),
                "updated_by": data.get("updated_by", "")}

    def _check(self, key: str, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, KEYS[key]):
            kind = "true, false," if KEYS[key] is bool else "text"
            raise TaskSettingsError(f"{key} is {kind} or null for the configuration's.")
        if isinstance(value, str):
            value = value.strip()
        names = self.profiles()
        if key == "review_with" and value and value not in names:
            raise TaskSettingsError(f"review_with names no profile: {value}. "
                                    f"Choose from: {', '.join(sorted(names)) or 'none are defined'}.")
        if key == "fallback_to" and value not in ("", CLAUDE_CODE) and value not in names:
            raise TaskSettingsError(f"fallback_to is {CLAUDE_CODE!r} or a profile's name, not {value!r}.")
        if key == "fallback_model" and (len(value) > MODEL_CHARS or any(c.isspace() for c in value)):
            raise TaskSettingsError("fallback_model is a model's id, such as claude-sonnet-5-5.")
        return value

    def set(self, changes: dict[str, Any], *, user: str = "") -> None:
        """Set some of KEYS for every task's bursts from now on; None for a key goes back to the configuration's.
        All or none: one that cannot be set leaves the rest as they were."""
        if unknown := sorted(set(changes) - set(KEYS)):
            raise TaskSettingsError(f"Not a task setting: {', '.join(unknown)}. They are {', '.join(KEYS)}.")
        if not changes:
            raise TaskSettingsError(f"Give one or more of {', '.join(KEYS)} (null for the configuration's).")
        checked = {key: self._check(key, value) for key, value in changes.items()}
        with self.lock:
            data = self._load()
            for key, value in checked.items():
                if value is None:
                    data.pop(key, None)
                else:
                    data[key] = value
            data["updated"], data["updated_by"] = datetime.now(UTC).isoformat(timespec="seconds"), user
            self._save(data)

    def set_attribution(self, value: Any, *, user: str = "") -> None:
        """On or off for every task's bursts from now on, or None to go back to the configuration's."""
        self.set({"claude_attribution": value}, user=user)

    def values(self) -> dict[str, Any]:
        """What is set here, by key: what a burst's config takes over the configuration's."""
        with self.lock:
            data = self._load()
        return {key: data[key] for key in KEYS if self._here(data, key)}

    def environment(self) -> dict[str, str]:
        """A burst's HARNESS_* settings for these: only what is set here."""
        return {f"HARNESS_{key.upper()}": str(value).lower() if isinstance(value, bool) else str(value)
                for key, value in self.values().items()}
