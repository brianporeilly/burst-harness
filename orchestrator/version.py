"""What is running: the release's version and the commit it was built from.

The version is the installed package's (pyproject.toml, which a release sets before its tag is built). The
commit is BURST_HARNESS_REVISION, which the image's build sets (Dockerfile, images.yml); from a checkout,
git's, with `dirty` when it has changes not committed.
"""

from __future__ import annotations

import os
import subprocess
from functools import cache
from importlib import metadata
from typing import Any

from .launch import REPO

REVISION_ENV = "BURST_HARNESS_REVISION"


@cache
def about() -> dict[str, Any]:
    try:
        version = metadata.version("burst-harness")
    except metadata.PackageNotFoundError:
        version = ""
    out: dict[str, Any] = {"version": version, "revision": os.environ.get(REVISION_ENV, "").strip()}
    if not out["revision"] and (REPO / ".git").exists():
        out["revision"] = _git("rev-parse", "HEAD")
        if out["revision"] and _git("status", "--porcelain", "--untracked-files=no"):
            out["dirty"] = True
    return out


def line() -> str:
    """For the log: `0.9.1 (abc1234)`."""
    found = about()
    revision = found["revision"][:7] + ("+changes" if found.get("dirty") else "")
    return " ".join(filter(None, [found["version"] or "unknown version", f"({revision})" if revision else ""]))


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=5,
                              check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
