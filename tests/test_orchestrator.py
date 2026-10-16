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
