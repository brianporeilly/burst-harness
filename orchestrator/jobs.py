"""Jobs, for the orchestrator: find them, start runs, read what the runs recorded.

A job is a directory with job.toml and prompt.md (harness/job.py). The
orchestrator is given directories to look in (`--jobs DIR`): one that is a job,
or one whose subdirectories are, such as a repo of jobs. A run is `harness job
run DIR` as a subprocess, so it is exactly what the command line would do, with
the orchestrator's environment (its credentials) and the run's output kept
beside the run's record. Runs started anywhere else are listed too: the record
is the harness's, not the orchestrator's.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from harness import job

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
OUTPUT = "output.log"
OUTPUT_TAIL = 64 * 1024
REPO = Path(__file__).resolve().parent.parent


class JobsError(ValueError):
    """A job or run that does not exist, or cannot be started now."""


class JobRunner:
    def __init__(self, directories: list[Path]) -> None:
        self.directories = [Path(d).expanduser().resolve() for d in directories]
        self.lock = threading.Lock()
        # name -> (run id, process) for runs this orchestrator started and that have not ended.
        self.running: dict[str, tuple[str, subprocess.Popen[bytes]]] = {}

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

    def _summary(self, name: str, path: Path) -> dict[str, Any]:
        spec, cfg, error = self._loaded(path)
        item: dict[str, Any] = {"name": name, "path": str(path), "error": error, "running": name in self.running}
        if spec is None:
            return item
        runs = job.list_runs(cfg, name, limit=20)
        missing = [s.env for s in spec.secrets if not os.environ.get(s.env)]
        item.update(
            trigger=spec.trigger, mode=spec.mode, engine=cfg.engine, model=f"{cfg.provider} {cfg.model_id}",
            outputs=list(spec.outputs), missing=missing,
            last_run=_alive(runs[0]) if runs else None,
            last_success=next((r for r in runs if r.get("state") == "reported"), None),
        )
        item["running"] = item["running"] or bool(runs and _alive(runs[0]).get("state") == "running")
        return item

    def jobs(self) -> list[dict[str, Any]]:
        return [self._summary(name, path) for name, path in sorted(self.discover().items())]

    def job(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        item = self._summary(name, path)
        spec, cfg, _ = self._loaded(path)
        if spec is not None:
            item["describe"] = job.describe(spec, cfg)
            item["prompt"] = spec.prompt
            item["runs"] = [_alive(r) for r in job.list_runs(cfg, name)]
            if name in self.running and not any(r["id"] == self.running[name][0] for r in item["runs"]):
                item["runs"].insert(0, {"id": self.running[name][0], "job": name, "state": "starting"})
        return item

    def run(self, name: str) -> str:
        """Start a run; its id. One run of a job at a time."""
        path = self._path(name)
        spec, cfg, error = self._loaded(path)
        if spec is None:
            raise JobsError(error)
        if missing := [s.env for s in spec.secrets if not os.environ.get(s.env)]:
            raise JobsError(f"The orchestrator's environment is missing {', '.join(missing)}, which {name} needs. "
                            "Set it where the orchestrator is started.")
        with self.lock:
            if name in self.running or self._summary(name, path)["running"]:
                raise JobsError(f"{name} is already running.")
            run_id = job.new_run_id()
            directory = job.runs_dir(cfg, name) / run_id
            directory.mkdir(parents=True, exist_ok=True)
            with open(directory / OUTPUT, "wb") as output:
                process = subprocess.Popen(
                    [sys.executable, "-m", "harness", "job", "run", str(path)],
                    cwd=REPO, env={**os.environ, job.RUN_ID_ENV: run_id, "PYTHONUNBUFFERED": "1"},
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                )
            self.running[name] = (run_id, process)
        threading.Thread(target=self._wait, args=(name, process), daemon=True).start()
        return run_id

    def _wait(self, name: str, process: subprocess.Popen[bytes]) -> None:
        process.wait()
        with self.lock:
            if self.running.get(name, ("", None))[1] is process:
                del self.running[name]

    def run_detail(self, name: str, run_id: str) -> dict[str, Any]:
        path = self._path(name)
        spec, cfg, error = self._loaded(path)
        if spec is None or not job.RUN_ID.fullmatch(run_id):
            raise JobsError(error or f"No run {run_id!r}.")
        directory = job.runs_dir(cfg, name) / run_id
        if not directory.is_dir():
            raise JobsError(f"No run {run_id!r} of {name}.")
        try:
            record = _alive(json_file(directory / "run.json"))
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


def _alive(record: dict[str, Any]) -> dict[str, Any]:
    """A record that says "running" for a process that is gone says "interrupted" instead."""
    if record.get("state") != "running" or not record.get("pid"):
        return record
    try:
        os.kill(int(record["pid"]), 0)
    except (OSError, ValueError):
        return {**record, "state": "interrupted", "reason": "the run's process is gone and it left no outcome"}
    return record


def _tail(path: Path) -> str:
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - OUTPUT_TAIL))
        text = handle.read().decode(errors="replace")
    return ("…(earlier output cut)\n" if size > OUTPUT_TAIL else "") + text
