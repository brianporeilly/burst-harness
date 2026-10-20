import os
import subprocess
from pathlib import Path

import pytest

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """No ambient tokens, HARNESS_* settings, or user config leak into a test."""
    for name in list(os.environ):
        if name.startswith("HARNESS_") or name in ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # Snapshots and job run records go here, not in the real ~/.burst-harness.
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path) -> Path:
    """A bare repo with one commit on main, standing in for a remote."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("hello\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "init")
    bare = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    return bare
