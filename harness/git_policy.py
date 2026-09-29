"""The git rules the harness enforces, independent of any agent framework.

Standard library only, on purpose: the Strands engine applies these through an
intervention (guardrails.py), and the Claude Code engine uploads this very file
into the target and runs it as a ``PreToolUse`` hook. One policy, two engines.

Hook usage (Claude Code passes the tool call as JSON on stdin; exit 2 blocks it
and returns stderr to the model as the reason)::

    python3 git_policy.py --protected main,master --remote origin --base main
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterable

# Shell operators that separate independent commands within one string.
_OPERATORS = {"&&", "||", ";", "|", "&"}

# git global options that take a value, so the subcommand is one token further on.
_GLOBAL_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}

# Leading `VAR=value` prefixes, as in `GIT_AUTHOR_NAME=x git commit`.
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

UNPARSEABLE = (
    "Could not parse this command safely (unbalanced quotes). "
    "Rewrite it as simpler, separate commands."
)


def segments(command: str) -> list[list[str]]:
    """Tokenize a shell command, then split it into independent commands.

    Tokenizing *before* splitting is the whole trick: splitting the raw string on
    `&&` first breaks quoting, so `git commit -m "... && ..."` would fragment into
    unparseable pieces and slip past every check below.

    Raises ValueError on unbalanced quotes.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    parts: list[list[str]] = [[]]
    for token in lexer:
        if token in _OPERATORS:
            parts.append([])
        else:
            parts[-1].append(token)
    return [part for part in parts if part]


def git_tokens(tokens: list[str]) -> list[str] | None:
    """Return the token list starting at `git`, or None if this is not a git call."""
    index = 0
    while index < len(tokens) and _ASSIGNMENT.match(tokens[index]):
        index += 1
    if index < len(tokens) and tokens[index] in {"git", "/usr/bin/git"}:
        return tokens[index:]
    return None


def subcommand(tokens: list[str]) -> tuple[str | None, list[str]]:
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


def check(
    tokens: list[str], *, branch: Callable[[], str], protected: Iterable[str], remote: str, base: str
) -> str | None:
    """Why one git invocation is refused, or None if it is allowed."""
    sub, args = subcommand(tokens)
    if sub is None:
        return None
    protected = set(protected)

    if sub in {"switch", "checkout"} and ({"-c", "-b", "-C", "-B"} & set(args)):
        return (
            f"Do not create branches. The harness already put you on "
            f"'{branch()}', created from {remote}/{base}. "
            "Continue working on it, or stop and record a pause if you need a different branch."
        )

    if sub == "commit":
        current = branch()
        if current in protected:
            return (
                f"Refusing to commit on protected branch '{current}'. "
                "The harness normally creates a task branch before you run; "
                "stop and record a pause explaining why, rather than committing here."
            )

    if sub == "push" and ({"--force", "-f", "--force-with-lease"} & set(args)):
        targets = protected & set(args)
        if targets or branch() in protected:
            return f"Refusing to force-push to a protected branch ({', '.join(sorted(targets)) or branch()})."

    if sub == "reset" and "--hard" in args and branch() in protected:
        return f"Refusing 'git reset --hard' on protected branch '{branch()}'."

    return None


def deny_reason(
    command: str, *, branch: Callable[[], str], protected: Iterable[str], remote: str, base: str
) -> str | None:
    """Why a whole shell command is refused, or None. Fails closed on bad quoting."""
    if "git" not in command:
        return None
    try:
        parts = segments(command)
    except ValueError:
        return UNPARSEABLE
    for part in parts:
        tokens = git_tokens(part)
        if tokens is not None:
            reason = check(tokens, branch=branch, protected=protected, remote=remote, base=base)
            if reason:
                return reason
    return None


def _current_branch() -> str:
    proc = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else "(unknown)"


def main(argv: list[str] | None = None) -> int:
    """Claude Code PreToolUse hook entry point."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--protected", default="main,master")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--base", default="main")
    args = parser.parse_args(argv)
    try:
        event = json.load(sys.stdin)
    except ValueError:
        print("git policy hook: unreadable hook input; refusing.", file=sys.stderr)
        return 2
    if event.get("tool_name") != "Bash":
        return 0
    command = (event.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str):
        return 0
    reason = deny_reason(
        command,
        branch=_current_branch,
        protected=[p for p in args.protected.split(",") if p],
        remote=args.remote,
        base=args.base,
    )
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
