"""python -m orchestrator [--port 8700]: the orchestrator, on this machine only.

Sessions run on this machine's podman, or, with target = agent-sandbox in the
configuration (HARNESS_TARGET and the rest of a cluster session's settings:
README "On a cluster"), on that cluster, behind its shared gateway.

Credentials it can hand to sessions come from its own environment, as for the
harness: HARNESS_GIT_TOKEN (or GH_TOKEN), ANTHROPIC_API_KEY,
CLAUDE_CODE_OAUTH_TOKEN. Your ~/.config/burst-harness/config.toml supplies the
defaults (model, images, egress_allow).

--jobs DIR (repeatable, or ORCHESTRATOR_JOBS, separated by ":") adds jobs: a
job's directory, or one whose subdirectories are jobs. Their runs get the same
environment, so a job's secrets (GITHUB_TOKEN, ...) are set here too.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from harness.config import HarnessConfig

from .app import create_app
from .backend import ClusterBackend, PodmanBackend
from .jobs import JobRunner


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m orchestrator", description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8700)
    parser.add_argument("--jobs", action="append", default=[], metavar="DIR",
                        help="A job's directory, or one of job directories. Repeatable.")
    args = parser.parse_args()
    directories = [*args.jobs, *filter(None, os.environ.get("ORCHESTRATOR_JOBS", "").split(os.pathsep))]
    jobs = JobRunner([Path(d) for d in directories]) if directories else None
    base = HarnessConfig.load(None, {})
    backend = ClusterBackend(base) if base.target == "agent-sandbox" else PodmanBackend(base)
    where = f"sessions in {base.namespace} on the cluster" if backend.name == "agent-sandbox" else "sessions on podman"
    print(f"Orchestrator on http://127.0.0.1:{args.port}/ (this machine only), {where}")
    # Loopback only: there is no login (see app.py).
    if jobs:
        print(f"Jobs from {', '.join(str(d) for d in jobs.directories)}: {', '.join(jobs.discover()) or 'none found'}")
    uvicorn.run(create_app(backend, jobs), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
