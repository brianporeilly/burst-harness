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
started from Slack keeps where to answer in its record (`slack`).

A task's PR is looked at on GitHub while the task is idle (when its page is read, at most every
PR_FRESH seconds, and every PR_CHECK seconds in the background): once it is merged, the task is
`merged`, and continuing it starts its next round rather than another burst on the merged branch. A
round is a fresh branch from the base (the first round's branch, with -2, -3... after it) and a PR of
its own; its first burst carries on from the task document, which came to the base with the merge
(harness `run --branch`). The task keeps its earlier rounds' branches and PRs (`rounds`).

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

from harness import ask, credentials, egress, git, job, models
from harness.config import HarnessConfig
from harness.gateway_control import Gateway, GatewayError

from .backend import SpecError, check_model, model_choice, model_settings, task_providers
from .jobs import OUTPUT, _iso, _tail, json_file
from .launch import REPO, Launched, LaunchError, Subprocess, granted
from .mcp_servers import McpServersError, McpServerStore
from .profiles import ProfilesError, ProfileStore, check, names_given
from .task_settings import TaskSettings
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
SANDBOX = re.compile(r"--target (?:podman|docker) --docker-container burst-[a-z0-9-]{1,60}"
                     r"|--target agent-sandbox --claim [a-z0-9][a-z0-9-]{0,62}")
# The burst ended where a kept sandbox is no longer needed: the work is pushed, or given up.
FINISHED = ("pr", "pushed_no_pr", "paused", "abandoned")
TASK_FILE = "task.json"
# How many times a burst cut short by the orchestrator stopping is started again.
RESTARTS = 2
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
                 settings: TaskSettings | None = None) -> None:
        self.base, self.limit, self.claim_ttl = base, max(1, limit), claim_ttl
        self.profiles = profiles  # set in Settings (profiles.py); None: the configuration's alone
        self.mcp_servers = mcp_servers  # saved in Settings (mcp_servers.py); None: the configuration's alone
        self.settings = settings  # set in Settings (task_settings.py); None: the configuration's alone
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
        """The task started from this Slack thread, if any."""
        for record in self._records():
            if record.get("slack") == {"channel": channel, "thread": thread}:
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
        record = {
            "slug": slug, "repo": url, "prompt": prompt, "engine": engine, "model": model,
            "provider": provider, "base_url": base_url, "context_window": context_window, "settings": settings,
            # What that comes to, the orchestrator's defaults filled in: for showing which model works on it.
            "uses": {"provider": chosen.get("provider", self.base.provider),
                     "model": models.model_name(replace(self.base, **chosen)),
                     "base_url": chosen.get("base_url", self.base.base_url)},
            "delegate_to": delegate_to, "mcp": mcp_chosen,
            # Its network: the orchestrator's unless it chose log, and hosts added to the allowlist for it alone.
            "egress": egress_mode or None, "allow": _hosts(raw.get("allow")),
            "base_branch": base_branch, "draft": bool(raw.get("draft")) and url is not None,
            "branch": self.base.branch_prefix + slug if url else None, "created": _now(), "created_by": user,
            "state": "queued", "queued": time.time_ns(), "pr": None, "message": None, "restarts": 0, "bursts": [],
            "sandbox": None, "said": None, "slack": slack,
        }
        with self.lock:
            if (self._dir(slug) / TASK_FILE).exists():
                raise TasksError(f"There is already a task named {slug}: continue it, or pick another name.")
            self._write(record)
        self.dispatch()
        return _summary(self._read(slug))

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

    def resume(self, slug: str, message: str = "") -> dict[str, Any]:
        """Queue another burst: with the user's reply, or to carry on from where it stopped. Once the task's
        PR is merged, the burst starts the task's next round."""
        self.check_pr(slug, 0)  # merged since it was last looked at?
        with self.lock:
            record = self._read(slug)
            if record["state"] not in IDLE:
                raise TasksError(f"{slug} is {record['state']}: wait for it to stop first.")
            if record["state"] == "answered" and not message.strip():
                raise TasksError(f"{slug} is answered: say what to ask next.")
            kept = record.get("sandbox") if record["state"] == "merged" else None
            if record["state"] == "merged":
                self._next_round(record)
            record.update(state="queued", queued=time.time_ns(), message=message.strip() or None, restarts=0)
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
                answer["error"] = str(err)
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
                      pr_checked=None, sandbox=None)

    def check_pr(self, slug: str, fresh: int) -> None:
        """Look at the task's PR on GitHub, unless it was looked at in the last `fresh` seconds, and note
        its state; a merged PR makes an idle task `merged`. Trouble asking is printed, not raised."""
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
                    record["state"] = "merged"
            self._write(record)

    def check_prs(self) -> None:
        """Look at every idle task's PR that was not looked at in the last PR_CHECK seconds."""
        for record in self._records():
            if record.get("pr") and record.get("state") in IDLE and record.get("state") != "merged":
                self.check_pr(str(record["slug"]), PR_CHECK)

    def cancel(self, slug: str) -> dict[str, Any]:
        with self.lock:
            record = self._read(slug)
            if record["state"] == "queued":
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
                if record.get("cancelling"):
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
        argv = [sys.executable, "-m", "harness", "--engine", record["engine"], "--claim-ttl", str(self.claim_ttl)]
        if record["engine"] == "claude-code":
            # Nobody is there to approve a command, and a task is always in a sandbox: it may run anything
            # (the git rules still hold, as a hook).
            argv += ["--permission-mode", "bypassPermissions"]
        if self.base.target in ("local", "podman", "docker"):
            # Never on the orchestrator's own machine: in a container, through a gateway.
            runtime = "docker" if self.base.target == "docker" else "podman"
            argv += ["--target", runtime] + (["--egress", "allowlist"] if self.base.egress == "open" else [])
        elif not record.get("repo") and self.base.egress == "open":
            argv += ["--egress", "allowlist"]  # a question runs as a job does: through a gateway
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
        if record.get("sandbox"):
            argv += shlex.split(record["sandbox"])  # the sandbox the last burst kept, after --target to win
        argv += ["run", record["slug"], "--task", record["prompt"]]
        if record.get("rounds"):
            argv += ["--branch", record["branch"]]  # a later round's own branch, which the harness starts from the base
        if record.get("message"):
            argv += ["--message", record["message"]]
        return argv

    def _config(self, record: dict[str, Any]) -> HarnessConfig:
        """The orchestrator's settings with the task's model on top: what its burst runs with, for what
        it is granted (a provider plugin's credentials go to the tasks that use it)."""
        cfg = replace(self.base, **self._chosen(record))
        if self.profiles is not None:
            cfg = replace(cfg, agents=self.profiles.profiles())
        if record.get("delegate_to") is not None:
            cfg = replace(cfg, delegate_to=tuple(record["delegate_to"]))
        if record.get("mcp") is not None:
            cfg = replace(cfg, mcp=tuple(record["mcp"]))
        return cfg

    def _chosen(self, record: dict[str, Any]) -> dict[str, Any]:
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
        return {f"HARNESS_{name.upper()}": str(value).lower() if isinstance(value, bool) else str(value)
                for name, value in chosen.items()} | self._profile_env(record) | servers \
            | (self.settings.environment() if self.settings is not None else {})

    def _profile_env(self, record: dict[str, Any]) -> dict[str, str]:
        """The profiles, and who this task's lead may hand work to, as the burst's HARNESS_AGENTS and
        HARNESS_DELEGATE_TO: a burst run as a Job has no config file of the orchestrator's to read them from."""
        return self.profiles.environment(record.get("delegate_to")) if self.profiles is not None else {}

    def _start(self, record: dict[str, Any]) -> None:
        """Start a burst of a queued task. Called with the lock held."""
        with open(self._output(record["slug"]), "ab") as output:
            offset = output.tell()  # where this burst's output starts: its HARNESS_SANDBOX line is after it
            heading = f"\n=== burst {len(record['bursts']) + 1}, {_now()}"
            output.write((heading + (f": {record['message']}" if record.get("message") else "") + " ===\n").encode())
            output.flush()
        # A question's run is found by its id, which is set here.
        run = {} if record.get("repo") else {job.RUN_ID_ENV: job.new_run_id()}
        try:
            launched = self.launcher.start(
                record["slug"], len(record["bursts"]) + 1, self._argv(record),
                credentials.for_run([], {"PYTHONUNBUFFERED": "1", **self._model_env(record), **run}),
                granted(credentials.for_task(self._config(record))), self._output(record["slug"]))
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
        done_with = None
        with self.lock:
            self.running.pop(slug, None)
            try:
                record = self._read(slug)
            except TasksError:
                record = None  # removed meanwhile
            if record is not None:
                state = "cancelled" if record.get("cancelling") else _state(code, result)
                record["bursts"][-1].update(ended=_now(), exit=code, state=state, delegated=result.get("delegated"),
                                            egress=_sandbox_line(self._output(slug), record["bursts"][-1]))
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
                if result.get("kept") and SANDBOX.fullmatch(kept):
                    record["sandbox"] = kept
                elif record.get("sandbox") and state in FINISHED:
                    # Attached to an earlier burst's sandbox, which the harness leaves alone: release it here.
                    done_with, record["sandbox"] = record["sandbox"], None
                self._write(record)
        if done_with:
            self._release(slug, done_with)
        if record is not None:
            self._tell("ended", {**record, "exit": code})
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
                             delegated=result.get("delegated"))
                if result.get("agent"):
                    provider, _, model = str(result.get("model") or "").partition(" ")
                    record["uses"] = {**record.get("uses", {}), "provider": provider, "model": model,
                                      "agent": result["agent"]}
                record.update(state=state, message=None, cancelling=False)
                record.pop("pid", None), record.pop("job", None)
                self._write(record)
                self._tell("ended", {**record, "exit": code})
        self.dispatch()

    def _release(self, slug: str, sandbox: str) -> None:
        """Remove a kept sandbox (`harness ... release`), noting it in the task's output."""
        with open(self._output(slug), "ab") as output:
            output.write(f"\n=== releasing the kept sandbox ({sandbox}) ===\n".encode())
            output.flush()
            subprocess.run([sys.executable, "-m", "harness", *shlex.split(sandbox), "release"], cwd=REPO,
                           env=credentials.for_run(credentials.for_task(self.base), {"PYTHONUNBUFFERED": "1"}),
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


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in (
        "slug", "repo", "prompt", "engine", "provider", "base_url", "model", "context_window", "settings", "uses", "delegate_to", "mcp", "base_branch", "draft", "branch", "created", "created_by",
        "state", "pr", "pr_state", "rounds", "message", "restarts", "sandbox", "said", "server_wait", "egress",
        "allow")} | {
        "bursts": len(record.get("bursts", [])), "last_burst": (record.get("bursts") or [None])[-1]}


def _now() -> str:
    return _iso(datetime.now(UTC))
