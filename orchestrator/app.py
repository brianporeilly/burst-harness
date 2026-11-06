"""The orchestrator's web app: create, list, start, stop and remove sessions,
watch what their gateways see, and approve what they refused.

It has no login of its own. Run on a laptop, two things stand in for one:

* It refuses requests whose Host is not this machine, so a web page cannot
  reach it by pointing a name of its own at 127.0.0.1 (DNS rebinding).
* Anything that changes something needs the `X-Orchestrator: 1` header. A page
  on another site cannot send a custom header here without a CORS preflight,
  which this app never answers, so it cannot drive the API from your browser.

In a cluster it sits behind a proxy that signs people in (Envoy Gateway's
OIDC, in home-ops): `hosts` adds the name it is reached by to the Host check,
and `user_header` names the header the proxy sets to who signed in. A request
without that header is refused, and the header is only as good as the network
policy that lets nothing but the proxy reach this app.

Scripts, other machines and bots use an API token instead (tokens.py), which
reaches /api/tasks only: never the Settings (settings.py, profiles.py), where
credentials and the profiles tasks hand work to are set. With `local_browser` (listening beyond this machine
with no signing-in proxy), the browser's way in is for this machine alone, and
anyone else needs a token.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from harness import containers, credentials, egress, git, mcp, session
from harness.exec_target import ExecError

from .backend import Backend, SessionSpec, SpecError, probe
from .jobs import JobRunner, JobsError
from .launch import HANDOFF_PATH, LaunchError
from .profiles import ProfilesError, ProfileStore
from .settings import CredentialStore, SettingsError
from .slack import SlackBot
from .tasks import TaskRunner, TasksError
from .tokens import TokenError, TokenStore, user_of

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
CHANGE_HEADER = "x-orchestrator"
# What the harness raises for something the user can fix: shown to them, not a 500.
USER_ERRORS = (LaunchError, SpecError, session.SessionError, egress.EgressError, containers.ContainerError, git.GitError, ExecError,
               JobsError, TasksError, TokenError, SettingsError, ProfilesError)
LOOPBACK = ("127.0.0.1", "::1")


def create_app(backend: Backend, jobs: JobRunner | None = None, *, tasks: TaskRunner | None = None,
               tokens: TokenStore | None = None, settings: CredentialStore | None = None,
               profiles: ProfileStore | None = None, slack: SlackBot | None = None,
               hosts: tuple[str, ...] = (), user_header: str = "", local_browser: bool = False) -> FastAPI:
    app = FastAPI(title="burst orchestrator", docs_url=None, redoc_url=None, openapi_url=None)
    allowed = {*LOCAL_HOSTS, *(host.lower() for host in hosts)}

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Any:
        if request.url.path == HANDOFF_PATH:
            return await call_next(request)  # a burst's pod, by its address: its one-time token is checked there
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            record = tokens.check(presented) if tokens is not None else None
            if record is None:
                return JSONResponse({"error": "That token is not valid (revoked, or mistyped)."}, status_code=401)
            path = request.url.path
            if path != "/api/tasks" and not path.startswith("/api/tasks/"):
                return JSONResponse({"error": "A token reaches /api/tasks only."}, status_code=403)
            request.state.user = user_of(record)
            return await call_next(request)
        if local_browser and (request.client is None or request.client.host not in LOOPBACK):
            return JSONResponse({"error": "From another machine, use an API token (Authorization: Bearer ...)."},
                                status_code=401)
        request.state.user = request.headers.get(user_header, "").strip() if user_header else ""
        host = request.headers.get("host", "").rsplit(":", 1)[0] if not request.headers.get("host", "").startswith("[") \
            else request.headers.get("host", "").split("]")[0] + "]"
        if host.lower() not in allowed:
            return JSONResponse({"error": f"This orchestrator does not answer to {host or 'no host'}."}, status_code=403)
        if user_header and not request.headers.get(user_header, "").strip():
            return JSONResponse({"error": "Not signed in."}, status_code=401)
        if request.method not in ("GET", "HEAD") and request.headers.get(CHANGE_HEADER) != "1":
            return JSONResponse({"error": f"Missing the {CHANGE_HEADER} header."}, status_code=403)
        return await call_next(request)

    async def user_error(request: Request, err: Exception) -> JSONResponse:
        return JSONResponse({"error": str(err)}, status_code=400)

    for kind in USER_ERRORS:
        app.add_exception_handler(kind, user_error)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/info")
    def info(request: Request) -> dict[str, Any]:
        return {**backend.info(), "user": request.state.user, "slack": slack.status if slack else None}

    @app.get("/api/sessions")
    def sessions() -> list[dict[str, Any]]:
        return backend.sessions()

    @app.post("/api/sessions", status_code=202)
    async def create(request: Request) -> dict[str, str]:
        try:
            raw = await request.json()
        except ValueError as err:
            raise HTTPException(400, "The body must be JSON.") from err
        if not isinstance(raw, dict):
            raise HTTPException(400, "The body must be a JSON object.")
        spec = SessionSpec.parse(raw)
        await run_in_threadpool(backend.create, spec)
        return {"name": spec.name, "status": "starting"}

    @app.post("/api/sessions/{name}/start", status_code=202)
    def start(name: str) -> dict[str, str]:
        backend.start(name)
        return {"name": name, "status": "starting"}

    @app.post("/api/sessions/{name}/stop")
    def stop(name: str) -> dict[str, str]:
        return {"message": backend.stop(name)}

    @app.delete("/api/sessions/{name}")
    def remove(name: str) -> dict[str, str]:
        return {"message": backend.remove(name)}

    @app.get("/api/sessions/{name}/pending")
    def pending(name: str) -> list[dict[str, Any]]:
        return backend.pending(name)

    @app.post("/api/approve")
    async def approve(request: Request) -> dict[str, str]:
        raw = await request.json()
        if not isinstance(raw, dict) or not raw.get("id"):
            raise HTTPException(400, "Say which refusal: {\"id\": ..., \"scope\": ...}.")
        return {"message": await run_in_threadpool(backend.approve, str(raw["id"]), str(raw.get("scope") or "session"))}

    @app.get("/api/sessions/{name}/events")
    async def events(name: str) -> StreamingResponse:
        session.container_name(name)  # checks the name before it reaches a command line

        async def stream() -> Any:
            async for line in backend.events(name):
                yield f"data: {json.dumps(json.loads(line))}\n\n"
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    # --- jobs ---

    def runner() -> JobRunner:
        if jobs is None:
            raise JobsError("No job directories: start the orchestrator with --jobs DIR.")
        return jobs

    @app.get("/api/jobs")
    def list_jobs() -> dict[str, Any]:
        if jobs is None:
            return {"directories": [], "jobs": []}
        return {"directories": [str(d) for d in jobs.directories], "jobs": jobs.jobs(),
                "schedule": {"on": jobs.schedule, "timezone": str(jobs.zone)}}

    @app.get("/api/jobs/{name}")
    def job_detail(name: str) -> dict[str, Any]:
        return runner().job(name)

    @app.post("/api/jobs/{name}/run", status_code=202)
    def run_job(name: str) -> dict[str, str]:
        return {"name": name, "run": runner().run(name)}

    @app.post("/api/jobs/{name}/pause")
    def pause_job(name: str) -> dict[str, Any]:
        runner().pause(name, True)
        return {"name": name, "paused": True}

    @app.post("/api/jobs/{name}/resume")
    def resume_job(name: str) -> dict[str, Any]:
        runner().pause(name, False)
        return {"name": name, "paused": False}

    @app.get("/api/jobs/{name}/runs/{run_id}")
    def run_detail(name: str, run_id: str) -> dict[str, Any]:
        return runner().run_detail(name, run_id)

    # --- tasks ---

    def task_runner() -> TaskRunner:
        if tasks is None:
            raise TasksError("Tasks are not on in this orchestrator.")
        return tasks

    async def body(request: Request) -> dict[str, Any]:
        try:
            raw = await request.json()
        except ValueError as err:
            raise HTTPException(400, "The body must be JSON.") from err
        if not isinstance(raw, dict):
            raise HTTPException(400, "The body must be a JSON object.")
        return raw

    @app.get("/api/tasks")
    def list_tasks() -> dict[str, Any]:
        if tasks is None:
            return {"on": False, "tasks": []}
        return {"on": True, "limit": tasks.limit, "tasks": tasks.tasks()}

    @app.post("/api/tasks", status_code=202)
    async def create_task(request: Request) -> dict[str, Any]:
        return await run_in_threadpool(task_runner().create, await body(request), user=request.state.user)

    @app.post(HANDOFF_PATH)
    def handoff(request: Request) -> dict[str, Any]:
        """A burst's pod trading its one-time token for its credentials (launch.py, burst.py)."""
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        values = tasks.handoff(presented.strip()) if tasks is not None and scheme.lower() == "bearer" else None
        if values is None:
            return JSONResponse({"error": "No credentials are kept under that token (used, expired, or not one)."},
                                status_code=401)
        return {"env": values}

    @app.get("/api/probe")
    def probe_server(base_url: str) -> dict[str, Any]:
        """A llama-server's own description, for the Tasks form (backend.probe)."""
        return probe(base_url)

    @app.get("/api/tasks/{slug}")
    def task_detail(slug: str) -> dict[str, Any]:
        return task_runner().task(slug)

    @app.post("/api/tasks/{slug}/continue", status_code=202)
    async def continue_task(slug: str, request: Request) -> dict[str, Any]:
        raw = await body(request) if await request.body() else {}
        return await run_in_threadpool(task_runner().resume, slug, str(raw.get("message") or ""))

    @app.post("/api/tasks/{slug}/cancel")
    def cancel_task(slug: str) -> dict[str, Any]:
        return task_runner().cancel(slug)

    @app.delete("/api/tasks/{slug}")
    def remove_task(slug: str) -> dict[str, str]:
        task_runner().remove(slug)
        return {"message": f"Removed {slug}; its branch and PR are left as they are."}

    # --- API tokens (never with a token: see the guard) ---

    def token_store() -> TokenStore:
        if tokens is None:
            raise TokenError("API tokens are not on in this orchestrator.")
        return tokens

    @app.get("/api/tokens")
    def list_tokens() -> list[dict[str, Any]]:
        return token_store().tokens()

    @app.post("/api/tokens", status_code=201)
    async def create_token(request: Request) -> dict[str, Any]:
        record, token = token_store().create(str((await body(request)).get("name") or ""), user=request.state.user)
        return {**record, "token": token}

    @app.delete("/api/tokens/{ident}")
    def revoke_token(ident: str) -> dict[str, str]:
        record = token_store().revoke(ident)
        return {"message": f"Revoked {record['name']}."}

    # --- Settings: credentials (never with a token: see the guard) ---

    def credential_store() -> CredentialStore:
        if settings is None:
            raise SettingsError("Settings are not on in this orchestrator.")
        return settings

    @app.get("/api/settings/credentials")
    def list_credentials() -> list[dict[str, Any]]:
        # What this orchestrator's sessions, tasks and jobs would use: its MCP servers' keys, its jobs' secrets.
        base = getattr(backend, "base", None)
        names = [*(name for name in credentials.GIT if os.environ.get(name)),
                 *(mcp.key_var(name) for name in (mcp.servers(base) if base else {})),
                 *(jobs.secret_names() if jobs else [])]
        return credential_store().listing(names)

    @app.put("/api/settings/credentials/{name}")
    async def set_credential(name: str, request: Request) -> dict[str, str]:
        credential_store().set(name, str((await body(request)).get("value") or ""), user=request.state.user)
        return {"message": f"Set {name}. What starts from now on uses it."}

    @app.delete("/api/settings/credentials/{name}")
    def clear_credential(name: str) -> dict[str, str]:
        if not credential_store().clear(name):
            raise SettingsError(f"{name} is not set here.")
        back = "the environment's value" if credentials.source(name) else "nothing: it has no value now"
        return {"message": f"Cleared {name}; from now on it is {back}."}

    # --- Settings: profiles, the other agents a task's lead may hand work to (never with a token) ---

    def profile_store() -> ProfileStore:
        if profiles is None:
            raise ProfilesError("Settings are not on in this orchestrator.")
        return profiles

    @app.get("/api/settings/profiles")
    def list_profiles() -> dict[str, Any]:
        return profile_store().listing()

    @app.put("/api/settings/profiles/{name}")
    async def set_profile(name: str, request: Request) -> dict[str, str]:
        profile_store().set(name, await body(request), user=request.state.user)
        return {"message": f"Set {name}. Bursts that start from now on use it."}

    @app.delete("/api/settings/profiles/{name}")
    def remove_profile(name: str) -> dict[str, str]:
        now = profile_store().remove(name)
        return {"message": f"Removed {name}." + (" The configuration's is used again." if now != "gone" else "")}

    @app.put("/api/settings/delegate_to")
    async def set_delegates(request: Request) -> dict[str, str]:
        raw = await body(request)
        profile_store().set_default(raw.get("names"), user=request.state.user)
        return {"message": "New tasks hand work to " + (", ".join(profile_store().default()) or "no one")
                + (" (the configuration's)." if raw.get("names") is None else ".")}

    return app
