"""The orchestrator's web app: create, list, start, stop and remove sessions,
watch what their gateways see, and approve what they refused.

Local only for now. There is no login, so two things stand in for one:

* It refuses requests whose Host is not this machine, so a web page cannot
  reach it by pointing a name of its own at 127.0.0.1 (DNS rebinding).
* Anything that changes something needs the `X-Orchestrator: 1` header. A page
  on another site cannot send a custom header here without a CORS preflight,
  which this app never answers, so it cannot drive the API from your browser.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from harness import containers, egress, git, session
from harness.exec_target import ExecError

from .backend import Backend, SessionSpec, SpecError
from .jobs import JobRunner, JobsError

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
CHANGE_HEADER = "x-orchestrator"
# What the harness raises for something the user can fix: shown to them, not a 500.
USER_ERRORS = (SpecError, session.SessionError, egress.EgressError, containers.ContainerError, git.GitError, ExecError,
               JobsError)


def create_app(backend: Backend, jobs: JobRunner | None = None) -> FastAPI:
    app = FastAPI(title="burst orchestrator", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Any:
        host = request.headers.get("host", "").rsplit(":", 1)[0] if not request.headers.get("host", "").startswith("[") \
            else request.headers.get("host", "").split("]")[0] + "]"
        if host not in LOCAL_HOSTS:
            return JSONResponse({"error": "This orchestrator only answers to this machine."}, status_code=403)
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
    def info() -> dict[str, Any]:
        return backend.info()

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
        return {"directories": [str(d) for d in jobs.directories], "jobs": jobs.jobs()}

    @app.get("/api/jobs/{name}")
    def job_detail(name: str) -> dict[str, Any]:
        return runner().job(name)

    @app.post("/api/jobs/{name}/run", status_code=202)
    def run_job(name: str) -> dict[str, str]:
        return {"name": name, "run": runner().run(name)}

    @app.get("/api/jobs/{name}/runs/{run_id}")
    def run_detail(name: str, run_id: str) -> dict[str, Any]:
        return runner().run_detail(name, run_id)

    return app
