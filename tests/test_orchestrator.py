import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from harness.config import HarnessConfig
from harness.gateway_control import GatewayError
from orchestrator.app import create_app  # noqa: E402
from orchestrator.backend import SessionSpec, SpecError  # noqa: E402
from orchestrator.jobs import JobsError

CHANGE = {"X-Orchestrator": "1"}


class FakeBackend:
    def __init__(self):
        self.created, self.approved, self.stopped = [], [], []

    def info(self):
        return {"backend": "fake", "credentials": {}, "defaults": {}}

    def sessions(self):
        return [{"name": spec.name, "status": "starting"} for spec in self.created]

    def create(self, spec):
        if any(s.name == spec.name for s in self.created):
            raise SpecError(f"There is already a session named {spec.name}.")
        self.created.append(spec)

    def start(self, name):
        pass

    def stop(self, name):
        self.stopped.append(name)
        return f"Stopped {name}."

    def remove(self, name):
        return f"Removed {name}."

    def pending(self, name):
        return [{"id": "a-1", "destination": "example.com", "times": 2, "last": "x", "sandbox": "burst-session-" + name}]

    def approve(self, request_id, scope):
        self.approved.append((request_id, scope))
        return "ok"

    async def events(self, name):
        yield '{"kind": "connect", "verdict": "allow", "host": "github.com"}'


@pytest.fixture
def client():
    backend = FakeBackend()
    test = TestClient(create_app(backend), base_url="http://127.0.0.1:8700")
    test.backend = backend
    return test


def test_page_and_info(client):
    assert "Burst Orchestrator" in client.get("/").text
    assert client.get("/api/info").json()["backend"] == "fake"


def test_create_checks_the_spec_and_starts_it(client):
    spec = {"name": "box", "agent": "claude-code", "allow": "example.com\nllm.lan:8080", "inject": ["model"],
            "idle": "2h", "root": True}
    reply = client.post("/api/sessions", json=spec, headers=CHANGE)
    assert reply.status_code == 202 and reply.json() == {"name": "box", "status": "starting"}
    made = client.backend.created[0]
    assert made.allow == ("example.com", "llm.lan:8080") and made.inject == ("model",) and made.root
    again = client.post("/api/sessions", json=spec, headers=CHANGE)
    assert again.status_code == 400 and "already" in again.json()["error"]


@pytest.mark.parametrize("spec,message", [
    ({"name": "Bad Name"}, "lowercase"),
    ({"name": "x", "agent": "vim"}, "agent"),
    ({"name": "x", "egress": "open"}, "egress"),
    ({"name": "x", "allow": "http://example.com"}, "allowlist entry"),
    ({"name": "x", "inject": ["ssh"]}, "inject takes"),
    ({"name": "x", "repo_url": "git@github.com:o/r.git"}, "https://"),
    ({"name": "x", "provider": "llamacpp", "base_url": "llm.lan"}, "not a base URL"),
    ({"name": "x", "idle": "soon"}, "duration"),
])
def test_bad_specs_are_refused_with_a_reason(client, spec, message):
    reply = client.post("/api/sessions", json=spec, headers=CHANGE)
    assert reply.status_code == 400 and message in reply.json()["error"]


def test_changes_need_the_header(client):
    assert client.post("/api/sessions", json={"name": "box"}).status_code == 403
    assert client.post("/api/sessions/box/stop").status_code == 403
    assert client.delete("/api/sessions/box").status_code == 403
    assert client.post("/api/sessions/box/stop", headers=CHANGE).json() == {"message": "Stopped box."}


def test_only_this_machine_may_ask(client):
    for host in ("evil.example", "127.0.0.1.evil.example", "10.0.0.5:8700"):
        assert client.get("/api/sessions", headers={"Host": host}).status_code == 403
    assert client.get("/api/sessions", headers={"Host": "localhost:8700"}).status_code == 200


def test_behind_a_signing_in_proxy():
    app = create_app(FakeBackend(), hosts=("Orchestrator.example",), user_header="Remote-User")
    proxied = TestClient(app, base_url="https://orchestrator.example")
    who = {"Remote-User": "me@example.com"}
    assert proxied.get("/api/sessions").status_code == 401  # not through the proxy's sign-in
    assert proxied.get("/api/sessions", headers={"Remote-User": " "}).status_code == 401
    assert proxied.get("/api/info", headers=who).json()["user"] == "me@example.com"
    assert proxied.get("/api/sessions", headers={**who, "Host": "evil.example"}).status_code == 403
    assert proxied.post("/api/sessions/box/stop", headers=who).status_code == 403  # still needs the header
    assert proxied.post("/api/sessions/box/stop", headers={**who, **CHANGE}).json() == {"message": "Stopped box."}


def test_pending_approve_and_events(client):
    assert client.get("/api/sessions/box/pending").json()[0]["destination"] == "example.com"
    assert client.post("/api/approve", json={"id": "a-1", "scope": "permanent"}, headers=CHANGE).json() == {"message": "ok"}
    assert client.backend.approved == [("a-1", "permanent")]
    assert client.post("/api/approve", json={}, headers=CHANGE).status_code == 400
    body = client.get("/api/sessions/box/events").text
    assert 'data: {"kind": "connect"' in body and "event: end" in body
    assert client.get("/api/sessions/Bad%20Name/events").status_code == 400


def test_spec_overrides_only_what_was_chosen():
    base = HarnessConfig(provider="llamacpp", base_url="http://typhon:8080", model_id="raon")
    spec = SessionSpec.parse({"name": "box", "provider": "llamacpp", "model": "qwen"})
    assert spec.overrides(base) == {"repo_url": "", "egress": "allowlist", "egress_inject": (), "container_root": False,
                                    "model_id": "qwen"}
    # Another provider starts from its own defaults, not the orchestrator's URL and model.
    other = SessionSpec.parse({"name": "box", "provider": "anthropic"}).overrides(base)
    assert (other["provider"], other["base_url"], other["model_id"]) == ("anthropic", "", HarnessConfig().model_id)
    with pytest.raises(SpecError, match="needs a base URL"):
        SessionSpec.parse({"name": "box", "provider": "openai"}).overrides(base)


def test_mcp_servers_in_a_spec_are_checked_and_kept():
    spec = SessionSpec.parse({"name": "box", "mcp": "context7=https://mcp.context7.com/mcp\n\n"})
    assert spec.mcp == ("context7=https://mcp.context7.com/mcp",)
    assert spec.overrides(HarnessConfig())["mcp"] == spec.mcp
    # An empty field is none at all; no field at all is the orchestrator's own setting.
    assert SessionSpec.parse({"name": "box", "mcp": ""}).overrides(HarnessConfig())["mcp"] == ()
    assert "mcp" not in SessionSpec.parse({"name": "box"}).overrides(HarnessConfig())
    with pytest.raises(SpecError, match="only passes HTTPS"):
        SessionSpec.parse({"name": "box", "mcp": "c7=http://mcp.context7.com/mcp"})
    with pytest.raises(SpecError, match="only passes HTTPS"):  # a session's opencode is in the sandbox
        SessionSpec.parse({"name": "box", "mcp": "local=http://127.0.0.1:8931/mcp"})


def test_a_saved_spec_keeps_its_mcp_servers(tmp_path):
    from dataclasses import asdict

    from harness.config import HarnessConfig
    from orchestrator.backend import PodmanBackend

    backend = PodmanBackend(HarnessConfig(state_dir=str(tmp_path), mcp=("c7=https://mcp.context7.com/mcp",)))
    spec = SessionSpec.parse({"name": "box", "mcp": ""})
    backend.spec_path("box").parent.mkdir(parents=True)
    backend.spec_path("box").write_text(json.dumps(asdict(spec)))
    assert backend.saved_spec("box") == spec
    assert backend.info()["defaults"]["mcp"] == ["c7=https://mcp.context7.com/mcp"]
    assert backend.info()["mcp_keys"] == {"c7": False}


def _jobs_dir(tmp_path):
    from harness import job

    root = tmp_path / "my-jobs"
    for name, extra in (("digest", '[job.secrets.gh]\nenv = "GITHUB_TOKEN"\nhost = "api.github.com"\n'),
                        ("watcher", "")):
        (root / name).mkdir(parents=True)
        (root / name / "prompt.md").write_text("do it")
        (root / name / "job.toml").write_text(extra)
    (root / "not-a-job").mkdir()
    return root, job


@pytest.fixture
def jobs_client(tmp_path):
    from orchestrator.jobs import JobRunner

    root, job = _jobs_dir(tmp_path)
    runner = JobRunner([root])
    test = TestClient(create_app(FakeBackend(), runner), base_url="http://127.0.0.1:8700")
    test.runner, test.job = runner, job
    return test


def test_jobs_are_found_with_what_they_lack_and_their_runs(jobs_client, monkeypatch):
    job = jobs_client.job
    listed = jobs_client.get("/api/jobs").json()
    assert [j["name"] for j in listed["jobs"]] == ["digest", "watcher"]
    digest = listed["jobs"][0]
    assert digest["missing"] == ["GITHUB_TOKEN"] and digest["last_run"] is None and digest["engine"] == "strands"
    # A run made from the command line is listed: the record is the harness's.
    cfg = job.config(job.load(jobs_client.runner.discover()["watcher"]))
    run_dir = job.runs_dir(cfg, "watcher") / "20261020T120000Z-abcd"
    job._write_run(run_dir, {"id": "20261020T120000Z-abcd", "job": "watcher", "state": "reported",
                             "started": "2026-10-20T12:00:00Z", "seconds": 9, "egress": {"requests": {"a.com": 2}}})
    (run_dir / "report.md").write_text("# All clear")
    detail = jobs_client.get("/api/jobs/watcher").json()
    assert detail["last_success"]["id"] == "20261020T120000Z-abcd" and "trigger" in detail["describe"]
    assert [r["id"] for r in detail["runs"]] == ["20261020T120000Z-abcd"]
    run = jobs_client.get("/api/jobs/watcher/runs/20261020T120000Z-abcd").json()
    assert run["report"] == "# All clear" and run["output"] is None
    assert jobs_client.get("/api/jobs/watcher/runs/../../etc").status_code in (400, 404)
    assert jobs_client.get("/api/jobs/watcher/runs/20991020T120000Z-ffff").status_code == 400
    assert jobs_client.get("/api/jobs/nope").status_code == 400


def test_a_run_whose_process_is_gone_is_interrupted(jobs_client):
    job = jobs_client.job
    cfg = job.config(job.load(jobs_client.runner.discover()["watcher"]))
    job._write_run(job.runs_dir(cfg, "watcher") / "20261020T120000Z-abcd",
                   {"id": "20261020T120000Z-abcd", "job": "watcher", "state": "running", "pid": 2 ** 22 + 12345})
    detail = jobs_client.get("/api/jobs/watcher").json()
    assert detail["runs"][0]["state"] == "interrupted" and not detail["running"]


def test_running_a_job_starts_the_harness_once(jobs_client, monkeypatch):
    from orchestrator import launch as launch_module

    monkeypatch.setenv("GH_TOKEN", "orchestrators")
    monkeypatch.setenv("KUBE_TOKEN", "watchers")

    started = []

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            started.append((argv, kwargs["env"]))

        def wait(self):
            import time
            time.sleep(0.5)

    monkeypatch.setattr(launch_module.subprocess, "Popen", FakeProcess)
    assert jobs_client.post("/api/jobs/digest/run").status_code == 403  # no X-Orchestrator header
    refused = jobs_client.post("/api/jobs/digest/run", headers=CHANGE)
    assert refused.status_code == 400 and "GITHUB_TOKEN" in refused.json()["error"]
    response = jobs_client.post("/api/jobs/watcher/run", headers=CHANGE)
    assert response.status_code == 202
    run_id = response.json()["run"]
    (argv, env), = started
    assert argv[-3:] == ["job", "run", str(jobs_client.runner.discover()["watcher"])]
    assert "GH_TOKEN" not in env and "KUBE_TOKEN" not in env  # nothing it was not granted
    assert env[jobs_client.job.RUN_ID_ENV] == run_id
    assert jobs_client.post("/api/jobs/watcher/run", headers=CHANGE).status_code == 400  # already running
    assert jobs_client.get("/api/jobs/watcher").json()["runs"][0] == {"id": run_id, "job": "watcher", "state": "starting"}


def test_a_job_run_gets_the_secrets_its_job_names(jobs_client, monkeypatch):
    from orchestrator import launch as launch_module

    started = []

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            started.append(kwargs["env"])

        def wait(self):
            pass

    monkeypatch.setattr(launch_module.subprocess, "Popen", FakeProcess)
    monkeypatch.setenv("GITHUB_TOKEN", "digests")
    monkeypatch.setenv("HARNESS_GIT_TOKEN", "tasks")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack")
    assert jobs_client.post("/api/jobs/digest/run", headers=CHANGE).status_code == 202
    env, = started
    assert env["GITHUB_TOKEN"] == "digests"  # digest's job.toml names it
    assert "HARNESS_GIT_TOKEN" not in env and "SLACK_BOT_TOKEN" not in env  # stdout only: no Slack


def _scheduled(tmp_path, monkeypatch, **options):
    """A runner over two jobs due at 08:00 Anchorage time, one manual, with runs faked."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from harness import job
    from orchestrator.jobs import JobRunner

    root = tmp_path / "jobs"
    for name, trigger in (("watcher", "0 8 * * *"), ("digest", "0 8 * * *"), ("adhoc", "manual")):
        (root / name).mkdir(parents=True, exist_ok=True)  # again, for a restart
        (root / name / "prompt.md").write_text("do it")
        (root / name / "job.toml").write_text(f'[job]\ntrigger = "{trigger}"\n')
    runner = JobRunner([root], schedule=True, zone=ZoneInfo("America/Anchorage"),
                       catch_up=options.pop("catch_up", timedelta(hours=1)), **options)
    started = []

    def run(name, *, scheduled_for=None):
        started.append((name, scheduled_for))
        cfg = job.config(job.load(runner.discover()[name]))
        run_id = f"20261025T{16 + len(started):02d}0000Z-abcd"
        job._write_run(job.runs_dir(cfg, name) / run_id, {"id": run_id, "job": name, "state": "reported",
                                                           "started": f"2026-10-25T{16 + len(started):02d}:00:00Z"})
        return run_id

    monkeypatch.setattr(runner, "run", run)
    return runner, started


def _at(text):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo("America/Anchorage"))


def test_a_due_job_runs_once_and_a_restart_does_not_repeat_it(tmp_path, monkeypatch):
    runner, started = _scheduled(tmp_path, monkeypatch)
    assert runner.tick(_at("2026-10-25T07:59")) == [] and started == []
    done = runner.tick(_at("2026-10-25T08:00"))  # 16:00Z
    assert sorted(name for name, _ in started) == ["digest", "watcher"]
    assert all(due == _at("2026-10-25T08:00") for _, due in started) and len(done) == 2
    runner.tick(_at("2026-10-25T08:00:30"))
    assert len(started) == 2  # not again
    # A new orchestrator (a restart) finds the runs in the run log, and does not run them again.
    again, started_again = _scheduled(tmp_path, monkeypatch)
    again.tick(_at("2026-10-25T08:20"))
    assert started_again == []


def test_a_missed_time_runs_late_within_the_catch_up_and_not_after(tmp_path, monkeypatch):
    from datetime import timedelta

    runner, started = _scheduled(tmp_path, monkeypatch, catch_up=timedelta(minutes=30))
    runner.tick(_at("2026-10-25T08:45"))
    assert started == []  # missed by more than 30m: skipped, not made up
    runner.tick(_at("2026-10-26T08:29"))
    assert len(started) == 2 and started[0][1] == _at("2026-10-26T08:00")


def test_paused_jobs_are_not_run_and_new_ones_can_start_paused(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    runner, started = _scheduled(tmp_path, monkeypatch, start_paused=True)
    runner.tick(_at("2026-10-25T08:00"))
    assert started == []
    summary = {j["name"]: j for j in runner.jobs()}
    schedule = summary["watcher"]["schedule"]
    nxt = datetime.fromisoformat(schedule["next"]).astimezone(ZoneInfo("America/Anchorage"))
    assert schedule["on"] and schedule["paused"] and (nxt.hour, nxt.minute) == (8, 0)
    assert "schedule" not in summary["adhoc"]
    runner.pause("watcher", False)
    runner.tick(_at("2026-10-25T08:01"))
    assert started == [("watcher", _at("2026-10-25T08:00"))]
    runner.pause("watcher", True)
    runner.tick(_at("2026-10-26T08:00"))
    assert len(started) == 1
    with pytest.raises(JobsError, match="manual"):
        runner.pause("adhoc", True)


def test_a_due_job_that_cannot_run_is_tried_once_per_time(tmp_path, monkeypatch):
    runner, _ = _scheduled(tmp_path, monkeypatch)
    calls = []

    def refuse(name, *, scheduled_for=None):
        calls.append(name)
        raise JobsError(f"{name} is already running.")

    monkeypatch.setattr(runner, "run", refuse)
    assert runner.tick(_at("2026-10-25T08:00")) == [
        "digest: due 2026-10-25T16:00:00Z, not run: digest is already running.",
        "watcher: due 2026-10-25T16:00:00Z, not run: watcher is already running."]
    runner.tick(_at("2026-10-25T08:00:30"))
    assert len(calls) == 2


def test_a_scheduled_run_says_so_in_its_environment(jobs_client, monkeypatch):
    from datetime import UTC, datetime

    from orchestrator import launch as launch_module

    started = []

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            started.append(kwargs["env"])

        def wait(self):
            pass

    monkeypatch.setattr(launch_module.subprocess, "Popen", FakeProcess)
    jobs_client.runner.run("watcher", scheduled_for=datetime(2026, 10, 25, 16, tzinfo=UTC))
    assert started[0][jobs_client.job.TRIGGER_ENV] == "schedule"
    assert started[0][jobs_client.job.SCHEDULED_FOR_ENV] == "2026-10-25T16:00:00Z"
    jobs_client.runner.running.clear()
    jobs_client.runner.run("watcher")
    assert started[1][jobs_client.job.TRIGGER_ENV] == "manual" and jobs_client.job.SCHEDULED_FOR_ENV not in started[1]


def test_pause_and_resume_over_the_api(tmp_path, monkeypatch):
    runner, _ = _scheduled(tmp_path, monkeypatch)
    test = TestClient(create_app(FakeBackend(), runner), base_url="http://127.0.0.1:8700")
    assert test.get("/api/jobs").json()["schedule"] == {"on": True, "timezone": "America/Anchorage"}
    assert test.post("/api/jobs/watcher/pause", headers=CHANGE).json() == {"name": "watcher", "paused": True}
    assert test.get("/api/jobs/watcher").json()["schedule"]["paused"] is True
    assert test.post("/api/jobs/watcher/resume", headers=CHANGE).json()["paused"] is False
    assert test.post("/api/jobs/adhoc/pause", headers=CHANGE).status_code == 400


def test_without_jobs_the_jobs_api_says_they_are_not_on(client):
    assert client.get("/api/jobs").json() == {"directories": [], "sources": [], "jobs": []}
    assert "not on" in client.get("/api/jobs/x").json()["error"]


# --- sessions on a cluster ---


class FakeGateway:
    def __init__(self):
        self.kept = {"session-demo": [
            {"id": "g-1", "kind": "connect", "verdict": "deny", "host": "pypi.org", "port": 443, "ts": "t1"},
            {"id": "g-2", "kind": "connect", "verdict": "deny", "host": "pypi.org", "port": 443, "ts": "t2"},
            {"id": "g-3", "kind": "connect", "verdict": "deny", "host": "llm.lan", "port": 8080, "ts": "t3"},
            {"id": "g-4", "kind": "request", "verdict": "deny", "host": "-", "ts": "t4"},
            {"id": "g-5", "kind": "connect", "verdict": "allow", "host": "github.com", "port": 443, "ts": "t5"},
        ]}
        self.added = {}

    def _known(self, claim):
        if claim not in self.kept:
            raise GatewayError(f"gateway GET /sandboxes/{claim}/events: 404 not_registered")

    def events(self, claim, after=0):
        self._known(claim)
        return {"events": self.kept[claim][after:], "next": len(self.kept[claim]), "dropped": 0}

    def approvals(self, claim):
        self._known(claim)
        return {"approvals": self.added.get(claim, []), "spent": []}

    def approve(self, claim, entry):
        self._known(claim)
        self.added.setdefault(claim, []).append(entry)


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    from harness import cluster_session, session
    from harness.config import HarnessConfig
    from orchestrator.backend import ClusterBackend

    base = HarnessConfig(target="agent-sandbox", namespace="ns", pool="p", gateway_control="http://gw:8081",
                         state_dir=str(tmp_path))
    session.save_record(base, "demo", {"name": "demo", "allow": []})
    session.save_record(base, "idle", {"name": "idle", "allow": []})
    gateway = FakeGateway()
    backend = ClusterBackend(base)
    monkeypatch.setattr(backend, "gateway", lambda: gateway)
    monkeypatch.setattr(cluster_session, "load_record", session.load_record)  # records' storage: tested there
    monkeypatch.setattr(cluster_session, "add_allow", session.add_allow)
    monkeypatch.setattr(cluster_session, "list_sessions",
                        lambda cfg: [{"name": "demo", "status": "running"}, {"name": "idle", "status": "stopped"}])
    backend.POLL = 0
    return backend, gateway, base


def test_a_cluster_sessions_refusals_come_from_the_shared_gateway(cluster):
    backend, gateway, _ = cluster
    assert backend.pending("demo") == [
        {"id": "g-2", "destination": "pypi.org", "times": 2, "last": "t2", "sandbox": "session-demo"},
        {"id": "g-3", "destination": "llm.lan:8080", "times": 1, "last": "t3", "sandbox": "session-demo"},
    ]
    assert backend.pending("idle") == []  # not registered: stopped
    gateway.added["session-demo"] = [{"allow": "pypi.org"}]
    assert [row["destination"] for row in backend.pending("demo")] == ["llm.lan:8080"]
    gateway.added["session-demo"] = [{"allow": "llm.lan:8080", "id": "g-3", "once": True}]
    assert [row["destination"] for row in backend.pending("demo")] == ["pypi.org"]
    gateway.kept["session-demo"].append({"id": "g-4", "ts": "t4", "kind": "connect", "verdict": "deny",
                                         "host": "llm.lan", "port": 8080})  # the once was spent
    assert [(row["id"], row["times"]) for row in backend.pending("demo")] == [("g-2", 2), ("g-4", 2)]


def test_approving_a_cluster_sessions_refusal(cluster):
    from harness import session

    backend, gateway, base = cluster
    assert backend.approve("g-3", "once") == "Approved llm.lan:8080 for session demo (once)."
    assert gateway.added["session-demo"] == [{"allow": "llm.lan:8080", "id": "g-3", "once": True}]
    assert session.load_record(base, "demo")["allow"] == []
    assert backend.approve("g-1", "permanent") == "Approved pypi.org for session demo, from now on."
    assert session.load_record(base, "demo")["allow"] == ["pypi.org"]  # for its next start too
    with pytest.raises(SpecError, match="No running session has a refusal with id g-9"):
        backend.approve("g-9", "session")
    with pytest.raises(SpecError, match="g-4"):  # refused, but not for where it was going
        backend.approve("g-4", "session")


def test_a_signed_in_cluster_session_shows_no_password_or_token(cluster):
    from harness import session

    backend, _, base = cluster
    record = {"name": "demo", "allow": [], "agent": "opencode", "password": "pw", "link": "",
              "hosts": {"session-demo.example.com": 4096, "session-demo-term.example.com": 8083},
              "terminal": {"token": "tok", "path": "shell"}}
    session.save_record(base, "demo", record)
    (row,) = [r for r in backend.sessions() if r["name"] == "demo"]
    assert (row["password"], row["token"]) == ("pw", "tok")
    session.save_record(base, "demo", {**record, "sign_in": True})
    (row,) = [r for r in backend.sessions() if r["name"] == "demo"]
    assert (row["password"], row["token"]) == ("", "")
    assert row["urls"]["terminal"] == "https://session-demo-term.example.com/shell"


def test_a_cluster_sessions_events_follow_the_gateway(cluster):
    import asyncio

    backend, gateway, _ = cluster

    async def take(count):
        seen, stream = [], backend.events("demo")
        async for line in stream:
            seen.append(json.loads(line)["id"])
            if len(seen) == 2:
                gateway.kept["session-demo"].append({"id": "g-6", "kind": "connect", "verdict": "allow"})
            if len(seen) == 6:  # registered afresh: counted from 0 again
                gateway.kept["session-demo"] = [{"id": "r-1", "kind": "sandbox", "verdict": "register"}]
            if len(seen) == count:
                await stream.aclose()
                return seen

    assert asyncio.run(take(7)) == ["g-1", "g-2", "g-3", "g-4", "g-5", "g-6", "r-1"]


def test_a_cluster_backend_needs_a_cluster_target(tmp_path):
    from harness.config import HarnessConfig
    from orchestrator.backend import ClusterBackend

    with pytest.raises(ValueError, match="agent-sandbox"):
        ClusterBackend(HarnessConfig(state_dir=str(tmp_path)))
