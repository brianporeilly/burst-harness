"""git and gh, executed in whichever environment the agent is working in."""

from __future__ import annotations

from .exec_target import ExecError, ExecTarget


class GitError(RuntimeError):
    """A git or gh command failed."""


def run(target: ExecTarget, *args: str, check: bool = True) -> str:
    """Run a git command in the target's repo and return stdout."""
    try:
        proc = target.run(["git", *args], check=False)
    except ExecError as err:  # the target itself was unreachable
        raise GitError(str(err)) from err
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def gh(target: ExecTarget, *args: str) -> str:
    try:
        proc = target.run(["gh", *args], check=False)
    except ExecError as err:
        raise GitError(str(err)) from err
    if proc.returncode != 0:
        raise GitError(f"gh {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def is_repo(target: ExecTarget) -> bool:
    return run(target, "rev-parse", "--git-dir", check=False) != ""


def current_branch(target: ExecTarget) -> str:
    return run(target, "rev-parse", "--abbrev-ref", "HEAD")


def is_dirty(target: ExecTarget) -> bool:
    """True if anything is uncommitted, including untracked files."""
    return bool(run(target, "status", "--porcelain"))


def dirty_summary(target: ExecTarget, limit: int = 20) -> str:
    lines = run(target, "status", "--porcelain").splitlines()
    shown = "\n".join(lines[:limit])
    if len(lines) > limit:
        shown += f"\n... and {len(lines) - limit} more"
    return shown


def has_identity(target: ExecTarget) -> bool:
    """True if git can already resolve a committer identity."""
    return bool(run(target, "config", "--get", "user.email", check=False))


def start_branch(target: ExecTarget, branch: str, *, base: str, remote: str) -> str:
    """Fetch the remote base and create `branch` from it.

    This is the whole point of the harness: the branch is based on freshly
    fetched `remote/base`, never on whatever happened to be checked out.
    """
    run(target, "fetch", remote, base)
    if run(target, "branch", "--list", branch):
        run(target, "switch", branch)
        return f"Switched to existing branch {branch}."
    run(target, "switch", "-c", branch, f"{remote}/{base}")
    return f"Created {branch} from {remote}/{base} (freshly fetched)."


def commit_all(target: ExecTarget, message: str, *, name: str = "", email: str = "") -> str:
    """Stage everything and commit.

    Identity is passed with -c rather than written into the repo config, so the
    harness never mutates a checkout it does not own.
    """
    if not is_dirty(target):
        return "Nothing to commit; working tree already clean."
    run(target, "add", "-A")
    identity = []
    if name:
        identity += ["-c", f"user.name={name}"]
    if email:
        identity += ["-c", f"user.email={email}"]
    run(target, *identity, "commit", "-m", message)
    return f"Committed: {run(target, 'log', '-1', '--pretty=%h %s')}"


def push(target: ExecTarget, branch: str, *, remote: str) -> str:
    run(target, "push", "-u", remote, branch)
    return f"Pushed {branch} to {remote}."


def open_pr(target: ExecTarget, *, title: str, body: str, base: str, draft: bool) -> str:
    """Create a PR and return its URL."""
    args = ["pr", "create", "--title", title, "--body", body, "--base", base]
    if draft:
        args.append("--draft")
    return gh(target, *args)
