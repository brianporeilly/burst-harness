import json
import subprocess
import sys
from pathlib import Path

from harness import git_policy
from harness.git_policy import deny_reason, git_tokens, segments, subcommand

from .conftest import git as sh_git

POLICY = dict(protected=["main"], remote="origin", base="main")


def test_quoted_operators_do_not_split():
    segs = segments('git commit -m "a && b" && git push --force origin main')
    assert len(segs) == 2
    assert subcommand(git_tokens(segs[1])) == ("push", ["--force", "origin", "main"])


def test_global_options_skipped():
    assert subcommand(git_tokens(["GIT_X=1", "git", "-C", "dir", "-c", "a=b", "switch", "-c", "x"]))[0] == "switch"


def test_rules():
    on = lambda name: (lambda: name)  # noqa: E731
    assert deny_reason("git commit -m x", branch=on("main"), **POLICY)
    assert deny_reason("git commit -m x", branch=on("harness/t"), **POLICY) is None
    assert deny_reason("ls && git switch -c other", branch=on("harness/t"), **POLICY)
    assert deny_reason("git push -f origin main", branch=on("harness/t"), **POLICY)
    assert deny_reason("git status; echo 'unbalanced", branch=on("harness/t"), **POLICY) == git_policy.UNPARSEABLE
    assert deny_reason("ls -la", branch=on("main"), **POLICY) is None


def _hook(cwd: Path, event: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, git_policy.__file__, "--protected", "main"],
        input=json.dumps(event), capture_output=True, text=True, cwd=cwd,
    )


def test_hook_protocol(origin, tmp_path):
    """Run as a standalone script, as Claude Code does: exit 2 + stderr blocks."""
    work = tmp_path / "w"
    sh_git(tmp_path, "clone", "-q", str(origin), str(work))
    blocked = _hook(work, {"tool_name": "Bash", "tool_input": {"command": "git commit -am x"}})
    assert blocked.returncode == 2 and "protected branch 'main'" in blocked.stderr
    assert _hook(work, {"tool_name": "Bash", "tool_input": {"command": "git status"}}).returncode == 0
    assert _hook(work, {"tool_name": "Edit", "tool_input": {"file_path": "x"}}).returncode == 0
    sh_git(work, "switch", "-q", "-c", "harness/t")
    assert _hook(work, {"tool_name": "Bash", "tool_input": {"command": "git commit -am x"}}).returncode == 0
