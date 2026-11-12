"""Where sessions run, behind one interface the web app talks to.

`PodmanBackend` drives `harness session` and `harness egress` on this machine.
`ClusterBackend` does the same for sessions on a cluster (agent-sandbox claims
behind the namespace's shared gateway): `harness.cluster_session` for the
sessions, and the gateway's control API for their events and approvals. The app
never calls the harness directly, so it does not know which it has.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import urllib.request
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from harness import cluster_session, containers, credentials, egress, git, mcp, models, session
from harness.config import HarnessConfig
from harness.gateway_control import Gateway, GatewayError

from .settings import orchestrator_dir

# What a session's opencode can be given. A task can use any provider the harness has (task_providers).
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
    # NAME=URL entries; None (not given) is the orchestrator's own `mcp` setting.
    mcp: tuple[str, ...] | None = None

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
            context_window=int(raw.get("context_window") or 0), mcp=servers(raw.get("mcp")),
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
        if spec.repo_url and not spec.repo_url.startswith("https://"):
            raise SpecError("The repo must be an https:// URL: a session reaches the network only through its gateway.")
        check_model(spec.provider, spec.base_url, spec.model)
        return spec

    def overrides(self, base: HarnessConfig) -> dict[str, Any]:
        """The harness configuration this spec stands for, on top of `base` (the orchestrator's own)."""
        chosen: dict[str, Any] = {
            "repo_url": self.repo_url, "egress": self.egress, "egress_inject": self.inject,
            "container_root": self.root, "mcp": self.mcp,
        }
        return {key: value for key, value in chosen.items() if value is not None} | model_choice(
            base, self.provider, self.base_url, self.model, self.context_window)


# The settings that go with one provider and mean nothing to another: a different provider starts
# from the harness's defaults for them, not from the orchestrator's (typhon's URL is no Anthropic endpoint).
PROVIDER_SETTINGS = ("base_url", "model_id", "context_window_limit", "model_params", "token_command", "token_file")


def task_providers() -> list[str]:
    """What a task's burst can use: the built-ins and installed provider plugins."""
    return models.available()


def check_model(provider: str, base_url: str, model: str, providers: tuple[str, ...] | list[str] = PROVIDERS) -> None:
    """A provider, base URL and model id as a form or the API gives them (empty: the default)."""
    if provider and provider not in providers:
        raise SpecError(f"provider is one of {', '.join(providers)}.")
    if base_url and not re.fullmatch(r"https?://[A-Za-z0-9.:\[\]_-]+(/[A-Za-z0-9._~/-]*)?", base_url):
        raise SpecError(f"{base_url!r} is not a base URL: http(s)://host[:port][/path].")
    if model and not re.fullmatch(r"[A-Za-z0-9._:/@-]{1,100}", model):
        raise SpecError(f"{model!r} is not a model id.")


# A task's model settings (model_settings): how hard it thinks, and the raw request parameters.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODEL_PARAMS_CHARS = 8000


def model_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """A task's model settings as a form or the API gives them, checked; what is not given is left out.

    effort and thinking are Anthropic's (the Messages API's effort, and adaptive
    thinking on or off); max_tokens caps one reply on any provider; model_params
    is a JSON object merged into every request last, for what the others do not
    cover (llama.cpp's chat_template_kwargs.enable_thinking, an OpenAI-compatible
    server's reasoning_effort, temperature...). Raises SpecError.
    """
    found: dict[str, Any] = {}
    effort = str(raw.get("effort") or "").strip()
    if effort:
        if effort not in EFFORTS:
            raise SpecError(f"effort is one of {', '.join(EFFORTS)}.")
        found["effort"] = effort
    thinking = raw.get("thinking")
    if isinstance(thinking, str):
        thinking = {"": None, "on": True, "true": True, "off": False, "false": False}.get(thinking.strip().lower(), thinking)
    if thinking is not None:
        if not isinstance(thinking, bool):
            raise SpecError("thinking is on or off (true or false).")
        found["thinking"] = thinking
    try:
        max_tokens = int(raw.get("max_tokens") or 0)
    except (TypeError, ValueError):
        max_tokens = -1
    if max_tokens < 0:
        raise SpecError("max_tokens is a number of tokens.")
    if max_tokens:
        found["max_tokens"] = max_tokens
    params = raw.get("model_params")
    if isinstance(params, str):
        if len(params) > MODEL_PARAMS_CHARS:
            raise SpecError(f"model_params is at most {MODEL_PARAMS_CHARS} characters.")
        try:
            params = json.loads(params) if params.strip() else None
        except ValueError as err:
            raise SpecError(f"model_params is not JSON: {err}.") from err
    if params is not None:
        if not isinstance(params, dict):
            raise SpecError('model_params is a JSON object, such as {"temperature": 0.2}.')
        if params:
            found["model_params"] = params
    return found


def model_choice(base: HarnessConfig, provider: str, base_url: str, model: str, context_window: int,
                 settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """The harness settings for a chosen provider and model, on top of `base`. Empty means base's;
    another provider than base's resets the settings that went with base's. `settings` are
    model_settings', model_params as the JSON the harness takes."""
    chosen: dict[str, Any] = {}
    if provider and provider != base.provider:
        if provider in ("llamacpp", "openai") and not base_url:
            raise SpecError(f"provider {provider} needs a base URL.")
        defaults = HarnessConfig()
        chosen = {"provider": provider, **{name: getattr(defaults, name) for name in PROVIDER_SETTINGS}}
    for name, value in (("base_url", base_url), ("model_id", model), ("context_window_limit", context_window)):
        if value:
            chosen[name] = value
    for name, value in (settings or {}).items():
        chosen[name] = json.dumps(value) if name == "model_params" else value
    return chosen


# How much of a llama-server's /props is read: its chat template can be long, but not this long.
PROPS_BYTES = 4 << 20


def probe(base_url: str) -> dict[str, Any]:
    """What a llama-server at `base_url` says of itself (GET /props: a description, not a model call):
    its model, its context window per slot, its slots, and whether its chat template takes
    enable_thinking. {"found": False, "why": ...} when it cannot be read from here."""
    check_model("", base_url, "")
    if not base_url:
        raise SpecError("Give the server's base URL.")
    root = re.sub(r"/v1/?$", "", base_url.rstrip("/"))
    try:
        with urllib.request.urlopen(root + "/props", timeout=5) as response:
            props = json.loads(response.read(PROPS_BYTES))
    except (OSError, ValueError) as err:
        return {"found": False, "why": f"no llama-server description at {root}/props ({type(err).__name__}: {err})"}
    if not isinstance(props, dict):
        return {"found": False, "why": f"{root}/props is not a llama-server's description"}
    settings = props.get("default_generation_settings") if isinstance(props.get("default_generation_settings"), dict) else {}
    window = settings.get("n_ctx") or props.get("n_ctx")
    template = props.get("chat_template") if isinstance(props.get("chat_template"), str) else ""
    model = str(props.get("model_alias") or Path(str(props.get("model_path") or "")).name or "")
    return {"found": True, "model": model or None,
            "context_window": window if isinstance(window, int) and window > 0 else None,
            "slots": props.get("total_slots") if isinstance(props.get("total_slots"), int) else None,
            # Qwen's, GLM's and others' templates read it: chat_template_kwargs turns thinking off.
            "thinking": "enable_thinking" in template}


def servers(value: Any) -> tuple[str, ...] | None:
    """The form's MCP servers (NAME=URL, one per line or a list), checked; None if it gave none at all."""
    if value is None:
        return None
    items = value.split() if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise SpecError("mcp must be a list of NAME=URL.")
    found = tuple(item.strip() for item in items if item.strip())
    try:
        mcp.servers(replace(HarnessConfig(), engine="opencode", mcp=found))  # a session runs opencode
    except mcp.MCPError as err:
        raise SpecError(str(err)) from err
    return found


def _spec(raw: dict[str, Any]) -> SessionSpec:
    """A saved spec, back from its JSON."""
    found = {**raw, "allow": tuple(raw.get("allow", ())), "inject": tuple(raw.get("inject", ()))}
    if found.get("mcp") is not None:
        found["mcp"] = tuple(found["mcp"])
    return SessionSpec(**found)


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
    """Sessions on this machine's podman (or docker: target = docker), through the harness."""

    # Where list_sessions, load_record, urls, launch, stop and remove come from.
    harness: Any = session

    def __init__(self, base: HarnessConfig | None = None) -> None:
        self.base = base or HarnessConfig.load(None, {})
        session.use(self.base.target)
        self.name = session.BINARY
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        # Starting pulls images, clones and waits for UIs: never on the request's thread.
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="session-start")

    def spec_path(self, name: str) -> Path:
        """What a session was created with, so starting it again means the same thing."""
        return orchestrator_dir(self.base) / "orchestrator" / f"{name}.json"

    def saved_spec(self, name: str) -> SessionSpec | None:
        path = self.spec_path(name)
        if not path.is_file():
            return None
        return _spec(json.loads(path.read_text()))

    def config(self, overrides: dict[str, Any] | None = None) -> HarnessConfig:
        return HarnessConfig.load(None, overrides or {}) if overrides else self.base

    def info(self) -> dict[str, Any]:
        """What the form needs to know: which credentials this process can hand on, and the defaults."""
        cfg = self.base
        return {
            "backend": self.name,
            "credentials": {
                "git": bool(git.token()),
                "anthropic_api_key": bool(credentials.get("ANTHROPIC_API_KEY")),
                "claude_oauth_token": bool(credentials.get("CLAUDE_CODE_OAUTH_TOKEN")),
            },
            "defaults": {"provider": cfg.provider, "base_url": cfg.base_url, "model": models.model_name(cfg),
                         # A provider plugin that gives Claude Code its own endpoint (harness/models.py).
                         "claude_endpoint": models.provider_attr(cfg.provider, "target_endpoint") is not None,
                         "context_window": cfg.context_window_limit,
                         "allow": list(cfg.egress_allow), "allowed_for_good": containers.approved(cfg),
                         "mcp": list(cfg.mcp)},
            # Which servers' keys this process holds, for the gateway to add (never the keys).
            "mcp_keys": {name: bool(mcp.auth(name)) for name in mcp.servers(cfg)},
            "agents": list(session.AGENTS), "providers": list(PROVIDERS), "task_providers": task_providers(),
        }

    def sessions(self) -> list[dict[str, Any]]:
        rows = {row["name"]: row for row in self.harness.list_sessions(self.base)}
        with self.lock:
            jobs = dict(self.jobs)
        found = []
        for name in sorted(set(rows) | set(jobs)):
            record = self.harness.load_record(self.base, name)
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
                item["urls"] = self.harness.urls(record)
                # Signed in by the cluster, which adds them: nothing to show (cluster_session.py).
                signed = bool(record.get("sign_in"))
                item["password"] = record.get("password") if item["agent"] == "opencode" and not signed else ""
                item["token"] = "" if signed else (record.get("terminal") or {}).get("token", "")
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

    def exists(self, name: str) -> bool:
        return bool(session.load_record(self.base, name)) or session.state(name) != "absent"

    def create(self, spec: SessionSpec) -> None:
        if self.exists(spec.name):
            raise SpecError(f"There is already a session named {spec.name}; start it, or remove it first.")
        cfg = self.config(spec.overrides(self.base))
        path = self.spec_path(spec.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(spec), indent=2) + "\n")
        self._run(spec.name, lambda: self.harness.launch(
            cfg, spec.name, agent=spec.agent, allow=spec.allow, terminal=spec.terminal,
            idle=spec.idle or "0", lifetime=spec.lifetime or "0",
        ))

    def start(self, name: str) -> None:
        record = self.harness.load_record(self.base, name)
        if not record:
            raise SpecError(f"No session named {name}.")
        if spec := self.saved_spec(name):
            # The allowlist is the session's own record's: approvals since creation are in it.
            cfg = self.config(spec.overrides(self.base))
        else:  # made outside the orchestrator: what its record says
            cfg = self.config({"egress": record.get("egress") or "allowlist", "container_root": bool(record.get("root")),
                               **({"repo_url": record["repo_url"]} if record.get("repo_url") else {})})
        self._run(name, lambda: self.harness.launch(cfg, name))

    def stop(self, name: str) -> str:
        with self.lock:
            self.jobs.pop(name, None)
        return self.harness.stop(self.base, name)

    def remove(self, name: str) -> str:
        with self.lock:
            self.jobs.pop(name, None)
        self.spec_path(name).unlink(missing_ok=True)
        return self.harness.remove(self.base, name)

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


class ClusterBackend(PodmanBackend):
    """Sessions on a cluster: claimed, suspended and removed by harness.cluster_session, and
    watched and approved through the shared gateway's control API (the base config's
    target, namespace, pool, gateway_control and so on say which cluster and gateway)."""

    name = "agent-sandbox"
    harness = cluster_session
    POLL = 2.0

    def __init__(self, base: HarnessConfig | None = None) -> None:
        super().__init__(base)
        if self.base.target != "agent-sandbox":
            raise ValueError("ClusterBackend needs target = agent-sandbox in the configuration.")

    def gateway(self) -> Gateway:
        found = Gateway.from_config(self.base)
        if found is None:
            raise SpecError("Sessions on a cluster need gateway_control set.")
        return found

    def info(self) -> dict[str, Any]:
        found = super().info()
        found["defaults"]["allowed_for_good"] = []  # a cluster session's approvals go in its own allowlist
        found["namespace"] = self.base.namespace
        return found

    def exists(self, name: str) -> bool:
        return bool(cluster_session.load_record(self.base, name)) or cluster_session.state(self.base, name) != "absent"

    def _refusals(self, name: str) -> tuple[Gateway, list[dict[str, Any]], list[dict[str, Any]]]:
        """The session's gateway, the events it has kept, and its approvals."""
        gateway, claim = self.gateway(), cluster_session.claim_name(name)
        try:
            kept = gateway.events(claim).get("events") or []
        except GatewayError as err:
            if " 404 " in f" {err} ":
                return gateway, [], []  # not registered: stopped, or never started
            raise
        try:
            approvals = gateway.approvals(claim).get("approvals") or []
        except GatewayError:
            approvals = []  # a gateway from before approvals over the API: none of them
        return gateway, kept, approvals

    def pending(self, name: str) -> list[dict[str, Any]]:
        _, kept, approvals = self._refusals(name)
        return sorted(({"id": event.get("id") or "", "destination": event["destination"], "times": event["times"],
                        "last": event.get("ts") or "", "sandbox": cluster_session.claim_name(name)}
                       for event in egress.unapproved(kept, approvals)), key=lambda row: row["last"])

    def approve(self, request_id: str, scope: str) -> str:
        """Approve a refusal on the gateway, for the session whose events have it. `permanent`
        also adds it to the session's own allowlist, so its next start has it."""
        extra = egress._scope(scope)
        for row in self.harness.list_sessions(self.base):
            name = row["name"]
            gateway, kept, _ = self._refusals(name)
            if (event := next((e for e in kept if e.get("id") == request_id and e.get("verdict") == "deny"
                               and e.get("kind") in egress.DESTINATION_KINDS and e.get("host") not in (None, "-")),
                              None)) is None:
                continue
            where = egress.destination(event)
            try:
                gateway.approve(cluster_session.claim_name(name), {"allow": where, "id": request_id, **extra})
            except GatewayError as err:
                raise SpecError(str(err)) from err
            if scope.strip().lower() == "permanent":
                cluster_session.add_allow(self.base, name, where)
                return f"Approved {where} for session {name}, from now on."
            return f"Approved {where} for session {name} ({scope})."
        raise SpecError(f"No running session has a refusal with id {request_id}. A stopped session's are gone; "
                        "add the host to its allowlist instead.")

    async def events(self, name: str) -> AsyncIterator[str]:
        """The gateway's events for the session as they come, after the last few hundred it kept.
        Polled: the control API answers with what is new since a sequence number."""
        gateway, claim, after, first = self.gateway(), cluster_session.claim_name(name), 0, True
        while True:
            try:
                answer = await asyncio.to_thread(gateway.events, claim, after)
            except GatewayError:
                answer = None  # not registered now (stopped, starting): ask again
            if answer is not None and int(answer.get("next") or 0) < after:
                after = 0  # registered afresh (a start), so counted from 0 again
                continue
            if answer is not None:
                found = answer.get("events") or []
                for event in found[-300:] if first else found:
                    yield json.dumps(event)
                after, first = int(answer.get("next") or after), False
            await asyncio.sleep(self.POLL)
