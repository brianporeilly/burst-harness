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
    terminal: str | None = None  # "pr" | "pushed_no_pr" | "push_failed" | "paused" | "abandoned"
    # Why the remote refused the push, if it did: the commits exist only in the target, so it is kept.
    push_error: str | None = None
    pr_url: str | None = None
    # Set when a headless run stopped on a question for the user.
    waiting: bool = False
    # Set when an interactive session was detached and is still running in the
    # target, so the target must be kept for reattaching.
    detached: bool = False
    # Set when the target was kept at the end (see cli._close), for whatever runs the next burst.
    kept: bool = False
    # Set when a run stopped short of a terminal state for a reason of its own ("max_tokens").
    stopped: str = ""
    # The profile the lead runs as, when not the top-level model (see agents.py).
    agent: str = ""
    # What each delegate used, by profile name (see agent.delegate_tool).
    delegated: dict[str, dict[str, object]] = field(default_factory=dict)
    # What the run said its end was about, for whatever launched it (HARNESS_RESULT's `said`): a pause's
    # next step and questions, an abandon's reason, the questions it stopped on.
    said: dict[str, object] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
