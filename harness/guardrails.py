"""Git workflow guardrails enforced in code rather than by prompting.

These exist because reminding a model to "branch from origin/main" is
probabilistic. A denial comes back to the model as a correctable error, so it
self-corrects instead of needing a human to notice.

The rules themselves live in git_policy.py, which the Claude Code engine also
runs as a hook; this is the Strands adapter.
"""

from __future__ import annotations

from strands import InterventionHandler
from strands.hooks import BeforeToolCallEvent
from strands.interventions import Deny, InterventionAction, Proceed

from . import git, git_policy
from .config import HarnessConfig
from .exec_target import ExecTarget


class GitGuardrails(InterventionHandler):
    """Refuse shell commands that would break the harness's git invariants."""

    name = "harness:git-guardrails"

    def __init__(self, target: ExecTarget, cfg: HarnessConfig) -> None:
        self.target = target
        self.cfg = cfg

    @property
    def on_error(self) -> str:
        # Fail closed: a broken policy must not silently stop enforcing.
        return "deny"

    def before_tool_call(self, event: BeforeToolCallEvent, **kwargs: object) -> InterventionAction:
        if event.tool_use.get("name") not in ("shell", "sandbox_shell"):
            return Proceed()
        command = (event.tool_use.get("input") or {}).get("command", "")
        if not isinstance(command, str):
            return Proceed()
        reason = git_policy.deny_reason(
            command,
            branch=self._branch,
            protected=self.cfg.protected_branches,
            remote=self.cfg.remote,
            base=self.cfg.base_branch,
        )
        return Deny(reason=reason) if reason else Proceed()

    def _branch(self) -> str:
        try:
            return git.current_branch(self.target)
        except git.GitError:
            return "(unknown)"
