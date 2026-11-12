"""git, executed in whichever environment the agent is working in; PRs, from the driver.

Credentials belong to the driver, never the target. The harness passes a token
only on its own clone/fetch/push invocations (``git -c http.extraHeader=...``,
which is not persisted to the repo's config), and opens pull requests from the
driver: with ``gh --repo`` on GitHub, through a Forgejo's API on one listed in
``forges`` (forge.py). Each forge's token goes to its own host alone. The
agent's shell in the target therefore has no credential to push with or to
exfiltrate.

With ``egress_inject = git`` the harness passes nothing at all: the target's
gateway adds the token to requests for the repo (see containers.py), so git in
the target -- the harness's and the agent's alike -- authenticates without the
token ever being there.
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import subprocess
from pathlib import PurePosixPath

from typing import TYPE_CHECKING

from . import credentials, forge
from .exec_target import ExecError, ExecTarget

if TYPE_CHECKING:
    from .config import HarnessConfig

# Checked in order. GH_TOKEN/GITHUB_TOKEN are what gh itself reads, so a driver
# already set up for gh needs nothing extra.
TOKEN_ENV_VARS = credentials.GIT

Auth = tuple[str, ...]


class GitError(RuntimeError):
    """A git or gh command failed."""


# Every token the harness has sent, kept out of what it shows (error text reaches the model).
_sent: set[str] = set()


def token() -> str:
    """GitHub's token."""
    return credentials.first(TOKEN_ENV_VARS)


def auth_args(url: str, cfg: HarnessConfig | None = None) -> Auth:
    """`git -c` arguments that authenticate one invocation against an HTTPS remote: its forge's token
    (forge.py), as HTTP Basic credentials. Empty for SSH remotes, for a host that is no forge of `cfg`'s
    (without one, github.com alone), and when its token is not set; git then falls back to whatever the
    target already has."""
    basic = basic_credential(url, cfg)
    if not basic or not url.startswith("https://"):
        return ()
    return ("-c", f"http.extraHeader=Authorization: Basic {basic}")


def basic_credential(url: str = "https://github.com/", cfg: HarnessConfig | None = None) -> str:
    """The token for `url`'s forge as HTTP Basic credentials, or "" when there is none."""
    secret = forge.token_for(cfg, url)
    if secret:
        _sent.add(secret)
    return forge.basic_credential(secret)


def default_branch(url: str, cfg: HarnessConfig | None = None) -> str:
    """A remote repo's default branch, asked from the driver with its forge's token. Raises GitError if
    the repo cannot be read (missing, private without a token, or not reachable)."""
    try:
        # Only the token: no credential helper or password dialog to wait on.
        proc = subprocess.run(["git", "-c", "credential.helper=", *auth_args(url, cfg), "ls-remote", "--symref", url,
                               "HEAD"],
                              check=False, capture_output=True, text=True, timeout=30,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""})
    except (OSError, subprocess.TimeoutExpired) as err:
        raise GitError(f"could not ask {url} for its default branch: {err}") from err
    if proc.returncode != 0:
        raise GitError(_redact(f"cannot read {url}: {proc.stderr.strip() or 'git ls-remote failed'}"))
    if found := re.search(r"^ref: refs/heads/(\S+)\s+HEAD$", proc.stdout, re.MULTILINE):
        return found.group(1)
    raise GitError(f"{url} has no default branch (an empty repo?).")


def _redact(text: str) -> str:
    """Strip the tokens (raw or encoded) from anything that may reach the model: GitHub's, and any sent."""
    for secret in {*_sent, *([token()] if token() else [])}:
        text = text.replace(base64.b64encode(f"x-access-token:{secret}".encode()).decode(), "***")
        text = text.replace(secret, "***")
    return text


def run(
    target: ExecTarget, *args: str, check: bool = True, auth: Auth = (), cwd: str | None = None
) -> str:
    """Run a git command in the target's repo and return stdout.

    `auth` is prepended to the argv but never appears in an error message:
    GitError text is returned to the model by the terminal-state tools.
    """
    try:
        proc = target.run(["git", *auth, *args], check=False, cwd=cwd)
    except ExecError as err:  # the target itself was unreachable
        raise GitError(_redact(str(err))) from err
    if check and proc.returncode != 0:
        raise GitError(_redact(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}"))
    return proc.stdout.strip()


def is_repo(target: ExecTarget) -> bool:
    return target.exists() and run(target, "rev-parse", "--git-dir", check=False) != ""


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


def unpushed(target: ExecTarget, remote: str) -> int:
    """How many commits HEAD has that no branch of `remote` has (as last fetched or pushed). 0 when it
    cannot tell: no commits yet, or not a repo."""
    count = run(target, "rev-list", "--count", "HEAD", "--not", f"--remotes={remote}", check=False)
    return int(count) if count.isdigit() else 0


def has_identity(target: ExecTarget) -> bool:
    """True if git can already resolve a committer identity."""
    return bool(run(target, "config", "--get", "user.email", check=False))


def remote_url(target: ExecTarget, remote: str) -> str:
    return run(target, "remote", "get-url", remote, check=False)


def clone(target: ExecTarget, url: str, *, auth: Auth | None = None) -> str:
    """Clone `url` to the target's repo path. The parent directory must be writable."""
    dest = PurePosixPath(target.repo)
    target.run_shell(f"mkdir -p {shlex.quote(str(dest.parent))}", cwd="/")
    run(target, "clone", "--quiet", url, dest.name, auth=auth_args(url) if auth is None else auth, cwd=str(dest.parent))
    return f"Cloned {redact_url(url)} into {dest}."


def remote_has_branch(target: ExecTarget, branch: str, *, remote: str, auth: Auth = ()) -> bool:
    return bool(run(target, "ls-remote", "--heads", remote, branch, auth=auth))


def start_branch(target: ExecTarget, branch: str, *, base: str, remote: str, auth: Auth = ()) -> str:
    """Fetch the remote base and create `branch` from it.

    This is the whole point of the harness: the branch is based on freshly
    fetched `remote/base`, never on whatever happened to be checked out.
    """
    run(target, "fetch", remote, base, auth=auth)
    if run(target, "branch", "--list", branch):
        run(target, "switch", branch)
        return f"Switched to existing branch {branch}."
    run(target, "switch", "-c", branch, f"{remote}/{base}")
    return f"Created {branch} from {remote}/{base} (freshly fetched)."


def sync_branch(target: ExecTarget, branch: str, *, remote: str, auth: Auth = ()) -> str:
    """Switch to `branch` and fast-forward it to the remote, if the remote has it.

    A fresh clone gets a tracking branch from `switch`; a reused checkout picks
    up anything pushed since (review fixes, a human's commits). A branch that has
    diverged is left alone rather than merged: that is a human's call.
    """
    fetched = target.run(["git", *auth, "fetch", remote, branch], check=False).returncode == 0
    if current_branch(target) != branch:
        run(target, "switch", branch)
    if not fetched:
        return f"On {branch} (could not fetch {remote}/{branch}; working from the local copy)."
    merged = target.run(["git", "merge", "--ff-only", f"{remote}/{branch}"], check=False)
    if merged.returncode != 0:
        return f"On {branch}; it has diverged from {remote}/{branch}, left as-is."
    return f"On {branch}, up to date with {remote}/{branch}."


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


def push(target: ExecTarget, branch: str, *, remote: str, auth: Auth = ()) -> str:
    run(target, "push", "-u", remote, branch, auth=auth)
    return f"Pushed {branch} to {remote}."


# https://host/owner/repo(.git), ssh://git@host/owner/repo(.git), git@host:owner/repo(.git)
_REMOTE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?|ssh://(?:[^@/]+@)?|[^@/]+@)"
    r"(?P<host>[^/:]+)(?::\d+)?[/:](?P<path>[^/]+/[^/]+?)(?:\.git)?/?$"
)


def repo_slug(url: str) -> str | None:
    """`host/owner/repo` for a remote URL, the form `gh --repo` accepts."""
    match = _REMOTE.match(url.strip())
    return f"{match['host']}/{match['path']}" if match else None


def redact_url(url: str) -> str:
    """Drop any userinfo (an embedded token) from a URL before printing it."""
    return re.sub(r"^(\w+://)[^@/]+@", r"\1", url)


PR_URL = re.compile(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/pull/[0-9]+")


def _forge(cfg: HarnessConfig | None, url: str) -> forge.Forge:
    try:
        found = forge.for_url(cfg, url)
    except forge.ForgeError as err:
        raise GitError(str(err)) from err
    if found is None:
        raise GitError(f"{forge._host(url) or url!r} is not a forge the harness knows: github.com, or one "
                       "listed in forges (NAME=https://host).")
    return found


def pr_state(url: str, cfg: HarnessConfig | None = None) -> str:
    """A PR's state, asked of its forge from the driver with its token: "open", "merged" or "closed"."""
    host = _forge(cfg, url)
    if host.kind == "forgejo":
        try:
            return forge.forgejo_pr_state(host, url)
        except forge.ForgeError as err:
            raise GitError(_redact(str(err))) from err
    if not PR_URL.fullmatch(url):
        raise GitError(f"{url!r} is not a PR's URL.")
    env = dict(os.environ)
    if secret := token():
        env["GH_TOKEN"] = secret
    try:
        proc = subprocess.run(["gh", "pr", "view", url, "--json", "state", "--jq", ".state"],
                              capture_output=True, text=True, env=env, timeout=60, check=False)
    except FileNotFoundError as err:
        raise GitError("gh is not installed on the driver.") from err
    except subprocess.TimeoutExpired as err:
        raise GitError(f"gh pr view {url} took too long.") from err
    state = proc.stdout.strip().lower()
    if proc.returncode != 0 or state not in ("open", "merged", "closed"):
        raise GitError(_redact(f"gh pr view failed: {(proc.stderr or proc.stdout).strip()}"))
    return state


def open_pr(*, repo: str, head: str, title: str, body: str, base: str, draft: bool,
            cfg: HarnessConfig | None = None) -> str:
    """Create a PR from the driver and return its URL. `repo` is `host/owner/repo` (repo_slug), or
    `owner/repo` on GitHub.

    Runs on the driver, not in the target: the target has no gh and no token.
    """
    if repo.count("/") >= 2 and (host := _forge(cfg, f"https://{repo}")).kind == "forgejo":
        try:
            return forge.forgejo_open_pr(host, f"https://{repo}", head=head, base=base, title=title, body=body,
                                         draft=draft)
        except forge.ForgeError as err:
            raise GitError(_redact(str(err))) from err
    args = ["gh", "pr", "create", "--repo", repo, "--head", head,
            "--title", title, "--body", body, "--base", base]
    if draft:
        args.append("--draft")
    env = dict(os.environ)
    if secret := token():
        env["GH_TOKEN"] = secret
    try:
        proc = subprocess.run(args, capture_output=True, text=True, env=env, timeout=120)
    except FileNotFoundError as err:
        raise GitError("gh is not installed on the driver.") from err
    if proc.returncode != 0:
        # A later burst of a task with a PR open: the push updated it, so it is the PR.
        if "already exists" in proc.stderr and (found := re.search(r"https://\S+/pull/[0-9]+", proc.stderr)):
            return found.group(0)
        raise GitError(_redact(f"gh pr create failed: {(proc.stderr or proc.stdout).strip()}"))
    return proc.stdout.strip()
