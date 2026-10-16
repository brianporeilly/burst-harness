"""Where sessions run, behind one interface the web app talks to.

`PodmanBackend` is the only one so far: it drives `harness session` and
`harness egress` on this machine. A Kubernetes backend (agent-sandbox claims
behind a shared gateway per namespace) is meant to slot in beside it, which is
why the app never calls the harness directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from harness import containers, egress, git, session
from harness.config import HarnessConfig

PROVIDERS = ("anthropic", "llamacpp", "openai")
EGRESS = ("allowlist", "log")
INJECT = ("git", "model")
ENTRY = re.compile(r"[A-Za-z0-9*]([A-Za-z0-9*.-]*[A-Za-z0-9*])?(:\d{1,5})?")


class SpecError(ValueError):
    """A request to create a session that cannot be carried out as given."""


@dataclass(frozen=True)
class SessionSpec:
    """Everything the new-session form can choose."""

    name: str
    agent: str = "opencode"
    repo_url: str = ""
    egress: str = "allowlist"
    allow: tuple[str, ...] = ()
    inject: tuple[str, ...] = ()
    root: bool = False
    terminal: bool = False
    idle: str = ""
    lifetime: str = ""
    provider: str = ""
    base_url: str = ""
    model: str = ""
    context_window: int = 0

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "SessionSpec":
        """From the form's JSON, checking everything a user could get wrong."""
        def text(key: str) -> str:
            value = raw.get(key) or ""
            if not isinstance(value, str):
                raise SpecError(f"{key} must be text.")
            return value.strip()

        def entries(key: str, allowed: tuple[str, ...] | None = None) -> tuple[str, ...]:
            value = raw.get(key) or []
            items = re.split(r"[\s,]+", value) if isinstance(value, str) else value
            if not isinstance(items, list):
                raise SpecError(f"{key} must be a list.")
            found = tuple(dict.fromkeys(str(item).strip().lower() for item in items if str(item).strip()))
            for item in found:
                if allowed is not None and item not in allowed:
                    raise SpecError(f"{key} takes {', '.join(allowed)} (got {item!r}).")
                if allowed is None and not ENTRY.fullmatch(item):
                    raise SpecError(f"Not an allowlist entry: {item!r}. Use example.com, *.example.com, or host:port.")
            return found

        spec = cls(
            name=text("name"), agent=text("agent") or "opencode", repo_url=text("repo_url"),
            egress=text("egress") or "allowlist", allow=entries("allow"), inject=entries("inject", INJECT),
            root=bool(raw.get("root")), terminal=bool(raw.get("terminal")), idle=text("idle"), lifetime=text("lifetime"),
            provider=text("provider"), base_url=text("base_url"), model=text("model"),
            context_window=int(raw.get("context_window") or 0),
        )
        try:
            session.container_name(spec.name)
            for duration in (spec.idle, spec.lifetime):
                if duration:
                    session.seconds(duration)
        except session.SessionError as err:
            raise SpecError(str(err)) from err
        if spec.agent not in session.AGENTS:
            raise SpecError(f"agent is one of {', '.join(session.AGENTS)}.")
        if spec.egress not in EGRESS:
            raise SpecError(f"egress is one of {', '.join(EGRESS)}.")
        if spec.provider and spec.provider not in PROVIDERS:
            raise SpecError(f"provider is one of {', '.join(PROVIDERS)}.")
        if spec.repo_url and not spec.repo_url.startswith("https://"):
            raise SpecError("The repo must be an https:// URL: a session reaches the network only through its gateway.")
        if spec.provider in ("llamacpp", "openai") and not spec.base_url:
            raise SpecError(f"provider {spec.provider} needs a base URL.")
        return spec

    def overrides(self) -> dict[str, Any]:
        """The harness configuration this spec stands for, on top of the user's own."""
        chosen: dict[str, Any] = {
            "repo_url": self.repo_url, "egress": self.egress, "egress_inject": self.inject,
            "container_root": self.root, "provider": self.provider or None, "base_url": self.base_url or None,
            "model_id": self.model or None, "context_window_limit": self.context_window or None,
        }
        return {key: value for key, value in chosen.items() if value is not None}


class Backend(Protocol):
    def info(self) -> dict[str, Any]: ...
    def sessions(self) -> list[dict[str, Any]]: ...
    def create(self, spec: SessionSpec) -> None: ...
    def start(self, name: str) -> None: ...
    def stop(self, name: str) -> str: ...
    def remove(self, name: str) -> str: ...
    def pending(self, name: str) -> list[dict[str, Any]]: ...
    def approve(self, request_id: str, scope: str) -> str: ...
    def events(self, name: str) -> AsyncIterator[str]: ...


@dataclass
class Job:
    """A start in progress, or one that failed: what the list shows until the session is up."""

    state: str
    since: float = field(default_factory=time.time)
    error: str = ""


class PodmanBackend:
    """Sessions on this machine's podman, through the harness."""

    def __init__(self, base: HarnessConfig | None = None) -> None:
        self.base = base or HarnessConfig.load(None, {})
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        # Starting pulls images, clones and waits for UIs: never on the request's thread.
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="session-start")

    def spec_path(self, name: str) -> Path:
        """What a session was created with, so starting it again means the same thing."""
        return Path(self.base.state_dir).expanduser() / "orchestrator" / f"{name}.json"

    def saved_spec(self, name: str) -> SessionSpec | None:
        path = self.spec_path(name)
        if not path.is_file():
            return None
        raw = json.loads(path.read_text())
        return SessionSpec(**{**raw, "allow": tuple(raw.get("allow", ())), "inject": tuple(raw.get("inject", ()))})

    def config(self, overrides: dict[str, Any] | None = None) -> HarnessConfig:
        return HarnessConfig.load(None, overrides or {}) if overrides else self.base

    def info(self) -> dict[str, Any]:
        """What the form needs to know: which credentials this process can hand on, and the defaults."""
        cfg = self.base
        return {
            "backend": "podman",
            "credentials": {
                "git": bool(git.token()),
                "anthropic_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "claude_oauth_token": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
            },
            "defaults": {"provider": cfg.provider, "base_url": cfg.base_url, "model": cfg.model_id,
                         "allow": list(cfg.egress_allow), "allowed_for_good": containers.approved(cfg)},
            "agents": list(session.AGENTS), "providers": list(PROVIDERS),
        }

    def sessions(self) -> list[dict[str, Any]]:
        rows = {row["name"]: row for row in session.list_sessions(self.base)}
        with self.lock:
            jobs = dict(self.jobs)
        found = []
        for name in sorted(set(rows) | set(jobs)):
            record = session.load_record(self.base, name)
            row = rows.get(name, {"name": name, "status": "absent"})
            status = row.get("status", "absent")
            job = jobs.get(name)
            if job and job.state == "starting":
                status = "starting"
            elif job and job.state == "failed" and status != "running":
                status = "failed"
            item: dict[str, Any] = {
                "name": name, "status": status, "error": job.error if job and status == "failed" else "",
                "agent": record.get("agent") or "opencode", "repo": record.get("repo_url") or "",
                "egress": record.get("egress") or "", "allow": record.get("allow") or [],
                "root": bool(record.get("root")), "idle": record.get("idle") or 0, "lifetime": record.get("lifetime") or 0,
                "started": record.get("started") or "",
            }
            if record and status == "running":
                item["urls"] = session.urls(record)
                item["password"] = record.get("password") if item["agent"] == "opencode" else ""
                item["token"] = (record.get("terminal") or {}).get("token", "")
            found.append(item)
        return found

    def _run(self, name: str, work: Any) -> None:
        with self.lock:
            if (job := self.jobs.get(name)) and job.state == "starting":
                raise SpecError(f"Session {name} is already starting.")
            self.jobs[name] = Job("starting")

        def go() -> None:
            try:
                work()
            except Exception as err:  # reported in the list, where the user is looking
                with self.lock:
                    self.jobs[name] = Job("failed", error=str(err) or type(err).__name__)
            else:
                with self.lock:
                    self.jobs.pop(name, None)

        self.pool.submit(go)

    def create(self, spec: SessionSpec) -> None:
        if session.load_record(self.base, spec.name) or session.state(spec.name) != "absent":
            raise SpecError(f"There is already a session named {spec.name}; start it, or remove it first.")
        cfg = self.config(spec.overrides())
        path = self.spec_path(spec.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(spec), indent=2) + "\n")
        self._run(spec.name, lambda: session.launch(
            cfg, spec.name, agent=spec.agent, allow=spec.allow, terminal=spec.terminal,
            idle=spec.idle or "0", lifetime=spec.lifetime or "0",
        ))

    def start(self, name: str) -> None:
        record = session.load_record(self.base, name)
        if not record:
            raise SpecError(f"No session named {name}.")
        if spec := self.saved_spec(name):
            # The allowlist is the session's own record's: approvals since creation are in it.
            cfg = self.config(spec.overrides())
        else:  # made outside the orchestrator: what its record says
            cfg = self.config({"egress": record.get("egress") or "allowlist", "container_root": bool(record.get("root")),
                               **({"repo_url": record["repo_url"]} if record.get("repo_url") else {})})
        self._run(name, lambda: session.launch(cfg, name))

    def stop(self, name: str) -> str:
        with self.lock:
            self.jobs.pop(name, None)
        return session.stop(self.base, name)

    def remove(self, name: str) -> str:
        with self.lock:
            self.jobs.pop(name, None)
        self.spec_path(name).unlink(missing_ok=True)
        return session.remove(self.base, name)

    def pending(self, name: str) -> list[dict[str, Any]]:
        box = session.container_name(name)
        return [row for row in egress.pending() if row["sandbox"] == box]

    def approve(self, request_id: str, scope: str) -> str:
        return egress.approve(self.base, request_id, scope)

    async def events(self, name: str) -> AsyncIterator[str]:
        """The gateway's events as they happen, after the last few hundred it already has."""
        proxy = f"{session.container_name(name)}-egress"
        proc = await asyncio.create_subprocess_exec(
            session.BINARY, "logs", "--follow", "--tail", "300", proxy,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            assert proc.stdout is not None
            while line := await proc.stdout.readline():
                text = line.decode(errors="replace").strip()
                if text.startswith("{"):
                    yield text
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
