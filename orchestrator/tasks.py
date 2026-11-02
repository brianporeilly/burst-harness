"""Tasks, for the orchestrator: a repo and a prompt, worked on until there is a PR.

A task is `harness run SLUG --task PROMPT` against a repo URL, as a subprocess,
exactly what the command line would do, with the orchestrator's environment
(its credentials, its target: podman or docker here, agent-sandbox on a
cluster). Each run of it is a burst. A burst ends in a PR, a pause (the work pushed, with
questions in the task document), an abandon, or no terminal state at all.
Continuing a task is another burst, `harness run SLUG --message REPLY`, which
picks up from the pushed branch in a fresh sandbox, so nothing waits in memory.

The orchestrator keeps a record per task (tasks/SLUG/task.json under the state
directory) and the bursts' output beside it. At most `limit` tasks run at once;
the rest wait in the order they came, in their records, so a restart keeps the
queue. A burst that was running when the orchestrator stopped is queued again
on the next start, up to RESTARTS times, which picks the task up from its
branch: what was pushed is kept, anything not yet pushed is redone.

A task with no repo is a question (harness/ask.py): each burst is
`harness ask SLUG --task PROMPT`, one run in a fresh sandbox that ends in an
answer, kept with the burst. Continuing it with a message is a follow-up,
answered with the earlier questions and answers in view; continuing one that
gave no answer asks it again.

A burst that ends with work it has not pushed (it failed, or was stopped)
keeps its sandbox, and says so. The next burst is attached to that sandbox, so
it carries on with the work there; once a burst pushes (a PR, a pause) or
gives up, or the task is removed, the orchestrator releases it.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness import ask, credentials, git, job
from harness.config import HarnessConfig

from .backend import SpecError, check_model, model_choice
from .jobs import OUTPUT, REPO, _iso, _tail, json_file

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
# owner/repo on GitHub, or a repo's https URL.
SHORT_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+")
URL_REPO = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9._/-]+")
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")
ENGINES = ("strands", "claude-code")
RESULT = "HARNESS_RESULT "
JOB_RESULT = "HARNESS_JOB "  # what a question's burst ends with
# A sandbox a burst kept, as the harness gives the flags that reattach to it. The output carries what
# the model printed too, so only these shapes are taken from it.
SANDBOX = re.compile(r"--target (?:podman|docker) --docker-container burst-[a-z0-9-]{1,60}"
                     r"|--target agent-sandbox --claim [a-z0-9][a-z0-9-]{0,62}")
# The burst ended where a kept sandbox is no longer needed: the work is pushed, or given up.
FINISHED = ("pr", "pushed_no_pr", "paused", "abandoned")
TASK_FILE = "task.json"
# How many times a burst cut short by the orchestrator stopping is started again.
RESTARTS = 2
# How long a cancelled burst gets to release its sandbox before it is killed.
CANCEL_GRACE = 30
# States in which nothing is running or waiting to run.
IDLE = ("pr", "pushed_no_pr", "paused", "abandoned", "waiting", "max_tokens", "failed", "interrupted", "cancelled",
        "answered")


class TasksError(ValueError):
    """A task that does not exist, or a request that cannot be done now."""


def repo_url(text: str) -> str:
    text = text.strip().removesuffix("/")
    if SHORT_REPO.fullmatch(text):
        return f"https://github.com/{text.removesuffix('.git')}.git"
    if URL_REPO.fullmatch(text) and ".." not in text:
        return text
    raise TasksError(f"{text!r} is not a repo: give owner/repo (GitHub) or its https URL.")


def slug_for(prompt: str) -> str:
    """A slug from the prompt's first words, made unique with a few random characters."""
    words = re.findall(r"[a-z0-9]+", prompt.lower())[:5]
    stem = "-".join(words)[:36].strip("-") or "task"
    return f"{stem}-{secrets.token_hex(2)}"


class TaskRunner:
    def __init__(self, base: HarnessConfig, *, limit: int = 1, claim_ttl: int = 4 * 3600) -> None:
        self.base, self.limit, self.claim_ttl = base, max(1, limit), claim_ttl
        self.root = base.resolve_state_dir("tasks")
        self.lock = threading.Lock()
        # slug -> process, for bursts this orchestrator started and that have not ended.
        self.running: dict[str, subprocess.Popen[bytes]] = {}
        # Marks the records of bursts this orchestrator started, so one left "running" by another is known.
        self.boot = secrets.token_hex(6)

    # --- records ---

    def _dir(self, slug: str) -> Path:
        if not SLUG.fullmatch(slug):
            raise TasksError(f"No task named {slug!r}.")
        return self.root / slug

    def _read(self, slug: str) -> dict[str, Any]:
        try:
            return json_file(self._dir(slug) / TASK_FILE)
        except (OSError, ValueError) as err:
            raise TasksError(f"No task named {slug!r}.") from err

    def _write(self, record: dict[str, Any]) -> None:
        directory = self._dir(record["slug"])
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / (TASK_FILE + ".new")
        temporary.write_text(json.dumps(record, indent=1))
        temporary.replace(directory / TASK_FILE)

    def _records(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        found = []
        for directory in self.root.iterdir():
            try:
                found.append(json_file(directory / TASK_FILE))
            except (OSError, ValueError):
                continue
        return sorted(found, key=lambda r: r.get("created", ""), reverse=True)

    # --- what the API does ---

    def tasks(self) -> list[dict[str, Any]]:
        return [_summary(r) for r in self._records()]

    def task(self, slug: str) -> dict[str, Any]:
        record = self._read(slug)
        output = self._dir(slug) / OUTPUT
        return {**record, "output": _tail(output) if output.is_file() else None}

    def create(self, raw: dict[str, Any], *, user: str = "") -> dict[str, Any]:
        prompt = str(raw.get("prompt") or "").strip()
        if not prompt:
            raise TasksError("Say what to do: a prompt.")
        # No repo: a question, answered rather than turned into a PR.
        url = repo_url(str(raw["repo"])) if str(raw.get("repo") or "").strip() else None
        slug = str(raw.get("slug") or "").strip() or slug_for(prompt)
        if not SLUG.fullmatch(slug):
            raise TasksError(f"{slug!r} is not a slug: lowercase letters, digits and dashes, up to 48.")
        if url is None and self._asked(slug).exists():
            raise TasksError(f"A question named {slug} was asked before (from the command line?): pick another name.")
        engine = str(raw.get("engine") or self.base.engine)
        if engine not in ENGINES:
            raise TasksError(f"The engine is one of {', '.join(ENGINES)}.")
        base_branch = str(raw.get("base_branch") or "").strip()
        if base_branch and not BRANCH.fullmatch(base_branch):
            raise TasksError(f"{base_branch!r} is not a branch name.")
        if url is None:
            base_branch = ""
        elif not base_branch:
            # The repo's own default, not a guess: and a repo that cannot be read is refused now, not in a burst.
            try:
                base_branch = git.default_branch(url)
            except git.GitError as err:
                raise TasksError(f"{err} (the orchestrator reads it with its git token, if it has one)") from err
        provider, base_url, model = (str(raw.get(key) or "").strip() for key in ("provider", "base_url", "model"))
        try:
            context_window = int(raw.get("context_window") or 0)
            if context_window < 0:
                raise ValueError
            if engine == "claude-code":
                if provider not in ("", "anthropic"):
                    raise SpecError("Claude Code always uses Anthropic (the orchestrator's Claude token or key): "
                                    "leave the provider as it is, or use the Strands engine.")
                provider = "anthropic"  # and not the orchestrator's other provider's URL or model
            check_model(provider, base_url, model)
            chosen = model_choice(self.base, provider, base_url, model, context_window)
        except ValueError as err:  # SpecError is one
            raise TasksError(str(err) if isinstance(err, SpecError) else "The context window is a number of tokens.") from err
        record = {
            "slug": slug, "repo": url, "prompt": prompt, "engine": engine, "model": model,
            "provider": provider, "base_url": base_url, "context_window": context_window,
            # What that comes to, the orchestrator's defaults filled in: for showing which model works on it.
            "uses": {"provider": chosen.get("provider", self.base.provider),
                     "model": chosen.get("model_id", self.base.model_id),
                     "base_url": chosen.get("base_url", self.base.base_url)},
            "base_branch": base_branch, "draft": bool(raw.get("draft")) and url is not None,
            "branch": self.base.branch_prefix + slug if url else None, "created": _now(), "created_by": user,
            "state": "queued", "queued": time.time_ns(), "pr": None, "message": None, "restarts": 0, "bursts": [],
            "sandbox": None,
        }
        with self.lock:
            if (self._dir(slug) / TASK_FILE).exists():
                raise TasksError(f"There is already a task named {slug}: continue it, or pick another name.")
            self._write(record)
        self.dispatch()
        return _summary(self._read(slug))

    def resume(self, slug: str, message: str = "") -> dict[str, Any]:
        """Queue another burst: with the user's reply, or to carry on from where it stopped."""
        with self.lock:
            record = self._read(slug)
            if record["state"] not in IDLE:
                raise TasksError(f"{slug} is {record['state']}: wait for it to stop first.")
            if record["state"] == "answered" and not message.strip():
                raise TasksError(f"{slug} is answered: say what to ask next.")
            record.update(state="queued", queued=time.time_ns(), message=message.strip() or None, restarts=0)
            self._write(record)
        self.dispatch()
        return _summary(self._read(slug))

    def cancel(self, slug: str) -> dict[str, Any]:
        with self.lock:
            record = self._read(slug)
            if record["state"] == "queued":
                record["state"] = "cancelled"
                self._write(record)
                return _summary(record)
            process = self.running.get(slug)
            if record["state"] != "running" or process is None:
                raise TasksError(f"{slug} is not running or queued.")
            record["cancelling"] = True
            self._write(record)
        # SIGINT, as Ctrl-C would: the harness releases its sandbox (or keeps one with uncommitted work).
        os.killpg(process.pid, signal.SIGINT)
        threading.Timer(CANCEL_GRACE, _kill, args=(process,)).start()
        return _summary(record)

    def remove(self, slug: str) -> None:
        with self.lock:
            record = self._read(slug)
            if record["state"] not in IDLE:
                raise TasksError(f"{slug} is {record['state']}: cancel it first.")
            if record.get("sandbox"):
                self._release(slug, record["sandbox"])
            directory = self._dir(slug)
            for path in directory.iterdir():
                path.unlink()
            directory.rmdir()
            if not record.get("repo"):
                shutil.rmtree(self._asked(slug), ignore_errors=True)  # its conversation, which a follow-up reads

    def _asked(self, slug: str) -> Path:
        """Where a question's runs are kept (harness/ask.py)."""
        return job.runs_dir(self.base, slug, ask.ROOT).parent

    # --- running ---

    def recover(self) -> list[str]:
        """On start: a burst another orchestrator left running is queued again, or failed after RESTARTS."""
        done = []
        with self.lock:
            for record in self._records():
                if record.get("state") != "running" or record.get("boot") == self.boot:
                    continue
                if record.get("bursts"):
                    record["bursts"][-1].update(ended=_now(), state="interrupted")
                if record.get("cancelling"):
                    record.update(state="cancelled", cancelling=False)
                elif record.get("restarts", 0) < RESTARTS:
                    record.update(state="queued", restarts=record.get("restarts", 0) + 1)
                    done.append(f"{record['slug']}: cut short by a restart, queued again")
                else:
                    record["state"] = "interrupted"
                    done.append(f"{record['slug']}: cut short by a restart {RESTARTS + 1} times, left interrupted")
                self._write(record)
        return done

    def dispatch(self) -> None:
        """Start queued tasks, oldest first, while fewer than `limit` are running."""
        with self.lock:
            queued = sorted((r for r in self._records() if r.get("state") == "queued"), key=lambda r: r.get("queued", 0))
            for record in queued[:max(0, self.limit - len(self.running))]:
                self._start(record)

    def _argv(self, record: dict[str, Any]) -> list[str]:
        argv = [sys.executable, "-m", "harness", "--engine", record["engine"], "--claim-ttl", str(self.claim_ttl)]
        if self.base.target in ("local", "podman", "docker"):
            # Never on the orchestrator's own machine: in a container, through a gateway.
            runtime = "docker" if self.base.target == "docker" else "podman"
            argv += ["--target", runtime] + (["--egress", "allowlist"] if self.base.egress == "open" else [])
        elif not record.get("repo") and self.base.egress == "open":
            argv += ["--egress", "allowlist"]  # a question runs as a job does: through a gateway
        if not record.get("repo"):
            return argv + ["ask", record["slug"], "--task", record["prompt"]] + (
                ["--message", record["message"]] if record.get("message") else [])
        argv += ["--repo-url", record["repo"], "--draft-pr" if record.get("draft") else "--no-draft-pr"]
        if record.get("base_branch"):
            argv += ["--base-branch", record["base_branch"]]
        if record.get("sandbox"):
            argv += shlex.split(record["sandbox"])  # the sandbox the last burst kept, after --target to win
        argv += ["run", record["slug"], "--task", record["prompt"]]
        if record.get("message"):
            argv += ["--message", record["message"]]
        return argv

    def _model_env(self, record: dict[str, Any]) -> dict[str, str]:
        """The task's provider and model as the burst's HARNESS_* settings, over the orchestrator's."""
        chosen = model_choice(self.base, record.get("provider") or "", record.get("base_url") or "",
                              record.get("model") or "", record.get("context_window") or 0)
        if chosen:
            chosen["router"] = ""  # a task given its model is not routed (harness/agents.py)
        elif not record.get("repo") and (record.get("uses") or {}).get("agent"):
            # A question's follow-ups stay with the profile the router chose for it (a repo's task keeps
            # its own choice, in the harness's state).
            chosen = {"agent": record["uses"]["agent"], "router": ""}
        return {f"HARNESS_{name.upper()}": str(value) for name, value in chosen.items()}

    def _start(self, record: dict[str, Any]) -> None:
        """Start a burst of a queued task. Called with the lock held."""
        directory = self._dir(record["slug"])
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / OUTPUT, "ab") as output:
            heading = f"\n=== burst {len(record['bursts']) + 1}, {_now()}"
            output.write((heading + (f": {record['message']}" if record.get("message") else "") + " ===\n").encode())
            output.flush()
            # A question's run is found by its id, which is set here.
            run = {} if record.get("repo") else {job.RUN_ID_ENV: job.new_run_id()}
            process = subprocess.Popen(
                self._argv(record), cwd=REPO,
                env=credentials.for_run(credentials.for_task(self.base),
                                        {"PYTHONUNBUFFERED": "1", **self._model_env(record), **run}),
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
            )
        record["bursts"].append({"started": _now(), "message": record.get("message"),
                                 **({"run": run[job.RUN_ID_ENV]} if run else {})})
        record.update(state="running", boot=self.boot, pid=process.pid)
        self._write(record)
        self.running[record["slug"]] = process
        threading.Thread(target=self._wait, args=(record["slug"], process), daemon=True).start()

    def _wait(self, slug: str, process: subprocess.Popen[bytes]) -> None:
        code = process.wait()
        try:
            if not self._read(slug).get("repo"):
                self._answered(slug, code)
                return
        except TasksError:
            pass  # removed meanwhile
        result = _result(self._dir(slug) / OUTPUT)
        done_with = None
        with self.lock:
            self.running.pop(slug, None)
            try:
                record = self._read(slug)
            except TasksError:
                record = None  # removed meanwhile
            if record is not None:
                state = "cancelled" if record.get("cancelling") else _state(code, result)
                record["bursts"][-1].update(ended=_now(), exit=code, state=state, delegated=result.get("delegated"))
                if result.get("agent"):  # routed to a profile (harness/agents.py): that is what works on it
                    provider, _, model = str(result.get("model") or "").partition(" ")
                    record["uses"] = {"provider": provider, "model": model, "base_url": result.get("base_url") or "",
                                      "agent": result["agent"]}
                record.update(state=state, pr=result.get("pr") or record.get("pr"), message=None, cancelling=False)
                record.pop("pid", None)
                kept = str(result.get("target") or "")
                if result.get("kept") and SANDBOX.fullmatch(kept):
                    record["sandbox"] = kept
                elif record.get("sandbox") and state in FINISHED:
                    # Attached to an earlier burst's sandbox, which the harness leaves alone: release it here.
                    done_with, record["sandbox"] = record["sandbox"], None
                self._write(record)
        if done_with:
            self._release(slug, done_with)
        self.dispatch()

    def _answered(self, slug: str, code: int) -> None:
        """A question's burst ended: keep its answer, if it gave one."""
        result = _result(self._dir(slug) / OUTPUT, JOB_RESULT)
        with self.lock:
            self.running.pop(slug, None)
            try:
                record = self._read(slug)
            except TasksError:
                record = None
            if record is not None:
                burst = record["bursts"][-1]
                report = job.runs_dir(self.base, slug, ask.ROOT) / str(burst.get("run")) / "report.md"
                answer = report.read_text() if burst.get("run") and report.is_file() else None
                state = "cancelled" if record.get("cancelling") else "answered" if answer is not None else "failed"
                burst.update(ended=_now(), exit=code, state=state, answer=answer, reason=result.get("reason"),
                             delegated=result.get("delegated"))
                if result.get("agent"):
                    provider, _, model = str(result.get("model") or "").partition(" ")
                    record["uses"] = {**record.get("uses", {}), "provider": provider, "model": model,
                                      "agent": result["agent"]}
                record.update(state=state, message=None, cancelling=False)
                record.pop("pid", None)
                self._write(record)
        self.dispatch()

    def _release(self, slug: str, sandbox: str) -> None:
        """Remove a kept sandbox (`harness ... release`), noting it in the task's output."""
        with open(self._dir(slug) / OUTPUT, "ab") as output:
            output.write(f"\n=== releasing the kept sandbox ({sandbox}) ===\n".encode())
            output.flush()
            subprocess.run([sys.executable, "-m", "harness", *shlex.split(sandbox), "release"], cwd=REPO,
                           env=credentials.for_run(credentials.for_task(self.base), {"PYTHONUNBUFFERED": "1"}),
                           stdin=subprocess.DEVNULL, stdout=output,
                           stderr=subprocess.STDOUT, timeout=300, check=False)


def _state(code: int, result: dict[str, Any]) -> str:
    """What a burst ended in: its HARNESS_RESULT's terminal state, or failed when it has none."""
    state = result.get("state")
    if state in (*FINISHED, "waiting", "max_tokens"):
        return state
    return "failed"


def _result(output: Path, prefix: str = RESULT) -> dict[str, Any]:
    """The last HARNESS_RESULT (or `prefix`) line in the output, or {} if the burst printed none."""
    try:
        text = _tail(output)
    except OSError:
        return {}
    for line in reversed(text.splitlines()):
        if line.startswith(prefix):
            try:
                return json.loads(line[len(prefix):])
            except ValueError:
                return {}
    return {}


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in (
        "slug", "repo", "prompt", "engine", "provider", "base_url", "model", "context_window", "uses", "base_branch", "draft", "branch", "created", "created_by",
        "state", "pr", "message", "restarts", "sandbox")} | {"bursts": len(record.get("bursts", [])),
                                                    "last_burst": (record.get("bursts") or [None])[-1]}


def _kill(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass


def _now() -> str:
    return _iso(datetime.now(UTC))
