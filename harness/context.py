"""Per-run state shared by the CLI, the engines, and the terminal-state actions."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import git
from .config import HarnessConfig
from .exec_target import ExecTarget


@dataclass
class HarnessContext:
    """Per-run state shared between the CLI and the tools."""

    target: ExecTarget
    cfg: HarnessConfig
    slug: str
    branch: str
    # Harness-side snapshot directory name for this repo (see state.state_key).
    state_key: str
    # `git -c` credentials for the harness's own fetch/push; never the agent's.
    auth: git.Auth = ()
    # host/owner/repo for `gh --repo`, or None if the remote isn't recognised.
    repo_slug: str | None = None
    terminal: str | None = None  # "pr" | "pushed_no_pr" | "paused" | "abandoned"
    pr_url: str | None = None
    # Set when a headless run stopped on a question for the user.
    waiting: bool = False
    # Set when an interactive session was detached and is still running in the
    # target, so the target must be kept for reattaching.
    detached: bool = False
    notes: list[str] = field(default_factory=list)
