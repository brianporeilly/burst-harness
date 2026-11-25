"""Tasks, for the orchestrator: a repo and a prompt, worked on until there is a PR.

A task is `harness run SLUG --task PROMPT` against a repo URL, as a subprocess
or on a cluster a Kubernetes Job (launch.py), exactly what the command line would do, with the orchestrator's environment
(its credentials, its target: podman or docker here, agent-sandbox on a
cluster). Each run of it is a burst. A burst ends in a PR, a pause (the work pushed, with
questions in the task document), an abandon, or no terminal state at all.
Continuing a task is another burst, `harness run SLUG --message REPLY`, which
picks up from the pushed branch in a fresh sandbox, so nothing waits in memory.

The orchestrator keeps a record per task (tasks/SLUG/task.json under its own
state directory, settings.orchestrator_dir) and the bursts' output in
tasks/SLUG/ under the harness's, which a burst run as a Job shares. At most `limit` tasks run at once;
the rest wait in the order they came, in their records, so a restart keeps the
queue. A burst that was running when the orchestrator stopped is followed
again if it is a Job that is still there, and otherwise queued again on the
next start, up to RESTARTS times, which picks the task up from its
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

Whatever wants to hear about a task's bursts (the Slack bot, slack.py) is
told with `watch`: each burst's start and end, with the task's record. A task
started from Slack keeps where to answer in its record (`slack`). One started elsewhere (the page, the API)
with `notify` is told to the listed Slack users when a burst of it ends (but for a cancel): the bot keeps the
direct messages it said that in (`dm`), and its later bursts are told in their threads, where a reply
continues it, as in a task started from Slack.

A task's PR is looked at on GitHub while the task is idle (when its page is read, at most every
PR_FRESH seconds, and every PR_CHECK seconds in the background): once it is merged, the task is
`merged`, and continuing it starts its next round rather than another burst on the merged branch. A
round is a fresh branch from the base (the first round's branch, with -2, -3... after it) and a PR of
its own; its first burst carries on from the task document, which came to the base with the merge
(harness `run --branch`). The task keeps its earlier rounds' branches and PRs (`rounds`).

A task can opt in to carrying on by itself (`auto_continue`): when a look finds the PR of a round that
ended in a PR merged, the next round is started then, as if Continue had been pressed with no message.
A round that ends any other way (paused, waiting on a question, abandoned, failed) stops there, so the
task runs until the work runs out or it needs someone.

A task can opt in to a review loop (`auto_review`: a reviewer, Claude Code with a model or a profile, and how
many times its findings may be fixed, `max_cycles`). When a burst ends in a PR, a review of it is queued (as the
Review button does: harness review, posted as the forge's reviewer). When the reviewer asks for changes, the
task carries on by itself, the review in its next burst's prompt, and that burst's PR is reviewed again. When it
approves, a person merges (the gate at each milestone: nothing is merged for them). The loop stops, for a person
to look, when the reviewer still asks for changes after `max_cycles` fixes, raises a blocking point again that
was raised before (the author and the reviewer disagree), gives no verdict, or fails. Where it stands is the
task's `review_status`. A person continuing the task starts the count again; a new round starts it afresh.

A task can run on a runner (runners.py, harness/runner.py), a host of your own, which it keeps for every burst:
the one it picked, or Settings' default runner when it picked none. On a podman or docker runner it is a
container there, with its gateway as here; on a plain host it has no gateway, so its network is open, and a
question (which reads through a gateway) or a task asking for a recorded or wider network cannot go there.

A burst that will call a llama.cpp server of ours takes it first (servers.py). While another run has it, or
it is busy, the task stays queued with the reason in its record (`server_wait`), and is tried again when a
server is given back, and every RETRY seconds (`start`), since what keeps a server busy may not be ours.
A queued task says what it waits for (`wait`, worked out when it is read): that, or a task slot, with
`limit` running.
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any

from harness import ask, credentials, egress, git, job, models, runner
from harness.config import HarnessConfig
from harness.gateway_control import Gateway, GatewayError

from .backend import SpecError, check_model, model_choice, model_settings, task_providers
from .jobs import OUTPUT, _iso, _tail, json_file
from .launch import REPO, Launched, LaunchError, Subprocess, granted
from .mcp_servers import McpServersError, McpServerStore
from .profiles import ProfilesError, ProfileStore, check, names_given
from .runners import RunnerStore, RunnersError
from .task_settings import MODEL_CHARS, TaskSettings
from .servers import ModelServers, models_for, servers_for

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
# owner/repo on GitHub, or a repo's https URL.
SHORT_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+")
URL_REPO = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?/[A-Za-z0-9._/-]+")
BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")
ENGINES = ("strands", "claude-code")
RESULT = "HARNESS_RESULT "
SANDBOX_LINE = "HARNESS_SANDBOX "  # which sandbox the gateway knows a burst by (harness/agent_sandbox.py)
EGRESS_MODES = ("allowlist", "log")
ALLOW_SCOPES = ("now", "task", "everywhere")
JOB_RESULT = "HARNESS_JOB "  # what a question's burst ends with
# A sandbox a burst kept, as the harness gives the flags that reattach to it. The output carries what
# the model printed too, so only these shapes are taken from it.
SANDBOX = re.compile(r"(?:--runner [a-z][a-z0-9_-]{0,31} )?--target (?:podman|docker) --docker-container burst-[a-z0-9-]{1,60}"
                     r"|--target agent-sandbox --claim [a-z0-9][a-z0-9-]{0,62}"
                     r"|--runner [a-z][a-z0-9_-]{0,31} --runner-workdir /[A-Za-z0-9_@%+=:./-]{1,300}")
# The directory a plain-host runner made for a run: its last part, which harness/runner.acquire names.
RUN_DIR = re.compile(r"[a-z0-9-]{1,40}-[0-9a-f]{6}")
# The burst ended where a kept sandbox is no longer needed: the work is pushed, or given up.
FINISHED = ("pr", "pushed_no_pr", "paused", "abandoned")
TASK_FILE = "task.json"
# How many times a burst cut short by the orchestrator stopping is started again.
RESTARTS = 2
# Who asks for a review the loop starts (auto_review).
AUTO_REVIEW = "auto-review"
AUTO_REVIEW_CYCLES = 3
# A reviewer marks a blocking point it raised before, and still sees, so (review.PR_SYSTEM).
STILL_BLOCKING = re.compile(r"\bstill blocking\b", re.IGNORECASE)
# States in which nothing is running or waiting to run.
IDLE = ("pr", "pushed_no_pr", "push_failed", "paused", "abandoned", "waiting", "max_tokens", "failed", "interrupted", "cancelled",
        "answered", "merged")
# How old a look at an idle task's PR may be when its page is read, and between looks in the background (s).
PR_FRESH = 60
PR_CHECK = 300


class TasksError(ValueError):
    """A task that does not exist, or a request that cannot be done now."""


def repo_url(text: str) -> str:
    text = text.strip().removesuffix("/")
    if SHORT_REPO.fullmatch(text):
        return f"https://github.com/{text.removesuffix('.git')}.git"
    if URL_REPO.fullmatch(text) and ".." not in text:
        return text
    raise TasksError(f"{text!r} is not a repo: give owner/repo (GitHub) or its https URL (GitHub, or a forge).")


def slug_for(prompt: str) -> str:
    """A slug from the prompt's first words, made unique with a few random characters."""
    words = re.findall(r"[a-z0-9]+", prompt.lower())[:5]
    stem = "-".join(words)[:36].strip("-") or "task"
    return f"{stem}-{secrets.token_hex(2)}"


class TaskRunner:
    RETRY = 30

    def __init__(self, base: HarnessConfig, *, limit: int = 1, claim_ttl: int = 4 * 3600,
                 launcher: Any = None, records: Path | None = None, profiles: ProfileStore | None = None,
                 servers: ModelServers | None = None, mcp_servers: McpServerStore | None = None,
                 settings: TaskSettings | None = None, runners: RunnerStore | None = None) -> None:
        self.base, self.limit, self.claim_ttl = base, max(1, limit), claim_ttl
        self.profiles = profiles  # set in Settings (profiles.py); None: the configuration's alone
        self.mcp_servers = mcp_servers  # saved in Settings (mcp_servers.py); None: the configuration's alone
        self.settings = settings  # set in Settings (task_settings.py); None: the configuration's alone
        self.runners = runners  # saved in Settings (runners.py); None: the configuration's alone
        self.launcher = launcher or Subprocess()  # launch.Subprocess or launch.Jobs
        # The records, the orchestrator's alone; the bursts' output, where the bursts write it.
        self.outputs = base.resolve_state_dir("tasks")
        self.root = records or self.outputs
        self.lock = threading.Lock()
        # slug -> burst, for bursts this orchestrator started (or followed again) and that have not ended.
        self.running: dict[str, Launched] = {}
        # Marks the records of bursts this orchestrator started, so one left "running" by another is known.
        self.boot = secrets.token_hex(6)
        self.watchers: list[Callable[[str, dict[str, Any]], None]] = []
        self.servers = servers or ModelServers()

    def watch(self, watcher: Callable[[str, dict[str, Any]], None]) -> None:
        """Call watcher("started" or "ended", record) for each burst. It must not block: it may be called
        with the lock held."""
        self.watchers.append(watcher)

    def _tell(self, event: str, record: dict[str, Any]) -> None:
        for watcher in self.watchers:
            try:
                watcher(event, dict(record))
            except Exception as err:  # a watcher's trouble is not the task's
                print(f"tasks: telling {watcher} about {record.get('slug')}: {err}")

    # --- records ---

    def _dir(self, slug: str) -> Path:
        if not SLUG.fullmatch(slug):
            raise TasksError(f"No task named {slug!r}.")
        return self.root / slug

    def _output(self, slug: str) -> Path:
        directory = self.outputs / self._dir(slug).name
        directory.mkdir(parents=True, exist_ok=True)
        return directory / OUTPUT

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
        return [_summary(r) | {"wait": self._waiting_for(r)} for r in self._records()]

    def _waiting_for(self, record: dict[str, Any]) -> str:
        """What a queued task is waiting for, or "": a model server (server_wait), or a slot while `limit` tasks
        run, then the tasks queued before it."""
        if record.get("state") != "queued":
            return ""
        if record.get("server_wait"):
            return f"its model server: {record['server_wait']}"
        running = sorted(self.running)
        if len(running) >= self.limit:
            return (f"a task slot: {self.limit} at a time (ORCHESTRATOR_TASK_LIMIT), and "
                    f"{', '.join(running)} {'is' if len(running) == 1 else 'are'} running")
        return ""

    def in_thread(self, channel: str, thread: str) -> str | None:
        """The task started from this Slack thread, or told in it, if any."""
        where = {"channel": channel, "thread": thread}
        for record in self._records():
            if record.get("slack") == where or where in (record.get("dm") or []):
                return str(record["slug"])
        return None

    def task(self, slug: str) -> dict[str, Any]:
        self.check_pr(slug, PR_FRESH)
        record = self._read(slug)
        output = self._output(slug)
        return {**record, "wait": self._waiting_for(record), "output": _tail(output) if output.is_file() else None}

    def create(self, raw: dict[str, Any], *, user: str = "", slack: dict[str, str] | None = None) -> dict[str, Any]:
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
                base_branch = git.default_branch(url, self.base)
            except git.GitError as err:
                raise TasksError(f"{err} (the orchestrator reads it with the token of the repo's forge, if it "
                                 "has one)") from err
        provider, base_url, model = (str(raw.get(key) or "").strip() for key in ("provider", "base_url", "model"))
        try:
            context_window = int(raw.get("context_window") or 0)
            if context_window < 0:
                raise ValueError
            # Claude Code uses Anthropic, unless the provider is a plugin that gives it an endpoint of its own.
            if engine == "claude-code" and models.provider_attr(provider or self.base.provider,
                                                                 "target_endpoint") is None:
                if provider not in ("", "anthropic"):
                    raise SpecError("Claude Code always uses Anthropic (the orchestrator's Claude token or key): "
                                    "leave the provider as it is, or use the Strands engine.")
                provider = "anthropic"  # and not the orchestrator's other provider's URL or model
            check_model(provider, base_url, model, task_providers())
            settings = model_settings(raw)
            chosen = model_choice(self.base, provider, base_url, model, context_window, settings)
        except ValueError as err:  # SpecError is one
            raise TasksError(str(err) if isinstance(err, SpecError) else "The context window is a number of tokens.") from err
        delegate_to = self._delegates(raw)
        mcp_chosen = self._mcp(raw, engine)
        egress_mode = str(raw.get("egress") or "").strip()
        if egress_mode not in ("", *EGRESS_MODES):
            raise TasksError("egress is allowlist, or log: everything let through, and recorded.")
        allow = _hosts(raw.get("allow"))
        on = self._choose_runner(raw.get("runner"), question=url is None, network=bool(egress_mode or allow))
        record = {
            "slug": slug, "repo": url, "prompt": prompt, "engine": engine, "model": model,
            "provider": provider, "base_url": base_url, "context_window": context_window, "settings": settings,
            # What that comes to, the orchestrator's defaults filled in: for showing which model works on it.
            "uses": {"provider": chosen.get("provider", self.base.provider),
                     "model": models.model_name(replace(self.base, **chosen)),
                     "base_url": chosen.get("base_url", self.base.base_url)},
            "delegate_to": delegate_to, "mcp": mcp_chosen,
            # Its network: the orchestrator's unless it chose log, and hosts added to the allowlist for it alone.
            "egress": egress_mode or None, "allow": allow, "runner": on or None,
            "base_branch": base_branch, "draft": bool(raw.get("draft")) and url is not None,
            "auto_continue": bool(raw.get("auto_continue")) and url is not None,
            # Each PR reviewed, and the reviewer's findings fixed by itself: None, or the reviewer and max_cycles.
            "auto_review": self._auto_review(raw.get("auto_review")) if url is not None else None,
            # Each burst carries on the last one's conversation, rather than starting from the repo (the task
            # document and git): the harness's resume_from = "snapshot". Off unless chosen.
            "keep_conversation": bool(raw.get("keep_conversation")) and url is not None,
            # The setup round (harness --setup): its first round turns a foundation document, or the prompt, into the
            # project's plan, ending in a PR; merging it approves the plan, and later rounds build it.
            "setup": bool(raw.get("setup")) and url is not None,
            "branch": self.base.branch_prefix + slug if url else None, "created": _now(), "created_by": user,
            "state": "queued", "queued": time.time_ns(), "pr": None, "message": None, "restarts": 0, "bursts": [],
            "sandbox": None, "said": None, "slack": slack,
            # Told in Slack when a burst ends: a task from Slack is told in its own thread anyway.
            "notify": bool(raw.get("notify")) and slack is None, "dm": [],
        }
        with self.lock:
            if (self._dir(slug) / TASK_FILE).exists():
                raise TasksError(f"There is already a task named {slug}: continue it, or pick another name.")
            self._write(record)
        self.dispatch()
        return _summary(self._read(slug))

    def clone_spec(self, slug: str) -> dict[str, Any]:
        """What a task was created with, as create() takes it: for a new task like it, its prompt and settings
        changed as wanted. The clone starts from the repo alone (its document, or its project's record and git):
        nothing of the old task's bursts, sandbox, branch or PR comes with it."""
        record = self._read(slug)
        spec: dict[str, Any] = {
            key: record.get(key) for key in (
                "repo", "prompt", "engine", "provider", "base_url", "model", "delegate_to", "mcp", "egress", "allow",
                "runner", "base_branch")}
        spec["context_window"] = record.get("context_window") or 0
        spec.update(record.get("settings") or {})  # effort, thinking, max_tokens, model_params
        for key in ("draft", "auto_continue", "keep_conversation", "setup", "notify"):
            spec[key] = bool(record.get(key))
        spec["auto_review"] = record.get("auto_review")
        spec["from"] = slug
        return spec

    def clone(self, slug: str, changes: dict[str, Any], *, user: str = "") -> dict[str, Any]:
        """A new task like `slug`, with `changes` over its spec (a new prompt, model, settings, name...)."""
        spec = {key: value for key, value in self.clone_spec(slug).items() if key != "from"}
        return self.create({**spec, **{key: value for key, value in changes.items() if key != "from"}}, user=user)

    def _runner(self, name: str) -> runner.Runner:
        try:
            return self.runners.find(name) if self.runners is not None else runner.find(self.base, name)
        except (RunnersError, runner.RunnerError) as err:
            raise TasksError(str(err)) from err

    def _runner_entries(self) -> tuple[str, ...]:
        return self.runners.entries() if self.runners is not None else self.base.runners

    def _choose_runner(self, value: Any, *, question: bool, network: bool) -> str:
        """Where a new task runs: a runner's name, or "" for the orchestrator's own target. None asks for
        Settings' default runner, which a task that cannot go to it (a plain host) passes by."""
        if value is None:
            name = self.runners.default() if self.runners is not None else ""
            if name and self._runner(name).kind == "ssh" and question:
                return ""
        else:
            name = str(value).strip()
        if not name:
            return ""
        if self._runner(name).kind == "ssh":
            if question:
                raise TasksError(f"Runner {name} is a plain host, and a question runs through a gateway that only "
                                 "reads: ask it on a podman or docker runner, or where the orchestrator runs.")
            if network:
                raise TasksError(f"Runner {name} is a plain host, with no gateway to record its network or add "
                                 "hosts to an allowlist: its network is its own. Pick another runner for that.")
        return name

    def _delegates(self, raw: dict[str, Any]) -> list[str] | None:
        """Who the task's lead may hand work to: the names given, or Settings' default; kept with the task so
        continuing it hands work to the same profiles. None without Settings' profiles: the configuration's."""
        if self.profiles is None:
            if raw.get("delegate_to") is not None:
                raise TasksError("This orchestrator has no Settings, so a task cannot name its delegates.")
            return None
        try:
            names = names_given(raw["delegate_to"]) if raw.get("delegate_to") is not None else self.profiles.default()
            check(self.base, self.profiles.profiles(), names)
        except ProfilesError as err:
            raise TasksError(str(err)) from err
        return list(names)

    def _mcp(self, raw: dict[str, Any], engine: str) -> list[str] | None:
        """The task's MCP servers, as NAME=URL: those named or given, or Settings' defaults; kept with the task
        so continuing it uses the same ones. None without Settings: the configuration's."""
        if self.mcp_servers is None:
            if raw.get("mcp") is not None:
                raise TasksError("This orchestrator has no Settings, so a task cannot choose its MCP servers.")
            return None
        try:
            return list(self.mcp_servers.choose(raw.get("mcp"), engine=engine))
        except McpServersError as err:
            raise TasksError(str(err)) from err

    def review(self, slug: str, raw: dict[str, Any], *, user: str = "") -> dict[str, Any]:
        """Queue a review of the task's open PR (harness review): by Claude Code, with `model` or its own default,
        or by `profile`. A fresh context, read-only on the PR's branch; its findings are a comment on the PR, which
        the task's next burst reads. The task is as it was afterwards."""
        spec = self._reviewer(raw)
        self.check_pr(slug, 0, auto=False)  # merged or closed since it was last looked at?
        with self.lock:
            record = self._read(slug)
            if not record.get("repo") or not record.get("pr"):
                raise TasksError(f"{slug} has no PR to review.")
            if record["state"] not in IDLE or record["state"] == "merged" or record.get("pr_state") in ("merged",
                                                                                                         "closed"):
                raise TasksError(f"{slug}'s PR is not open to review"
                                 + (f": the task is {record['state']}." if record["state"] not in IDLE else "."))
            record.update(review=spec, after=record["state"],
                          state="queued", queued=time.time_ns(), message=None, restarts=0, by_merge=False,
                          review_by=user)
            self._write(record)
        self.dispatch()
        return _summary(self._read(slug))

    def _reviewer(self, raw: dict[str, Any]) -> dict[str, str]:
        """Who reviews: {"profile": NAME}, or {"model": ID} (Claude Code; "" for its default)."""
        profile = str(raw.get("profile") or "").strip()
        model = str(raw.get("model") or "").strip()
        if profile and model:
            raise TasksError("Give a profile or a model, not both.")
        if model and (len(model) > MODEL_CHARS or any(c.isspace() for c in model)):
            raise TasksError("model is a model's id, such as claude-sonnet-5-5.")
        if profile and profile not in (self.profiles.profiles() if self.profiles is not None else self.base.agents):
            raise TasksError(f"No profile named {profile}.")
        return {"profile": profile} if profile else {"model": model}

    def _auto_review(self, raw: Any) -> dict[str, Any] | None:
        """A task's review loop as asked for: None (off), or its reviewer and max_cycles."""
        if not raw:
            return None
        if not isinstance(raw, dict):
            raise TasksError("auto_review is {model or profile, max_cycles}, or nothing.")
        try:
            given = raw.get("max_cycles")
            cycles = AUTO_REVIEW_CYCLES if given in (None, "") else int(given)
        except (TypeError, ValueError) as err:
            raise TasksError("max_cycles is how many times the reviewer's findings may be fixed: 1 to 10.") from err
        if not 1 <= cycles <= 10:
            raise TasksError("max_cycles is how many times the reviewer's findings may be fixed: 1 to 10.")
        return {**self._reviewer(raw), "max_cycles": cycles}

    def auto_review(self, slug: str, raw: dict[str, Any]) -> dict[str, Any]:
        """Turn the review loop on (`on`, with a reviewer and max_cycles) or off. Turned on for an idle task with
        an open PR, it reviews that PR now."""
        loop = self._auto_review(raw) if raw.get("on") else None
        with self.lock:
            record = self._read(slug)
            if not record.get("repo"):
                raise TasksError(f"{slug} is a question: it has no PR to review.")
            record["auto_review"] = loop
            record["review_status"] = {**(record.get("review_status") or {}), "cycles": 0, "stopped": None} \
                if loop else None
            self._write(record)
            now = bool(loop and record["state"] == "pr" and record.get("pr") and record.get("pr_state") != "merged")
        if now:
            return self.review(slug, loop, user=AUTO_REVIEW)
        return _summary(record)

    def _after_review(self, record: dict[str, Any], state: str, said: dict[str, Any] | None) -> bool:
        """The review loop's next step, once a review ends: True to carry on fixing what it asked for; otherwise
        it waits for a person, review_status saying why (or that the PR is approved)."""
        loop = record.get("auto_review")
        if not loop:
            return False
        status = {"cycles": 0, **(record.get("review_status") or {})}
        said = said or {}
        verdict = said.get("verdict") if state == "reviewed" else None
        status.update(verdict=verdict, stopped=None, approved_as_comment=False)
        if state != "reviewed":
            status["stopped"] = "the review failed"
        elif verdict == "approve":
            status["approved_as_comment"] = not said.get("posted")  # not the forge's reviewer: no approval counts
        elif verdict != "request_changes":
            status["stopped"] = "the review gave no verdict"
        elif status["cycles"] >= loop.get("max_cycles", AUTO_REVIEW_CYCLES):
            status["stopped"] = f"the reviewer still asks for changes after {status['cycles']} rounds of fixes"
        elif status["cycles"] and STILL_BLOCKING.search(str(said.get("review") or "")):
            status["stopped"] = "the reviewer raised a blocking point again: the author and the reviewer disagree"
        else:
            status["cycles"] += 1
        record["review_status"] = status
        return verdict == "request_changes" and not status["stopped"]

    @staticmethod
    def _reviewed(record: dict[str, Any]) -> None:
        """A review's end, however it ended: the task as it was before it."""
        record["state"] = record.pop("after", None) or "pr"
        record.pop("review", None), record.pop("review_by", None)

    def resume(self, slug: str, message: str = "", *, merged: bool = False, model: str = "",
               fixing: bool = False) -> dict[str, Any]:
        """Queue another burst: with the user's reply, or to carry on from where it stopped. Once the task's
        PR is merged, the burst starts the task's next round. `merged`: started by auto_continue, not a person.
        `model`: the task's model from this burst on (its provider and engine stay), "" to keep its own.
        `fixing`: started by the review loop, for what the reviewer asked; a person continuing starts its count
        again."""
        model = model.strip()
        if model:
            try:
                check_model("", "", model)
            except SpecError as err:
                raise TasksError(str(err)) from err
        self.check_pr(slug, 0, auto=False)  # merged since it was last looked at?
        with self.lock:
            record = self._read(slug)
            if record["state"] not in IDLE:
                raise TasksError(f"{slug} is {record['state']}: wait for it to stop first.")
            if record["state"] == "answered" and not message.strip():
                raise TasksError(f"{slug} is answered: say what to ask next.")
            kept = record.get("sandbox") if record["state"] == "merged" else None
            if record["state"] == "merged":
                self._next_round(record)
            if model and model != record.get("model"):
                record.update(model=model, switched_model=model)
                record.pop("uses", None)  # a profile a router chose: the model chosen now is what works on it
            if not fixing and record.get("auto_review"):
                record["review_status"] = {**(record.get("review_status") or {}), "cycles": 0, "stopped": None}
            record.update(state="queued", queued=time.time_ns(), message=message.strip() or None, restarts=0,
                          by_merge=merged)
            self._write(record)
        if kept:
            self._release(slug, kept)  # on the merged branch: of no use to the next round
        self.dispatch()
        return _summary(self._read(slug))

    # --- its network: what the gateway refused it, and letting that through ---

    def _sandbox(self, record: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        """The last burst's sandbox (_sandbox_line), and whether that burst is still running."""
        burst = (record.get("bursts") or [None])[-1]
        if not burst:
            return None, False
        return _sandbox_line(self._output(record["slug"]), burst), record.get("state") == "running" \
            and not burst.get("ended")

    def refusals(self, slug: str) -> dict[str, Any]:
        """What the gateway refused the task's last burst and nothing has let through since: from the gateway
        while the burst runs, from the events it kept once the burst ended."""
        record = self._read(slug)
        found, running = self._sandbox(record)
        answer: dict[str, Any] = {"egress": record.get("egress") or self.base.egress, "allow": record.get("allow") or [],
                                  "running": running, "sandbox": (found or {}).get("claim"), "refusals": []}
        if found is None:
            return answer
        events: list[dict[str, Any]] = []
        approvals: list[dict[str, Any]] = []
        gateway = Gateway.from_config(self.base) if running else None
        if gateway is not None:
            try:
                events = gateway.events(found["claim"]).get("events") or []
                approvals = gateway.approvals(found["claim"]).get("approvals") or []
            except GatewayError as err:
                print(f"tasks: {slug}: the gateway's refusals: {err}", flush=True)  # the detail, for the log
                answer["error"] = "The gateway could not be asked what it refused (the orchestrator's log says why)."
        elif found.get("events"):
            try:
                with open(found["events"]) as handle:
                    events = [json.loads(line) for line in handle if line.strip()]
            except (OSError, ValueError):
                events = []  # not kept, or not readable from here
        allowed = set(answer["allow"])
        answer["refusals"] = [{"destination": event["destination"], "times": event["times"],
                               "last": event.get("ts") or "", "id": event.get("id") or ""}
                              for event in egress.unapproved(events, approvals) if event["destination"] not in allowed]
        return answer

    def allow(self, slug: str, host: str, scope: str, *, everywhere_ok: bool = True) -> dict[str, str]:
        """Let `host` through for the task: `now`, for its running burst; `task`, for every burst of it from now
        on (and the running one); `everywhere`, for every task and job from now on (and the running burst)."""
        if scope not in ALLOW_SCOPES:
            raise TasksError(f"scope is one of {', '.join(ALLOW_SCOPES)}.")
        if scope == "everywhere" and not everywhere_ok:
            raise TasksError("Allowing a host for every task is for the page, not an API token.")
        (entry,) = _hosts([host]) or [""]
        if not entry:
            raise TasksError("Say which host to allow.")
        said = []
        with self.lock:
            record = self._read(slug)
            if scope == "task" and entry not in (record.get("allow") or []):
                record["allow"] = [*(record.get("allow") or []), entry]
                self._write(record)
                said.append(f"{slug}'s bursts reach {entry} from now on.")
        if scope == "everywhere":
            said.append(egress.allow(self.base, entry).replace(
                " (gateways already running are not changed; approve a refusal for those)", ""))
        found, running = self._sandbox(record)
        gateway = Gateway.from_config(self.base) if running and found else None
        if gateway is not None:
            try:
                gateway.approve(found["claim"], {"allow": entry})
                said.append(f"The running burst reaches {entry} now.")
            except GatewayError as err:
                said.append(f"The running burst could not be told: {err}")
        elif scope == "now":
            raise TasksError(f"No burst of {slug} is running on a cluster's gateway: allow it for the task instead.")
        return {"message": " ".join(said)}

    @staticmethod
    def _next_round(record: dict[str, Any]) -> None:
        """Put the merged round with the earlier ones and give the task a fresh branch and no PR."""
        rounds = [*record.get("rounds", []), {"branch": record["branch"], "pr": record.get("pr"),
                                              "pr_state": record.get("pr_state")}]
        record.update(rounds=rounds, branch=f"{rounds[0]['branch']}-{len(rounds) + 1}", pr=None, pr_state=None,
                      pr_checked=None, sandbox=None, review_status=None)

    def auto_continue(self, slug: str, on: bool) -> dict[str, Any]:
        """Turn carrying on by itself on or off. Turned on for a task whose PR is already merged, it starts
        the next round now, as the look that found it merged would have."""
        with self.lock:
            record = self._read(slug)
            if not record.get("repo"):
                raise TasksError(f"{slug} is a question: it has no PR to be merged.")
            record["auto_continue"] = bool(on)
            self._write(record)
        if on and record["state"] == "merged":
            return self.resume(slug)
        return _summary(record)

    def notify(self, slug: str, on: bool) -> dict[str, Any]:
        """Turn telling the listed Slack users of the task's bursts ending on or off."""
        with self.lock:
            record = self._read(slug)
            if record.get("slack"):
                raise TasksError(f"{slug} was started from Slack: it is told in its thread there.")
            record["notify"] = bool(on)
            self._write(record)
        return _summary(record)

    def told_in(self, slug: str, threads: list[dict[str, str]]) -> None:
        """Keep the Slack direct messages a task was told in, whose threads its later bursts are told in."""
        with self.lock:
            record = self._read(slug)
            record["dm"] = [*(record.get("dm") or []), *threads]
            self._write(record)

    def check_pr(self, slug: str, fresh: int, *, auto: bool = True) -> None:
        """Look at the task's PR on GitHub, unless it was looked at in the last `fresh` seconds, and note
        its state; a merged PR makes an idle task `merged`, and, with `auto` and the task's auto_continue,
        starts its next round if the round ended in that PR. Trouble asking is printed, not raised."""
        try:
            record = self._read(slug)
        except TasksError:
            return
        pr = record.get("pr")
        if (not pr or not record.get("repo") or record.get("state") not in IDLE or record["state"] == "merged"
                or time.time() - float(record.get("pr_checked") or 0) < fresh):
            return
        try:
            found: str | None = git.pr_state(pr, self.base)
        except git.GitError as err:
            print(f"tasks: {slug}: {err}", flush=True)
            found = None
        carry_on = False
        with self.lock:
            try:
                record = self._read(slug)
            except TasksError:
                return
            if record.get("pr") != pr:
                return  # a new round, or a burst, since
            record["pr_checked"] = time.time()
            if found:
                record["pr_state"] = found
                if found == "merged" and record.get("state") in IDLE:
                    carry_on = auto and bool(record.get("auto_continue")) and record["state"] == "pr"
                    if carry_on and _roadmap_changed(record):
                        carry_on = False
                        print(f"tasks: {slug}: its PR is merged, but it changed the project's roadmap: the next "
                              "round waits for a person to continue it.", flush=True)
                    record["state"] = "merged"
            self._write(record)
        if carry_on:
            print(f"tasks: {slug}: its PR is merged; starting its next round (auto_continue).", flush=True)
            try:
                self.resume(slug, merged=True)
            except TasksError as err:  # removed or continued meanwhile
                print(f"tasks: {slug}: {err}", flush=True)

    def by_pr(self, url: str) -> list[str]:
        """The tasks whose PR (this round's) is at that URL: for a forge's webhook (hooks.py)."""
        want = url.strip().lower().rstrip("/")
        return [str(r["slug"]) for r in self._records() if str(r.get("pr") or "").lower().rstrip("/") == want]

    def check_prs(self) -> None:
        """Look at every idle task's PR that was not looked at in the last PR_CHECK seconds."""
        for record in self._records():
            if record.get("pr") and record.get("state") in IDLE and record.get("state") != "merged":
                self.check_pr(str(record["slug"]), PR_CHECK)

    def cancel(self, slug: str) -> dict[str, Any]:
        with self.lock:
            record = self._read(slug)
            if record["state"] == "queued":
                if record.get("review"):
                    self._reviewed(record)
                else:
                    record["state"] = "cancelled"
                self._write(record)
                return _summary(record)
            launched = self.running.get(slug)
            if record["state"] != "running" or launched is None:
                raise TasksError(f"{slug} is not running or queued.")
            record["cancelling"] = True
            self._write(record)
        # The harness gets a SIGINT, as for Ctrl-C, and releases its sandbox (or keeps one with work not pushed).
        self.launcher.stop(launched)
        return _summary(record)

    def handoff(self, token: str) -> dict[str, str] | None:
        """The credentials kept for a burst's pod under this token, once (launch.Handoffs); None if there are none."""
        handoffs = getattr(self.launcher, "handoffs", None)
        return handoffs.take(token) if handoffs is not None and token else None

    def remove(self, slug: str) -> None:
        with self.lock:
            record = self._read(slug)
            if record["state"] not in IDLE:
                raise TasksError(f"{slug} is {record['state']}: cancel it first.")
            if record.get("sandbox"):
                self._release(slug, record["sandbox"])
            for directory in dict.fromkeys((self._dir(slug), self._output(slug).parent)):
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
        """On start: a burst another orchestrator left running is followed again if it is still there (a Job),
        else queued again, or failed after RESTARTS."""
        done, followed = [], []
        with self.lock:
            for record in self._records():
                if record.get("state") != "running" or record.get("boot") == self.boot:
                    continue
                if (launched := self.launcher.reattach(record)) is not None:
                    record["boot"] = self.boot
                    self._write(record)
                    self.running[record["slug"]] = launched
                    self.servers.hold(f"task {record['slug']}", servers_for(self._config(record)))
                    followed.append((record["slug"], launched, bool(record.get("cancelling"))))
                    done.append(f"{record['slug']}: its burst is still there ({launched.job}), followed again")
                    continue
                if record.get("bursts"):
                    record["bursts"][-1].update(ended=_now(), state="interrupted")
                if record.get("review") and (record.get("cancelling") or record.get("restarts", 0) >= RESTARTS):
                    record["cancelling"] = False
                    self._reviewed(record)
                    done.append(f"{record['slug']}: its review was cut short by a restart")
                elif record.get("cancelling"):
                    record.update(state="cancelled", cancelling=False)
                elif record.get("restarts", 0) < RESTARTS:
                    record.update(state="queued", restarts=record.get("restarts", 0) + 1)
                    done.append(f"{record['slug']}: cut short by a restart, queued again")
                else:
                    record["state"] = "interrupted"
                    done.append(f"{record['slug']}: cut short by a restart {RESTARTS + 1} times, left interrupted")
                self._write(record)
        for slug, launched, cancelling in followed:
            if cancelling:  # stopped before the restart: make sure of it
                self.launcher.stop(launched)
            threading.Thread(target=self._wait, args=(slug, launched), daemon=True).start()
        return done

    def dispatch(self) -> None:
        """Start queued tasks, oldest first, while fewer than `limit` are running. One whose model server is
        taken or busy stays queued, and the next is looked at."""
        with self.lock:
            queued = sorted((r for r in self._records() if r.get("state") == "queued"), key=lambda r: r.get("queued", 0))
            for record in queued:
                if len(self.running) >= self.limit:
                    break
                why = self.servers.take(f"task {record['slug']}", models_for(self._config(record)))
                if why:
                    if record.get("server_wait") != why:
                        record["server_wait"] = why
                        self._write(record)
                    continue
                record.pop("server_wait", None)
                self._start(record)

    def start(self) -> None:
        """Look at the queue every RETRY seconds, in a thread of its own, for tasks waiting on a model server
        that something other than this orchestrator keeps busy; and at idle tasks' PRs (check_prs)."""
        def loop() -> None:
            while True:
                time.sleep(self.RETRY)
                try:
                    if any(r.get("server_wait") and r.get("state") == "queued" for r in self._records()):
                        self.dispatch()
                    self.check_prs()
                except Exception as err:  # noqa: BLE001 -- the next look tries again
                    print(f"tasks: {type(err).__name__}: {err}", flush=True)

        threading.Thread(target=loop, name="task-queue", daemon=True).start()

    def _argv(self, record: dict[str, Any]) -> list[str]:
        review = record.get("review")
        engine = ("strands" if review.get("profile") else "claude-code") if review else record["engine"]
        argv = [sys.executable, "-m", "harness", "--engine", engine, "--claim-ttl", str(self.claim_ttl)]
        if engine == "claude-code":
            # Nobody is there to approve a command, and a task is always in a sandbox: it may run anything
            # (the git rules still hold, as a hook).
            argv += ["--permission-mode", "bypassPermissions"]
        if self.base.target in ("local", "podman", "docker"):
            # Never on the orchestrator's own machine: in a container, through a gateway.
            runtime = "docker" if self.base.target == "docker" else "podman"
            argv += ["--target", runtime] + (["--egress", "allowlist"] if self.base.egress == "open" else [])
        elif not record.get("repo") and self.base.egress == "open":
            argv += ["--egress", "allowlist"]  # a question runs as a job does: through a gateway
        if record.get("runner"):  # the target is the runner's (harness/runner.resolve)
            try:
                kind = self._runner(record["runner"]).kind
            except TasksError:
                kind = ""  # removed from Settings since: the burst says so
            argv += ["--runner", record["runner"]]
            if kind == "ssh":
                argv += ["--egress", "open"]  # a plain host has no gateway (the cluster's is for its sandboxes)
            elif self.base.egress == "open":
                argv += ["--egress", "allowlist"]  # a container: through its gateway, as on this machine
        if record.get("egress") == "log":
            argv += ["--egress", "log"]  # chosen for the task: after the above, so it wins
        if record.get("allow"):
            argv += ["--egress-add", ",".join(record["allow"])]
        if not record.get("repo"):
            return argv + ["ask", record["slug"], "--task", record["prompt"]] + (
                ["--message", record["message"]] if record.get("message") else [])
        argv += ["--repo-url", record["repo"], "--draft-pr" if record.get("draft") else "--no-draft-pr"]
        if record.get("base_branch"):
            argv += ["--base-branch", record["base_branch"]]
        if review:  # in a fresh sandbox, not the one the task's last burst kept
            return argv + [*(["--model", review["model"]] if review.get("model") else []),
                           "review", record["slug"], "--pr", record["pr"],
                           *(["--branch", record["branch"]] if record.get("branch") else []),
                           *(["--by", review["profile"]] if review.get("profile") else [])]
        if record.get("sandbox"):
            argv += shlex.split(record["sandbox"])  # the sandbox the last burst kept, after --target to win
        argv += ["run", record["slug"]]
        if record.get("setup") and not record.get("rounds"):
            argv += ["--task", record["prompt"], "--setup"]  # until the plan's PR is merged: then the rounds that build it
        elif not record.get("setup"):
            argv += ["--task", record["prompt"]]
        # A setup task's prompt was how to write the plan, so once that is merged it is left out: the plan is the
        # task, and each round carries on with its current milestone (or the message, if there is one).
        if record.get("rounds"):
            argv += ["--branch", record["branch"]]  # a later round's own branch, which the harness starts from the base
        if record.get("message"):
            argv += ["--message", record["message"]]
        return argv

    def _config(self, record: dict[str, Any]) -> HarnessConfig:
        """The orchestrator's settings with the task's model on top: what its burst runs with, for what
        it is granted (a provider plugin's credentials go to the tasks that use it)."""
        cfg = replace(self.base, **self._chosen(record))
        if (review := record.get("review")) and not review.get("profile"):
            cfg = replace(cfg, engine="claude-code")  # Claude Code's review: no model server of the task's to wait for
        if record.get("runner"):
            cfg = replace(cfg, runners=self._runner_entries(), runner=record["runner"])  # its SSH key
        if self.profiles is not None:
            cfg = replace(cfg, agents=self.profiles.profiles())
        if record.get("delegate_to") is not None:
            cfg = replace(cfg, delegate_to=tuple(record["delegate_to"]))
        if record.get("mcp") is not None:
            cfg = replace(cfg, mcp=tuple(record["mcp"]))
        if self.settings is not None:  # a review or a fallback set in Settings: the profiles it calls take turns too
            cfg = replace(cfg, **self.settings.values())
        return cfg

    def _chosen(self, record: dict[str, Any]) -> dict[str, Any]:
        if record.get("review"):  # the reviewer's model (--model), not the task's
            return {}
        return model_choice(self.base, record.get("provider") or "", record.get("base_url") or "",
                            record.get("model") or "", record.get("context_window") or 0, record.get("settings"))

    def _model_env(self, record: dict[str, Any]) -> dict[str, str]:
        """The task's provider and model as the burst's HARNESS_* settings, over the orchestrator's."""
        chosen = self._chosen(record)
        if chosen:
            chosen["router"] = ""  # a task given its model or its settings is not routed (harness/agents.py)
        elif not record.get("repo") and (record.get("uses") or {}).get("agent"):
            # A question's follow-ups stay with the profile the router chose for it (a repo's task keeps
            # its own choice, in the harness's state).
            chosen = {"agent": record["uses"]["agent"], "router": ""}
        # Its MCP servers, even none: over the orchestrator's HARNESS_MCP, which the burst would otherwise inherit.
        servers = {"HARNESS_MCP": ",".join(record["mcp"])} if record.get("mcp") is not None else {}
        kept = {"HARNESS_RESUME_FROM": "snapshot"} if record.get("keep_conversation") else {}
        return {f"HARNESS_{name.upper()}": str(value).lower() if isinstance(value, bool) else str(value)
                for name, value in chosen.items()} | self._profile_env(record) | servers \
            | (self.settings.environment() if self.settings is not None else {}) | kept

    def _profile_env(self, record: dict[str, Any]) -> dict[str, str]:
        """The profiles, and who this task's lead may hand work to, as the burst's HARNESS_AGENTS and
        HARNESS_DELEGATE_TO: a burst run as a Job has no config file of the orchestrator's to read them from."""
        return self.profiles.environment(record.get("delegate_to")) if self.profiles is not None else {}

    def _start(self, record: dict[str, Any]) -> None:
        """Start a burst of a queued task. Called with the lock held."""
        with open(self._output(record["slug"]), "ab") as output:
            offset = output.tell()  # where this burst's output starts: its HARNESS_SANDBOX line is after it
            heading = f"\n=== {'review' if record.get('review') else 'burst'} {len(record['bursts']) + 1}, {_now()}"
            output.write((heading + (f": {record['message']}" if record.get("message") else "") + " ===\n").encode())
            output.flush()
        # A question's run is found by its id, which is set here.
        run = {} if record.get("repo") else {job.RUN_ID_ENV: job.new_run_id()}
        try:
            launched = self.launcher.start(
                record["slug"], len(record["bursts"]) + 1, self._argv(record),
                credentials.for_run([], {"PYTHONUNBUFFERED": "1", **self._model_env(record), **run}),
                granted(credentials.for_task(self._config(record))
                        + (credentials.reviewer_tokens(self._config(record)) if record.get("review") else [])),
                self._output(record["slug"]))
        except LaunchError as err:
            with open(self._output(record["slug"]), "ab") as output:
                output.write(f"! could not start the burst: {err}\n".encode())
            record["bursts"].append({"started": _now(), "ended": _now(), "message": record.get("message"),
                                     "state": "failed"})
            record.update(state="failed", message=None)
            self._write(record)
            self._tell("ended", {**record, "exit": None})
            self.servers.give_back(f"task {record['slug']}", wake=False)  # the lock is held: dispatch goes on
            return
        record["bursts"].append({"started": _now(), "message": record.get("message"), "offset": offset,
                                 **({"by_merge": True} if record.get("by_merge") else {}),
                                 **({"review": {**record["review"], "by": record.get("review_by") or ""}}
                                    if record.get("review") else {}),
                                 **({"model": record.pop("switched_model")}
                                    if record.get("switched_model") and not record.get("review") else {}),
                                 **({"run": run[job.RUN_ID_ENV]} if run else {"branch": record.get("branch")})})
        record.update(state="running", boot=self.boot, **launched.record())
        self._write(record)
        self.running[record["slug"]] = launched
        self._tell("started", record)
        threading.Thread(target=self._wait, args=(record["slug"], launched), daemon=True).start()

    def _wait(self, slug: str, launched: Launched) -> None:
        code = self.launcher.wait(launched)
        self.servers.give_back(f"task {slug}")
        try:
            if not self._read(slug).get("repo"):
                self._answered(slug, code)
                return
        except TasksError:
            pass  # removed meanwhile
        result = _result(self._output(slug))
        done_with, fix, review = None, False, None
        with self.lock:
            self.running.pop(slug, None)
            try:
                record = self._read(slug)
            except TasksError:
                record = None  # removed meanwhile
            if record is not None and record.get("review"):
                said = result.get("said") if isinstance(result.get("said"), dict) else None
                record["bursts"][-1].update(
                    ended=_now(), exit=code, said=said, metrics=result.get("metrics"), config=result.get("config"),
                    delegated=result.get("delegated"),
                    state="cancelled" if record.get("cancelling") else
                    "reviewed" if result.get("state") == "reviewed" else "failed")
                fix = record["bursts"][-1]["state"] != "cancelled" and \
                    self._after_review(record, record["bursts"][-1]["state"], said)
                self._reviewed(record)
                record.update(cancelling=False)
                record.pop("pid", None), record.pop("job", None)
                self._write(record)
                self._tell("reviewed", {**record, "exit": code})
                record = None  # told: not as a burst of the task's
            elif record is not None:
                state = "cancelled" if record.get("cancelling") else _state(code, result)
                record["bursts"][-1].update(ended=_now(), exit=code, state=state, delegated=result.get("delegated"),
                                            metrics=result.get("metrics"), config=result.get("config"),
                                            fallback=result.get("fallback"), project=result.get("project"),
                                            egress=_sandbox_line(self._output(slug), record["bursts"][-1]))
                if isinstance(result.get("project"), dict):  # a project's milestone, its ticks, a plan change
                    criteria = (result.get("metrics") or {}).get("criteria")
                    record["project"] = {**result["project"], "criteria": criteria}
                if result.get("agent"):  # routed to a profile (harness/agents.py): that is what works on it
                    provider, _, model = str(result.get("model") or "").partition(" ")
                    record["uses"] = {"provider": provider, "model": model, "base_url": result.get("base_url") or "",
                                      "agent": result["agent"]}
                if result.get("pr") and result["pr"] != record.get("pr"):
                    record.update(pr_state="open", pr_checked=time.time())  # just opened
                record.update(state=state, pr=result.get("pr") or record.get("pr"), message=None, cancelling=False,
                              said=result.get("said") if isinstance(result.get("said"), dict) else None)
                record.pop("pid", None), record.pop("job", None)
                kept = str(result.get("target") or "")
                if result.get("kept") and self._keepable(kept):
                    record["sandbox"] = kept
                elif record.get("sandbox") and state in FINISHED:
                    # Attached to an earlier burst's sandbox, which the harness leaves alone: release it here.
                    done_with, record["sandbox"] = record["sandbox"], None
                if state == "pr" and record.get("auto_review") and not record.get("cancelling"):
                    review = record["auto_review"]
                self._write(record)
        if done_with:
            self._release(slug, done_with)
        if record is not None:
            self._tell("ended", {**record, "exit": code})
        try:  # the review loop's next step
            if fix:
                print(f"tasks: {slug}: the reviewer asked for changes; carrying on (auto_review).", flush=True)
                self.resume(slug, fixing=True)
            elif review:
                print(f"tasks: {slug}: its PR is up; reviewing it (auto_review).", flush=True)
                self.review(slug, review, user=AUTO_REVIEW)
        except TasksError as err:  # merged, removed or continued meanwhile
            print(f"tasks: {slug}: {err}", flush=True)
        self.dispatch()

    def _answered(self, slug: str, code: int) -> None:
        """A question's burst ended: keep its answer, if it gave one."""
        result = _result(self._output(slug), JOB_RESULT)
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
                             delegated=result.get("delegated"), metrics=result.get("metrics"),
                             config=result.get("config"))
                if result.get("agent"):
                    provider, _, model = str(result.get("model") or "").partition(" ")
                    record["uses"] = {**record.get("uses", {}), "provider": provider, "model": model,
                                      "agent": result["agent"]}
                record.update(state=state, message=None, cancelling=False)
                record.pop("pid", None), record.pop("job", None)
                self._write(record)
                self._tell("ended", {**record, "exit": code})
        self.dispatch()

    def _keepable(self, kept: str) -> bool:
        """Whether a burst's kept sandbox is one to attach to and release: one of SANDBOX's shapes, and on a
        plain-host runner a directory the harness made there, under the runner's path (release removes it)."""
        if not SANDBOX.fullmatch(kept):
            return False
        args = shlex.split(kept)
        if "--runner-workdir" not in args:
            return True
        try:
            found = self._runner(args[1])
        except TasksError:
            return False
        parent, _, last = args[3].rstrip("/").rpartition("/")
        if found.kind != "ssh" or ".." in parent.split("/") or not RUN_DIR.fullmatch(last):
            return False
        return parent == found.path if found.path.startswith("/") else parent.endswith("/" + found.path)

    def _release(self, slug: str, sandbox: str) -> None:
        """Remove a kept sandbox (`harness ... release`), noting it in the task's output."""
        args = shlex.split(sandbox)
        on = args[args.index("--runner") + 1] if "--runner" in args else ""
        cfg = replace(self.base, runners=self._runner_entries(), runner=on) if on else self.base
        with open(self._output(slug), "ab") as output:
            output.write(f"\n=== releasing the kept sandbox ({sandbox}) ===\n".encode())
            output.flush()
            subprocess.run([sys.executable, "-m", "harness", *shlex.split(sandbox), "release"], cwd=REPO,
                           env=credentials.for_run(credentials.for_task(cfg), {"PYTHONUNBUFFERED": "1"}),
                           stdin=subprocess.DEVNULL, stdout=output,
                           stderr=subprocess.STDOUT, timeout=300, check=False)


def _state(code: int, result: dict[str, Any]) -> str:
    """What a burst ended in: its HARNESS_RESULT's terminal state, or failed when it has none."""
    state = result.get("state")
    if state in (*FINISHED, "push_failed", "waiting", "max_tokens"):
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


def _hosts(raw: Any) -> list[str]:
    """Allowlist entries as given (a list, or a string of them separated by commas or spaces), checked."""
    entries = re.split(r"[,\s]+", raw) if isinstance(raw, str) else raw or []
    if not isinstance(entries, list):
        raise TasksError("allow is a list of hosts (host, host:port or *.domain).")
    found = []
    for entry in (str(e).strip().lower() for e in entries):
        if not entry:
            continue
        if not egress.HOST.fullmatch(entry):
            raise TasksError(f"{entry!r} is not a host to allow: host, host:port or *.domain.")
        found.append(entry)
    return list(dict.fromkeys(found))


def _sandbox_line(output: Path, burst: dict[str, Any]) -> dict[str, Any] | None:
    """A burst's HARNESS_SANDBOX: which sandbox the gateway knows it by, its mode, and where its events are
    kept once it ends. None before it has one (still starting), or for a burst with no shared gateway."""
    if burst.get("egress"):
        return dict(burst["egress"])
    try:
        with open(output, "rb") as handle:
            handle.seek(int(burst.get("offset") or 0))
            text = handle.read().decode(errors="replace")
    except OSError:
        return None
    found = None
    for line in text.splitlines():
        if line.startswith(SANDBOX_LINE):
            try:
                found = json.loads(line[len(SANDBOX_LINE):])
            except ValueError:
                continue
    return found if isinstance(found, dict) and found.get("claim") else None


def _roadmap_changed(record: dict[str, Any]) -> bool:
    """The round changed a project's roadmap (harness project.plan_change): a person continues it, not auto_continue."""
    return (record.get("project") or {}).get("plan_change") == "roadmap"


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in (
        "slug", "repo", "prompt", "engine", "provider", "base_url", "model", "context_window", "settings", "uses", "delegate_to", "mcp", "base_branch", "draft", "auto_continue", "auto_review", "review_status", "keep_conversation", "setup", "project", "branch", "created", "created_by",
        "state", "pr", "pr_state", "rounds", "message", "restarts", "sandbox", "said", "server_wait", "egress",
        "allow", "runner", "notify")} | {"from_slack": bool(record.get("slack")), "told": bool(record.get("dm")),
        "bursts": len(record.get("bursts", [])), "last_burst": (record.get("bursts") or [None])[-1]}


def _now() -> str:
    return _iso(datetime.now(UTC))
