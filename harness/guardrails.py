"""Git workflow guardrails enforced in code rather than by prompting.

These exist because reminding a model to "branch from origin/main" is
probabilistic. A denial comes back to the model as a correctable error, so it
self-corrects instead of needing a human to notice.
"""

from __future__ import annotations

import logging
import re
import shlex


from strands import InterventionHandler
from strands.hooks import BeforeToolCallEvent
from strands.interventions import Deny, InterventionAction, Proceed

from . import git
from .config import HarnessConfig
from .exec_target import ExecTarget

logger = logging.getLogger(__name__)

# Shell operators that separate independent commands within one string.
_OPERATORS = {"&&", "||", ";", "|", "&"}

# git global options that take a value, so the subcommand is one token further on.
_GLOBAL_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}

# Leading `VAR=value` prefixes, as in `GIT_AUTHOR_NAME=x git commit`.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _segments(command: str) -> list[list[str]]:
    """Tokenize a shell command, then split it into independent commands.

    Tokenizing *before* splitting is the whole trick: splitting the raw string on
    `&&` first breaks quoting, so `git commit -m "... && ..."` would fragment into
    unparseable pieces and slip past every check below.

    Raises ValueError on unbalanced quotes.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token in _OPERATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def _is_git(tokens: list[str]) -> list[str] | None:
    """Return the token list starting at `git`, or None if this is not a git call."""
    index = 0
    while index < len(tokens) and _ASSIGNMENT.match(tokens[index]):
        index += 1
    if index < len(tokens) and tokens[index] in {"git", "/usr/bin/git"}:
        return tokens[index:]
    return None


def _subcommand(tokens: list[str]) -> tuple[str | None, list[str]]:
    """Return the git subcommand and its arguments, skipping global options."""
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in _GLOBAL_OPTS_WITH_VALUE:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, tokens[index + 1 :]
    return None, []


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
        if not isinstance(command, str) or "git" not in command:
            return Proceed()

        try:
            segments = _segments(command)
        except ValueError:
            # Unparseable quoting would also defeat the checks below, so refuse
            # rather than guess. Such a command would fail in the shell anyway.
            logger.debug("guardrails: could not parse %r", command)
            return Deny(
                reason="Could not parse this command safely (unbalanced quotes). "
                "Rewrite it as simpler, separate commands."
            )
        for segment in segments:
            git_tokens = _is_git(segment)
            if git_tokens is None:
                continue
            denial = self._check(git_tokens)
            if denial is not None:
                return Deny(reason=denial)
        return Proceed()

    def _check(self, tokens: list[str]) -> str | None:
        sub, args = _subcommand(tokens)
        if sub is None:
            return None
        protected = set(self.cfg.protected_branches)

        if sub in {"switch", "checkout"} and ({"-c", "-b", "-C", "-B"} & set(args)):
            return (
                f"Do not create branches. The harness already put you on "
                f"'{self._branch()}', created from {self.cfg.remote}/{self.cfg.base_branch}. "
                "Continue working on it, or call pause_for_review if you need a different branch."
            )

        if sub == "commit":
            branch = self._branch()
            if branch in protected:
                return (
                    f"Refusing to commit on protected branch '{branch}'. "
                    "The harness normally creates a task branch before you run; "
                    "call pause_for_review and explain, rather than committing here."
                )

        if sub == "push" and ({"--force", "-f", "--force-with-lease"} & set(args)):
            target = protected & set(args)
            if target or self._branch() in protected:
                return (
                    f"Refusing to force-push to a protected branch "
                    f"({', '.join(sorted(target)) or self._branch()})."
                )

        if sub == "reset" and "--hard" in args and self._branch() in protected:
            return f"Refusing 'git reset --hard' on protected branch '{self._branch()}'."

        return None

    def _branch(self) -> str:
        try:
            return git.current_branch(self.target)
        except git.GitError:
            return "(unknown)"
