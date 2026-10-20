import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from orchestrator.app import create_app  # noqa: E402
from orchestrator.backend import SessionSpec, SpecError  # noqa: E402

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
    ({"name": "x", "provider": "llamacpp"}, "base URL"),
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


def test_pending_approve_and_events(client):
    assert client.get("/api/sessions/box/pending").json()[0]["destination"] == "example.com"
    assert client.post("/api/approve", json={"id": "a-1", "scope": "permanent"}, headers=CHANGE).json() == {"message": "ok"}
    assert client.backend.approved == [("a-1", "permanent")]
    assert client.post("/api/approve", json={}, headers=CHANGE).status_code == 400
    body = client.get("/api/sessions/box/events").text
    assert 'data: {"kind": "connect"' in body and "event: end" in body
    assert client.get("/api/sessions/Bad%20Name/events").status_code == 400


def test_spec_overrides_only_what_was_chosen():
    spec = SessionSpec.parse({"name": "box", "provider": "llamacpp", "base_url": "http://llm.lan:8080", "model": "qwen"})
    assert spec.overrides() == {"repo_url": "", "egress": "allowlist", "egress_inject": (), "container_root": False,
                                "provider": "llamacpp", "base_url": "http://llm.lan:8080", "model_id": "qwen"}


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
    from orchestrator import jobs as jobs_module

    started = []

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            started.append((argv, kwargs["env"]))

        def wait(self):
            import time
            time.sleep(0.5)

    monkeypatch.setattr(jobs_module.subprocess, "Popen", FakeProcess)
    assert jobs_client.post("/api/jobs/digest/run").status_code == 403  # no X-Orchestrator header
    refused = jobs_client.post("/api/jobs/digest/run", headers=CHANGE)
    assert refused.status_code == 400 and "GITHUB_TOKEN" in refused.json()["error"]
    response = jobs_client.post("/api/jobs/watcher/run", headers=CHANGE)
    assert response.status_code == 202
    run_id = response.json()["run"]
    (argv, env), = started
    assert argv[-3:] == ["job", "run", str(jobs_client.runner.discover()["watcher"])]
    assert env[jobs_client.job.RUN_ID_ENV] == run_id
    assert jobs_client.post("/api/jobs/watcher/run", headers=CHANGE).status_code == 400  # already running
    assert jobs_client.get("/api/jobs/watcher").json()["runs"][0] == {"id": run_id, "job": "watcher", "state": "starting"}


def test_without_job_directories_the_jobs_api_says_how_to_add_them(client):
    assert client.get("/api/jobs").json() == {"directories": [], "jobs": []}
    assert "--jobs" in client.get("/api/jobs/x").json()["error"]
