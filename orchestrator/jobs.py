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

from harness import credentials, cron, job, models

from .launch import Launched, LaunchError, Subprocess, granted

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
OUTPUT = "output.log"
OUTPUT_TAIL = 64 * 1024
# Beside a run's record, the Job it runs in, when it is one (a restarted orchestrator follows it from this).
LAUNCH_FILE = "launch.json"


class JobsError(ValueError):
    """A job or run that does not exist, or cannot be started now."""


SCHEDULE_FILE = "schedule.json"


class JobRunner:
    TICK = 30

    def __init__(self, directories: list[Path], *, schedule: bool = False, zone: tzinfo = UTC,
                 catch_up: timedelta = timedelta(hours=1), start_paused: bool = False, launcher: Any = None) -> None:
        self.directories = [Path(d).expanduser().resolve() for d in directories]
        self.lock = threading.Lock()
        self.launcher = launcher or Subprocess()  # launch.Subprocess or launch.Jobs
        # name -> (run id, its process or Job) for runs this orchestrator started or follows, not yet ended.
        self.running: dict[str, tuple[str, Launched]] = {}
        self.schedule, self.zone, self.catch_up, self.start_paused = schedule, zone, catch_up, start_paused
        # name -> the due time last acted on, so one that could not run (a secret missing) is tried once.
        self.tried: dict[str, datetime] = {}

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
            names += [s.env for s in spec.secrets] if spec else []
        return list(dict.fromkeys(names))

    def _summary(self, name: str, path: Path) -> dict[str, Any]:
        spec, cfg, error = self._loaded(path)
        item: dict[str, Any] = {"name": name, "path": str(path), "error": error, "running": name in self.running}
        if spec is None:
            return item
        runs = job.list_runs(cfg, name, limit=20)
        missing = [s.env for s in spec.secrets if not credentials.get(s.env)]
        item.update(
            trigger=spec.trigger, mode=spec.mode, engine=cfg.engine, model=f"{cfg.provider} {models.model_name(cfg)}",
            outputs=list(spec.outputs), missing=missing,
            last_run=self._alive(cfg, runs[0]) if runs else None,
            last_success=next((r for r in runs if r.get("state") == "reported"), None),
        )
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
                done.append(f"{name}: due {_iso(due)}, started run {self.run(name, scheduled_for=due)}")
            except JobsError as err:
                done.append(f"{name}: due {_iso(due)}, not run: {err}")
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
            if name in self.running and not any(r["id"] == self.running[name][0] for r in item["runs"]):
                item["runs"].insert(0, {"id": self.running[name][0], "job": name, "state": "starting"})
        return item

    def run(self, name: str, *, scheduled_for: datetime | None = None) -> str:
        """Start a run; its id. One run of a job at a time. `scheduled_for`: the scheduler's,
        for the time it was due."""
        path = self._path(name)
        spec, cfg, error = self._loaded(path)
        if spec is None:
            raise JobsError(error)
        if missing := [s.env for s in spec.secrets if not credentials.get(s.env)]:
            raise JobsError(f"The orchestrator's environment is missing {', '.join(missing)}, which {name} needs. "
                            "Set it where the orchestrator is started.")
        with self.lock:
            if name in self.running or self._summary(name, path)["running"]:
                raise JobsError(f"{name} is already running.")
            run_id = job.new_run_id()
            directory = job.runs_dir(cfg, name) / run_id
            directory.mkdir(parents=True, exist_ok=True)
            if self.launcher.kind == "job":
                path = _copy(path, directory / path.name)  # a Job's pod has the state directory, not ours
            # Only what this job is granted, not every credential the orchestrator holds.
            names = credentials.for_job(cfg, [s.env for s in spec.secrets], spec.outputs)
            env = credentials.for_run([], {
                job.RUN_ID_ENV: run_id, "PYTHONUNBUFFERED": "1",
                job.TRIGGER_ENV: "schedule" if scheduled_for else "manual",
                **({job.SCHEDULED_FOR_ENV: _iso(scheduled_for)} if scheduled_for else {})})
            try:
                launched = self.launcher.start(name, 0, [sys.executable, "-m", "harness", "job", "run", str(path)],
                                               env, granted(names), directory / OUTPUT, kind="job")
            except LaunchError as err:
                with open(directory / OUTPUT, "ab") as output:
                    output.write(f"! could not start the run: {err}\n".encode())
                raise JobsError(f"Could not start {name}: {err}") from err
            if launched.job:
                (directory / LAUNCH_FILE).write_text(json.dumps(launched.record()))
            self.running[name] = (run_id, launched)
        threading.Thread(target=self._wait, args=(name, launched), daemon=True).start()
        return run_id

    def _wait(self, name: str, launched: Launched) -> None:
        self.launcher.wait(launched)
        with self.lock:
            if self.running.get(name, ("", None))[1] is launched:
                del self.running[name]

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
                threading.Thread(target=self._wait, args=(name, launched), daemon=True).start()
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
