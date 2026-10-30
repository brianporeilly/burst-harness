"""python -m orchestrator [--port 8700]: the orchestrator, on this machine only
unless told otherwise.

Sessions run on this machine's podman (docker with HARNESS_TARGET=docker), or,
with target = agent-sandbox in the configuration (HARNESS_TARGET and the rest
of a cluster session's settings: README "On a cluster"), on that cluster,
behind its shared gateway.

Credentials it can hand to sessions come from its own environment, as for the
harness: HARNESS_GIT_TOKEN (or GH_TOKEN), ANTHROPIC_API_KEY,
CLAUDE_CODE_OAUTH_TOKEN. Your ~/.config/burst-harness/config.toml supplies the
defaults (model, images, egress_allow).

--jobs DIR (repeatable, or ORCHESTRATOR_JOBS, separated by ":") adds jobs: a
job's directory, or one whose subdirectories are jobs. Their runs get the same
environment, so a job's secrets (GITHUB_TOKEN, ...) are set here too.

--schedule (or ORCHESTRATOR_SCHEDULE=1) also runs each job whose trigger is a
cron schedule when it is due, in --timezone (ORCHESTRATOR_TIMEZONE; by default
this machine's). A time missed by up to --catch-up (ORCHESTRATOR_CATCH_UP,
default 1h) is run late, once; one missed for longer is skipped. With
--start-paused (ORCHESTRATOR_START_PAUSED=1), a job starts paused until it is
resumed in the Jobs tab. See jobs.py.

Tasks (tasks.py): a repo and a prompt, worked on in a sandbox until there is a
PR, with this orchestrator's credentials and target (podman or docker here,
never this machine itself). --task-limit (ORCHESTRATOR_TASK_LIMIT, default 1)
of them run at once, the rest queued; --task-ttl (ORCHESTRATOR_TASK_TTL,
default 4h) is how long a burst's sandbox claim lives on a cluster. --no-tasks
turns them off.

API tokens (tokens.py, made in the Tokens tab) let scripts, other machines and
bots use the Tasks API: `Authorization: Bearer orch_...`. --listen 0.0.0.0
with no --user-header serves the browser to this machine alone and the Tasks
API to anyone with a token. That is plain HTTP, so only on a network you
trust (or put TLS in front).

Behind a proxy that signs people in (in a cluster): --listen 0.0.0.0 to take
connections from it, --host NAME (repeatable) for the name it is reached by,
and --user-header HEADER for the header the proxy sets to who signed in;
requests without it are refused. Only with a network policy that lets nothing
else reach it: see app.py.
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import uvicorn

from harness.config import HarnessConfig
from harness.session import SessionError, seconds

from .app import create_app
from .backend import ClusterBackend, PodmanBackend
from .jobs import JobRunner
from .tasks import TaskRunner
from .tokens import TokenStore


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m orchestrator", description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8700)
    parser.add_argument("--listen", default="127.0.0.1", metavar="ADDRESS",
                        help="Where to take connections (default 127.0.0.1). Beyond this machine, without --user-header, others need an API token.")
    parser.add_argument("--host", action="append", default=[], metavar="NAME",
                        help="A name it is reached by, besides this machine's. Repeatable.")
    parser.add_argument("--user-header", default="", metavar="HEADER",
                        help="The header a signing-in proxy sets to who signed in; requests without it are refused.")
    parser.add_argument("--jobs", action="append", default=[], metavar="DIR",
                        help="A job's directory, or one of job directories. Repeatable.")
    parser.add_argument("--schedule", action="store_true", default=_flag("ORCHESTRATOR_SCHEDULE"),
                        help="Run jobs with a cron trigger when they are due.")
    parser.add_argument("--timezone", default=os.environ.get("ORCHESTRATOR_TIMEZONE", ""), metavar="ZONE",
                        help="The schedules' timezone, such as America/Anchorage (default: this machine's).")
    parser.add_argument("--catch-up", default=os.environ.get("ORCHESTRATOR_CATCH_UP", "1h"), metavar="DURATION",
                        help="How late a missed time is still run (default 1h).")
    parser.add_argument("--start-paused", action="store_true", default=_flag("ORCHESTRATOR_START_PAUSED"),
                        help="A job starts paused until it is resumed.")
    parser.add_argument("--tasks", action=argparse.BooleanOptionalAction, default=not _flag("ORCHESTRATOR_NO_TASKS"),
                        help="Take tasks: a repo and a prompt, worked on until there is a PR (default on).")
    parser.add_argument("--task-limit", type=int, default=int(os.environ.get("ORCHESTRATOR_TASK_LIMIT") or 1),
                        metavar="N", help="How many tasks run at once; the rest wait (default 1).")
    parser.add_argument("--task-ttl", default=os.environ.get("ORCHESTRATOR_TASK_TTL", "4h"), metavar="DURATION",
                        help="How long a task's sandbox claim lives, on a cluster (default 4h).")
    args = parser.parse_args()
    local_browser = args.listen not in ("127.0.0.1", "localhost", "::1") and not args.user_header
    try:
        zone = ZoneInfo(args.timezone) if args.timezone else _local_zone()
        catch_up = timedelta(seconds=seconds(args.catch_up))
        task_ttl = seconds(args.task_ttl)
    except (ZoneInfoNotFoundError, ValueError, SessionError) as err:
        parser.error(f"--timezone / --catch-up / --task-ttl: {err}")
    directories = [*args.jobs, *filter(None, os.environ.get("ORCHESTRATOR_JOBS", "").split(os.pathsep))]
    jobs = JobRunner([Path(d) for d in directories], schedule=args.schedule, zone=zone, catch_up=catch_up,
                     start_paused=args.start_paused) if directories else None
    base = HarnessConfig.load(None, {})
    backend = ClusterBackend(base) if base.target == "agent-sandbox" else PodmanBackend(base)
    where = f"sessions in {base.namespace} on the cluster" if backend.name == "agent-sandbox" else f"sessions on {backend.name}"
    if args.user_header:
        names = ", ".join(args.host) or "this machine"
        print(f"Orchestrator on {args.listen}:{args.port} for {names}, signed in by {args.user_header}, {where}")
    elif local_browser:
        print(f"Orchestrator on {args.listen}:{args.port}: the browser from this machine only "
              f"(http://127.0.0.1:{args.port}/), the Tasks API from anywhere with a token (plain HTTP), {where}")
    else:
        print(f"Orchestrator on http://127.0.0.1:{args.port}/ (this machine only), {where}")
    if jobs:
        print(f"Jobs from {', '.join(str(d) for d in jobs.directories)}: {', '.join(jobs.discover()) or 'none found'}")
        if jobs.schedule:
            print(f"Scheduled in {zone}, missed times run up to {args.catch_up} late"
                  + (", new jobs paused" if args.start_paused else ""))
        jobs.start()
    tasks = TaskRunner(base, limit=args.task_limit, claim_ttl=task_ttl) if args.tasks else None
    if tasks:
        print(f"Tasks in {tasks.root}, {tasks.limit} at a time")
        for line in tasks.recover():
            print(f"tasks: {line}")
        tasks.dispatch()
    tokens = TokenStore(Path(base.state_dir).expanduser() / "tokens.json")
    app = create_app(backend, jobs, tasks=tasks, tokens=tokens, hosts=tuple(args.host), user_header=args.user_header,
                     local_browser=local_browser)
    uvicorn.run(app, host=args.listen, port=args.port, log_level="warning")


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _local_zone() -> tzinfo:
    """This machine's timezone, with its summer time: TZ, or /etc/localtime, or UTC."""
    if name := os.environ.get("TZ", "").lstrip(":"):
        return ZoneInfo(name)
    try:
        with open("/etc/localtime", "rb") as handle:
            return ZoneInfo.from_file(handle, key="localtime")
    except OSError:
        return UTC


if __name__ == "__main__":
    main()
