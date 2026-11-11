"""Jobs, for the orchestrator: find them, start runs, read what the runs recorded.

A job is a directory with job.toml and prompt.md (harness/job.py). The
orchestrator is given directories to look in (`--jobs DIR`): one that is a job,
or one whose subdirectories are, such as a repo of jobs. A run is `harness job
run DIR`, so it is exactly what the command line would do, with the
credentials the job is granted and the run's output kept beside the run's
record. Runs started anywhere else are listed too: the record is the
harness's, not the orchestrator's.

It is started as a task's burst is (launch.py): a subprocess, or on a cluster
(--bursts job) a Kubernetes Job of its own, which goes on while the
orchestrator restarts. The Job's pod has the state directory but not the
orchestrator's job directories, so the job is copied into the run's directory
first and run from there: what ran is kept with the run. A restarted
orchestrator follows the Jobs of runs still under way (`recover`), and a run
whose Job is gone without an outcome is shown as interrupted.

With `schedule` on, a job whose trigger is a cron schedule is also run when it
is due, in the scheduler's timezone (harness/cron.py). Every TICK seconds it
looks at each job's latest due time within `catch_up`, and runs the job unless
a run of it started at or after that time. The run log is the only record, so
a restart neither misses a run (within `catch_up`) nor repeats one, and a time
missed for longer (the machine asleep, the orchestrator down) is skipped, not
made up. A job still running when it is due again is not run twice. A job can
be paused, which is kept beside its runs (jobs/NAME/schedule.json); one with
nothing kept starts paused when `start_paused` is set, so a new job runs only
once someone resumes it.

A job in act mode proposes plans (harness/plans.py): they are listed here, their items decided (by the page,
the API or Slack's buttons, slack.py), and once every item of a plan is decided with something approved, the
apply run is started (`harness job apply DIR --plan RUN-ID`): as any run, but calling no model. Watchers
(`watch`) hear when a plan opens, is decided, and is applied.

A run that will call a llama.cpp server of ours takes it first (servers.py): while another run has it, or it
is busy, a scheduled run is tried again on each tick (within `catch_up`), and one started by hand is refused
with the reason.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from collections.abc import Callable

from harness import credentials, cron, job, models, plans

from .launch import Launched, LaunchError, Subprocess, granted
from .servers import ModelServers, models_for, servers_for

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
OUTPUT = "output.log"
OUTPUT_TAIL = 64 * 1024
# Beside a run's record, the Job it runs in, when it is one (a restarted orchestrator follows it from this).
LAUNCH_FILE = "launch.json"


class JobsError(ValueError):
    """A job or run that does not exist, or cannot be started now."""


class ServerBusy(JobsError):
    """A model server the run needs is in use or busy: try again later."""


SCHEDULE_FILE = "schedule.json"


class JobRunner:
    TICK = 30

    def __init__(self, directories: list[Path], *, schedule: bool = False, zone: tzinfo = UTC,
                 catch_up: timedelta = timedelta(hours=1), start_paused: bool = False, launcher: Any = None,
                 servers: ModelServers | None = None) -> None:
        self.directories = [Path(d).expanduser().resolve() for d in directories]
        self.lock = threading.Lock()
        self.launcher = launcher or Subprocess()  # launch.Subprocess or launch.Jobs
        # name -> (run id, its process or Job) for runs this orchestrator started or follows, not yet ended.
        self.running: dict[str, tuple[str, Launched]] = {}
        self.schedule, self.zone, self.catch_up, self.start_paused = schedule, zone, catch_up, start_paused
        # name -> the due time last acted on, so one that could not run (a secret missing) is tried once.
        self.tried: dict[str, datetime] = {}
        self.servers = servers or ModelServers()
        self.waiting: dict[str, str] = {}  # name -> why its due run has not started yet (a model server)
        # Called (name, event, plan) when a plan is "proposed", "decided", "applying" or ends ("applied",
        # "apply_failed"); outside the lock, from the thread that did it.
        self.watchers: list[Callable[[str, str, dict[str, Any]], None]] = []

    def discover(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        for directory in self.directories:
            candidates = [directory] if (directory / job.JOB_FILE).is_file() else \
                sorted(d for d in directory.iterdir() if d.is_dir() and (d / job.JOB_FILE).is_file()) \
                if directory.is_dir() else []
            for path in candidates:
                found.setdefault(path.name, path)  # the first directory given wins a clash
        return found

    def _path(self, name: str) -> Path:
        if not NAME.fullmatch(name) or (path := self.discover().get(name)) is None:
            raise JobsError(f"No job named {name!r}.")
        return path

    def _loaded(self, path: Path) -> tuple[job.JobSpec | None, Any, str]:
        """The spec and its config, or why they could not be had."""
        try:
            spec = job.load(path)
            return spec, job.config(spec), ""
        except job.JobError as err:
            return None, None, str(err)

    def secret_names(self) -> list[str]:
        """The variables the jobs' secrets are read from, for the Settings tab."""
        names = []
        for path in self.discover().values():
            spec, _, _ = self._loaded(path)
            names += spec.secret_envs if spec else []
        return list(dict.fromkeys(names))

    def _summary(self, name: str, path: Path) -> dict[str, Any]:
        spec, cfg, error = self._loaded(path)
        item: dict[str, Any] = {"name": name, "path": str(path), "error": error, "running": name in self.running,
                                "waiting": self.waiting.get(name, "")}
        if spec is None:
            return item
        runs = job.list_runs(cfg, name, limit=20)
        missing = [name for name in spec.secret_envs if not credentials.get(name)]
        item.update(
            trigger=spec.trigger, mode=spec.mode, engine=cfg.engine, model=f"{cfg.provider} {models.model_name(cfg)}",
            outputs=list(spec.outputs), missing=missing,
            last_run=self._alive(cfg, runs[0]) if runs else None,
            last_success=next((r for r in runs if r.get("state") == "reported"), None),
        )
        if spec.mode == "act":
            newest = plans.list_plans(cfg, name, limit=1)
            item["plan"] = _plan_line(newest[0]) if newest and newest[0]["state"] in ("open", "applying") else None
        item["running"] = item["running"] or bool(runs and self._alive(cfg, runs[0]).get("state") == "running")
        if spec.trigger != "manual":
            due = cron.next_due(cron.parse(spec.trigger), self.zone, datetime.now(UTC))
            item["schedule"] = {"on": self.schedule, "paused": self.paused(cfg, name),
                                "next": _iso(due) if due else None}
        return item

    # --- schedule ---

    def _schedule_file(self, cfg: Any, name: str) -> Path:
        return job.runs_dir(cfg, name).parent / SCHEDULE_FILE

    def paused(self, cfg: Any, name: str) -> bool:
        try:
            return bool(json_file(self._schedule_file(cfg, name))["paused"])
        except (OSError, ValueError, KeyError, TypeError):
            return self.start_paused

    def pause(self, name: str, paused: bool) -> None:
        spec, cfg, error = self._loaded(self._path(name))
        if spec is None:
            raise JobsError(error)
        if spec.trigger == "manual":
            raise JobsError(f"{name} has no schedule to pause: its trigger is manual.")
        path = self._schedule_file(cfg, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".new")
        temporary.write_text(json.dumps({"paused": paused}))
        temporary.replace(path)

    def tick(self, now: datetime) -> list[str]:
        """Run each job that is due; what was done, a line each, for the log."""
        done = []
        for name, path in sorted(self.discover().items()):
            spec, cfg, _ = self._loaded(path)
            if spec is None or spec.trigger == "manual" or self.paused(cfg, name):
                continue
            due = cron.last_due(cron.parse(spec.trigger), self.zone, now, self.catch_up)
            if due is None or self.tried.get(name) == due:
                continue
            runs = job.list_runs(cfg, name, limit=1)
            if runs and _when(runs[0].get("started")) >= due:
                continue  # it has run since it was due (by us, by hand, or before a restart)
            self.tried[name] = due
            try:
                run_id = self.run(name, scheduled_for=due)
            except ServerBusy as err:
                del self.tried[name]  # tried again on the next tick, while it is within catch_up
                if self.waiting.get(name) != str(err):
                    done.append(f"{name}: due {_iso(due)}, waiting: {err}")
                self.waiting[name] = str(err)
                continue
            except JobsError as err:
                done.append(f"{name}: due {_iso(due)}, not run: {err}")
            else:
                done.append(f"{name}: due {_iso(due)}, started run {run_id}")
            self.waiting.pop(name, None)
        for name in [n for n in self.waiting if n not in self.discover()]:
            del self.waiting[name]
        return done

    def start(self) -> None:
        """Check the schedules every TICK seconds, in a thread of its own, if scheduling is on."""
        if not self.schedule:
            return

        def loop() -> None:
            while True:
                try:
                    for line in self.tick(datetime.now(UTC)):
                        print(f"schedule: {line}", file=sys.stderr, flush=True)
                except Exception as err:  # noqa: BLE001 -- the scheduler carries on; the next tick tries again
                    print(f"schedule: {type(err).__name__}: {err}", file=sys.stderr, flush=True)
                time.sleep(self.TICK)

        threading.Thread(target=loop, name="schedule", daemon=True).start()

    def jobs(self) -> list[dict[str, Any]]:
        return [self._summary(name, path) for name, path in sorted(self.discover().items())]

    def job(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        item = self._summary(name, path)
        spec, cfg, _ = self._loaded(path)
        if spec is not None:
            item["describe"] = job.describe(spec, cfg)
            item["prompt"] = spec.prompt
            item["runs"] = [self._alive(cfg, r) for r in job.list_runs(cfg, name)]
            item["plans"] = plans.list_plans(cfg, name, limit=5) if spec.mode == "act" else []
            if name in self.running and not any(r["id"] == self.running[name][0] for r in item["runs"]):
                item["runs"].insert(0, {"id": self.running[name][0], "job": name, "state": "starting"})
        return item

    def run(self, name: str, *, scheduled_for: datetime | None = None, applying: str = "") -> str:
        """Start a run; its id. One run of a job at a time. `scheduled_for`: the scheduler's,
        for the time it was due. `applying`: the run that applies that run's plan (apply())."""
        path = self._path(name)
        spec, cfg, error = self._loaded(path)
        if spec is None:
            raise JobsError(error)
        if missing := [name for name in spec.secret_envs if not credentials.get(name)]:
            raise JobsError(f"The orchestrator's environment is missing {', '.join(missing)}, which {name} needs. "
                            "Set it where the orchestrator is started.")
        with self.lock:
            if name in self.running or self._summary(name, path)["running"]:
                raise JobsError(f"{name} is already running.")
            if why := self.servers.take(f"job {name}", [] if applying else models_for(cfg)):  # applying: no model
                raise ServerBusy(f"{name} waits for its model server: {why}.")
            run_id = job.new_run_id()
            if applying:  # marked now, so it is neither decided nor applied again while the run starts
                try:
                    plans.change(cfg, name, applying, lambda plan: plans.start_applying(plan, run_id))
                except plans.PlanError as err:
                    self.servers.give_back(f"job {name}", wake=False)
                    raise JobsError(str(err)) from err
            directory = job.runs_dir(cfg, name) / run_id
            directory.mkdir(parents=True, exist_ok=True)
            if self.launcher.kind == "job":
                path = _copy(path, directory / path.name)  # a Job's pod has the state directory, not ours
            # Only what this job is granted, not every credential the orchestrator holds.
            names = credentials.for_job(cfg, spec.secret_envs, spec.outputs)
            env = credentials.for_run([], {
                job.RUN_ID_ENV: run_id, "PYTHONUNBUFFERED": "1",
                job.TRIGGER_ENV: "schedule" if scheduled_for else "approval" if applying else "manual",
                **({job.SCHEDULED_FOR_ENV: _iso(scheduled_for)} if scheduled_for else {})})
            try:
                how = ["apply", str(path), "--plan", applying] if applying else ["run", str(path)]
                launched = self.launcher.start(name, 0, [sys.executable, "-m", "harness", "job", *how],
                                               env, granted(names), directory / OUTPUT, kind="job")
            except LaunchError as err:
                with open(directory / OUTPUT, "ab") as output:
                    output.write(f"! could not start the run: {err}\n".encode())
                self.servers.give_back(f"job {name}", wake=False)  # the lock is held
                if applying:
                    plans.change(cfg, name, applying, plans.reopen)
                raise JobsError(f"Could not start {name}: {err}") from err
            if launched.job:
                (directory / LAUNCH_FILE).write_text(json.dumps(launched.record()))
            self.running[name] = (run_id, launched)
        threading.Thread(target=self._wait, args=(name, launched, run_id), daemon=True).start()
        if applying:
            self._tell(name, "applying", {**plans.load(cfg, name, applying), "state": "applying", "apply_run": run_id})
        return run_id

    def _wait(self, name: str, launched: Launched, run_id: str) -> None:
        self.launcher.wait(launched)
        with self.lock:
            if self.running.get(name, ("", None))[1] is launched:
                del self.running[name]
        self.servers.give_back(f"job {name}")
        self._ended(name, run_id)

    # --- plans ---
    def _ended(self, name: str, run_id: str) -> None:
        """After a run: tell the watchers of the plan it proposed, or of the one it applied."""
        spec, cfg, _ = self._loaded(self._path(name)) if name in self.discover() else (None, None, "")
        if spec is None or spec.mode != "act":
            return
        try:
            record = json_file(job.runs_dir(cfg, name) / run_id / "run.json")
            if record.get("applies"):
                plan = plans.load(cfg, name, str(record["applies"]))
                if plan["state"] == "applying" and plan.get("apply_run") == run_id:  # it ended without saying
                    plan = plans.change(cfg, name, plan["run"], lambda p: p.update(
                        state="apply_failed", reason=record.get("reason") or "the apply run ended without an outcome"))
                self._tell(name, plan["state"], plan)
            elif (record.get("plan") or {}).get("items"):
                self._tell(name, "proposed", plans.load(cfg, name, run_id))
        except (OSError, ValueError, plans.PlanError) as err:
            print(f"jobs: {name}: after run {run_id}: {err}", flush=True)

    def _tell(self, name: str, event: str, plan: dict[str, Any]) -> None:
        for watcher in self.watchers:
            try:
                watcher(name, event, plan)
            except Exception as err:  # noqa: BLE001 -- one watcher's trouble is not the others'
                print(f"jobs: telling of {name}'s plan {plan.get('run')}: {err}", flush=True)

    def watch(self, watcher: Callable[[str, str, dict[str, Any]], None]) -> None:
        self.watchers.append(watcher)

    def _act(self, name: str) -> Any:
        spec, cfg, error = self._loaded(self._path(name))
        if spec is None:
            raise JobsError(error)
        if spec.mode != "act":
            raise JobsError(f"{name} is not in act mode: it makes no plans.")
        return cfg

    def plan(self, name: str, run_id: str) -> dict[str, Any]:
        try:
            return plans.load(self._act(name), name, run_id)
        except plans.PlanError as err:
            raise JobsError(str(err)) from err

    def note(self, name: str, run_id: str, **fields: Any) -> None:
        """Keep something of a plan's own beside it (the Slack message it was posted as)."""
        plans.change(self._act(name), name, run_id, lambda plan: plan.update(fields))

    def decide(self, name: str, run_id: str, decisions: dict[str, str], *, by: str) -> dict[str, Any]:
        """Approve or reject items of a plan ("*" for all that are left). Once every item is decided and something
        was approved, its apply run is started: the plan comes back with `apply_run`, or with `apply_error` if it
        could not start (the job is running): apply() then starts it."""
        cfg = self._act(name)
        if not isinstance(decisions, dict) or not decisions:
            raise JobsError("Say what to decide: {\"decisions\": {\"ITEM\": \"approve\" or \"reject\", \"*\": ...}}.")
        try:
            plan = plans.decide(cfg, name, run_id, {str(k): str(v) for k, v in decisions.items()}, by=by)
        except plans.PlanError as err:
            raise JobsError(str(err)) from err
        self._tell(name, "decided", plan)
        if plan["state"] == "open" and all(item["decision"] for item in plan["items"]):
            try:
                plan = {**plan, "state": "applying", "apply_run": self.run(name, applying=run_id)}
            except JobsError as err:
                plan = {**plan, "apply_error": str(err)}
        return plan

    def apply(self, name: str, run_id: str) -> str:
        """Start applying a decided plan (when deciding could not: the job was running). The run's id."""
        self._act(name)
        return self.run(name, applying=run_id)

    def recover(self) -> list[str]:
        """On start: follow again the Jobs of runs another orchestrator left under way. A run whose Job is gone
        is left as it is, and shown as interrupted. A kubectl failure is raised (launch.Jobs.reattach)."""
        done = []
        for name, path in sorted(self.discover().items()):
            spec, cfg, _ = self._loaded(path)
            if spec is None:
                continue
            for record in job.list_runs(cfg, name, limit=5):
                launch_file = job.runs_dir(cfg, name) / str(record.get("id")) / LAUNCH_FILE
                if record.get("state") != "running" or not launch_file.is_file() or name in self.running:
                    continue
                try:
                    started = json_file(launch_file)
                except (OSError, ValueError):
                    continue
                if (launched := self.launcher.reattach(started)) is None:
                    done.append(f"{name}: run {record['id']}'s Job ({started.get('job')}) is gone, left interrupted")
                    continue
                with self.lock:
                    self.running[name] = (str(record["id"]), launched)
                self.servers.hold(f"job {name}", servers_for(cfg))
                threading.Thread(target=self._wait, args=(name, launched, str(record["id"])), daemon=True).start()
                done.append(f"{name}: run {record['id']} is still under way ({launched.job}), followed again")
        return done

    def handoff(self, token: str) -> dict[str, str] | None:
        """The credentials kept for a run's pod under this token, once (launch.Handoffs); None if there are none."""
        handoffs = getattr(self.launcher, "handoffs", None)
        return handoffs.take(token) if handoffs is not None and token else None

    def _alive(self, cfg: Any, record: dict[str, Any]) -> dict[str, Any]:
        """A record that says "running" for a run that is gone says "interrupted" instead. A run in a Job is
        alive while this orchestrator follows it; one in a subprocess, while its process is there."""
        if record.get("state") != "running":
            return record
        name, run_id = str(record.get("job") or ""), str(record.get("id") or "")
        if NAME.fullmatch(name) and job.RUN_ID.fullmatch(run_id) and \
                (job.runs_dir(cfg, name) / run_id / LAUNCH_FILE).is_file():
            followed = self.running.get(name, ("", None))[0] == run_id  # not under the lock: run() holds it
            return record if followed else {**record, "state": "interrupted",
                                            "reason": "the run's Job is gone and it left no outcome"}
        return _alive(record)

    def run_detail(self, name: str, run_id: str) -> dict[str, Any]:
        path = self._path(name)
        spec, cfg, error = self._loaded(path)
        if spec is None or not job.RUN_ID.fullmatch(run_id):
            raise JobsError(error or f"No run {run_id!r}.")
        directory = job.runs_dir(cfg, name) / run_id
        if not directory.is_dir():
            raise JobsError(f"No run {run_id!r} of {name}.")
        try:
            record = self._alive(cfg, json_file(directory / "run.json"))
        except (OSError, ValueError):
            record = {"id": run_id, "job": name, "state": "starting"}
        report = directory / "report.md"
        output = directory / OUTPUT
        return {
            **record,
            "report": report.read_text() if report.is_file() else None,
            "output": _tail(output) if output.is_file() else None,
        }


def _plan_line(plan: dict[str, Any]) -> dict[str, Any]:
    """A plan in a job's summary: whose it is, how far it is decided."""
    return {"run": plan["run"], "state": plan["state"], "items": len(plan["items"]), "expires": plan.get("expires"),
            "undecided": sum(1 for item in plan["items"] if item["decision"] is None)}


def json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(text: Any) -> datetime:
    """A record's UTC time, or the distant past if it has none."""
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)


def _alive(record: dict[str, Any]) -> dict[str, Any]:
    """A record that says "running" for a process that is gone says "interrupted" instead."""
    if record.get("state") != "running" or not record.get("pid"):
        return record
    try:
        os.kill(int(record["pid"]), 0)
    except (OSError, ValueError):
        return {**record, "state": "interrupted", "reason": "the run's process is gone and it left no outcome"}
    return record


def _copy(source: Path, destination: Path) -> Path:
    """A job's directory, its files' contents (a ConfigMap's are links into its ..data), without a repo's .git."""
    shutil.copytree(source, destination, ignore=lambda _, names: [n for n in names if n.startswith("..") or n == ".git"])
    return destination


def _tail(path: Path) -> str:
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - OUTPUT_TAIL))
        text = handle.read().decode(errors="replace")
    return ("…(earlier output cut)\n" if size > OUTPUT_TAIL else "") + text
