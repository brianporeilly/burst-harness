"""Execution environment pinned to a single repository root."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from strands.sandbox.not_a_sandbox_local_environment import NotASandboxLocalEnvironment
from strands.sandbox.types import ExecutionResult, StreamChunk


class RepoEnvironment(NotASandboxLocalEnvironment):
    """Run commands on the host, rooted at one repo.

    The vended `shell` tool calls `execute()` without a cwd, and each call is a
    fresh shell, so without this every command would land in whatever directory
    the harness process happens to be in and `cd` would never persist. This is
    not isolation -- commands still run with full host privileges.
    """

    def __init__(self, root: Path, *, inherit_venv: bool = False) -> None:
        self.root = Path(root).resolve()
        self._env = {} if inherit_venv else _venv_escape()

    def _resolve_path(self, path: str) -> Path:  # base declares this a staticmethod
        """Resolve relative paths against the repo root rather than the process cwd."""
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.root / candidate

    def _defaults(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        # `execute()` passes cwd=None explicitly, so setdefault would not fire.
        if kwargs.get("cwd") is None:
            kwargs["cwd"] = str(self.root)
        if self._env:
            kwargs["env"] = {**self._env, **(kwargs.get("env") or {})}
        return kwargs

    async def execute_streaming(self, *args: Any, **kwargs: Any) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        async for chunk in super().execute_streaming(*args, **self._defaults(kwargs)):
            yield chunk

    async def execute_code_streaming(self, *args: Any, **kwargs: Any) -> AsyncGenerator[StreamChunk | ExecutionResult, None]:
        async for chunk in super().execute_code_streaming(*args, **self._defaults(kwargs)):
            yield chunk


def _venv_escape() -> dict[str, str]:
    """Environment overrides that hide the harness's own virtualenv.

    Without this the agent's shell inherits the harness's VIRTUAL_ENV and PATH, so
    a `pip install` while working on someone else's repo lands in the harness's
    venv -- observed in testing, and able to corrupt the harness itself.
    """
    venv = os.environ.get("VIRTUAL_ENV")
    if not venv:
        return {}
    kept = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p and not p.startswith(venv)]
    return {"VIRTUAL_ENV": "", "PATH": os.pathsep.join(kept)}
