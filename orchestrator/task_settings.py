"""Settings for every task, set in the orchestrator's page: whether Claude Code adds its attribution to what a
task commits and the PRs it opens.

Unset, a task has the orchestrator's configuration's (claude_attribution, or HARNESS_CLAUDE_ATTRIBUTION in its
environment, which a burst inherits). Set here, a burst is given HARNESS_CLAUDE_ATTRIBUTION over that. It takes
effect for the bursts that start from now on: of new tasks, and of ones continued.

Kept in task-settings.json in the orchestrator's own state directory (settings.orchestrator_dir).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.config import HarnessConfig


class TaskSettingsError(ValueError):
    """A setting that cannot be set as asked."""


class TaskSettings:
    def __init__(self, path: Path, base: HarnessConfig) -> None:
        self.path = path
        self.base = base
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

    def listing(self) -> dict[str, Any]:
        with self.lock:
            data = self._load()
        here = isinstance(data.get("claude_attribution"), bool)
        return {"claude_attribution": data["claude_attribution"] if here else self.base.claude_attribution,
                "claude_attribution_source": "settings" if here else "configuration",
                "configuration": {"claude_attribution": self.base.claude_attribution},
                "updated": data.get("updated"), "updated_by": data.get("updated_by", "")}

    def set_attribution(self, value: Any, *, user: str = "") -> None:
        """On or off for every task's bursts from now on, or None to go back to the configuration's."""
        if value is not None and not isinstance(value, bool):
            raise TaskSettingsError("claude_attribution is true, false, or null for the configuration's.")
        with self.lock:
            data = self._load()
            if value is None:
                data.pop("claude_attribution", None)
            else:
                data["claude_attribution"] = value
            data["updated"], data["updated_by"] = datetime.now(UTC).isoformat(timespec="seconds"), user
            self._save(data)

    def environment(self) -> dict[str, str]:
        """A burst's HARNESS_* settings for these: only what is set here."""
        with self.lock:
            data = self._load()
        value = data.get("claude_attribution")
        return {"HARNESS_CLAUDE_ATTRIBUTION": str(value).lower()} if isinstance(value, bool) else {}
