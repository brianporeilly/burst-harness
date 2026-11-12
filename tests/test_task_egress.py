"""A task's network: its own egress mode and extra hosts, what the gateway refused its bursts, and letting it
through, for the running burst, the task, or every task."""

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from harness import containers, egress
from harness.config import HarnessConfig
from harness.exec_sandbox import CommandSandbox
from harness.exec_target import DockerTarget
from orchestrator import tasks as tasks_module
from orchestrator.app import create_app
from orchestrator.tasks import TasksError
from orchestrator.tokens import TokenStore

from .test_orchestrator import CHANGE, FakeBackend
from .test_tasks import _runner, _settled, harness  # noqa: F401 (harness: a fixture)


def _refused(host, ident, port=443):
    return {"ts": "2026-11-12T10:00:00Z", "kind": "connect", "verdict": "deny", "host": host, "port": port,
            "id": ident}


def test_a_task_keeps_its_own_network_for_every_burst(tmp_path, harness):  # noqa: F811
    runner = _runner(tmp_path)
    made = runner.create({"repo": "me/proj", "prompt": "x", "slug": "t", "egress": "log",
                          "allow": "docs.rs, *.Example.org docs.rs"})
    assert (made["egress"], made["allow"]) == ("log", ["docs.rs", "*.example.org"])
    argv = harness.argv()
    # After the orchestrator's own --egress for a local target, so the task's wins.
    assert argv[len(argv) - argv[::-1].index("--egress")] == "log"
    assert argv[argv.index("--egress-add") + 1] == "docs.rs,*.example.org"
    harness.started[0][1].finish(0, state="paused")
    _settled(runner, "t", "paused")
    runner.resume("t", "go on")
    assert "--egress-add" in harness.argv() and "log" in harness.argv()
    for bad, said in (({"egress": "open"}, "allowlist, or log"), ({"allow": ["not a host!"]}, "not a host")):
        with pytest.raises(TasksError, match=said):
            runner.create({"repo": "me/proj", "prompt": "x", "slug": "u", **bad})


def test_the_harness_adds_a_tasks_hosts_to_the_allowlist():
    cfg = HarnessConfig(egress_allow=("github.com",), egress_extra=("docs.rs",))
    assert containers.allowlist(cfg)[:2] == ["github.com", "docs.rs"]


def _burst(runner, harness, tmp_path, slug="t"):  # noqa: F811
    """A task whose burst has said which sandbox it is in, and where its gateway's events go."""
    runner.create({"repo": "me/proj", "prompt": "x", "slug": slug})
    events = tmp_path / "egress" / "pool-1.jsonl"
    with open(harness.started[-1][1].output, "a") as handle:
        handle.write("HARNESS_SANDBOX " + json.dumps({"claim": "pool-1", "mode": "allowlist",
                                                     "events": str(events)}) + "\n")
    return events


def test_what_an_ended_burst_was_refused_is_read_from_its_events(tmp_path, harness):  # noqa: F811
    runner = _runner(tmp_path)
    events = _burst(runner, harness, tmp_path)
    events.parent.mkdir(parents=True)
    events.write_text("".join(json.dumps(e) + "\n" for e in (
        _refused("docs.rs", "a-1"), _refused("docs.rs", "a-2"), _refused("static.rust-lang.org", "a-3"),
        {"ts": "x", "kind": "connect", "verdict": "allow", "host": "crates.io", "port": 443})))
    harness.started[0][1].finish(0, state="paused")
    _settled(runner, "t", "paused")
    assert runner.task("t")["bursts"][-1]["egress"]["claim"] == "pool-1"  # kept with the burst

    seen = runner.refusals("t")
    assert not seen["running"] and seen["sandbox"] == "pool-1"
    assert [(r["destination"], r["times"]) for r in seen["refusals"]] == [("docs.rs", 2), ("static.rust-lang.org", 1)]

    assert "bursts reach docs.rs from now on" in runner.allow("t", "docs.rs", "task")["message"]
    assert runner.task("t")["allow"] == ["docs.rs"]
    assert [r["destination"] for r in runner.refusals("t")["refusals"]] == ["static.rust-lang.org"]
    with pytest.raises(TasksError, match="No burst of t is running"):
        runner.allow("t", "static.rust-lang.org", "now")
    with pytest.raises(TasksError, match="not an API token"):
        runner.allow("t", "static.rust-lang.org", "everywhere", everywhere_ok=False)
    runner.allow("t", "static.rust-lang.org", "everywhere")
    assert "static.rust-lang.org" in containers.approved(runner.base)  # every task's and job's from now on
    with pytest.raises(TasksError, match="not a host"):
        runner.allow("t", "a b", "task")


class FakeGateway:
    def __init__(self, events):
        self.kept, self.approved = events, []

    def events(self, name, after=0):
        return {"events": self.kept}

    def approvals(self, name):
        return {"approvals": [entry for _, entry in self.approved]}

    def approve(self, name, entry):
        self.approved.append((name, entry))
        return {}


def test_a_running_bursts_refusals_come_from_its_gateway_and_are_let_through_there(tmp_path, harness,  # noqa: F811
                                                                                     monkeypatch):
    runner = _runner(tmp_path)
    gateway = FakeGateway([_refused("docs.rs", "b-1"), _refused("api.github.com", "b-2")])
    monkeypatch.setattr(tasks_module.Gateway, "from_config", staticmethod(lambda cfg: gateway))
    _burst(runner, harness, tmp_path)
    seen = runner.refusals("t")
    assert seen["running"] and [r["destination"] for r in seen["refusals"]] == ["docs.rs", "api.github.com"]

    assert runner.allow("t", "docs.rs", "now")["message"] == "The running burst reaches docs.rs now."
    assert gateway.approved == [("pool-1", {"allow": "docs.rs"})] and runner.task("t")["allow"] == []
    assert [r["destination"] for r in runner.refusals("t")["refusals"]] == ["api.github.com"]  # approved there
    said = runner.allow("t", "api.github.com", "task")["message"]
    assert "from now on" in said and "reaches api.github.com now" in said and runner.task("t")["allow"] == [
        "api.github.com"]
    harness.started[0][1].finish(0, state="paused")
    _settled(runner, "t", "paused")


def test_the_api_lets_a_token_allow_for_a_task_but_not_everywhere(tmp_path, harness):  # noqa: F811
    runner = _runner(tmp_path)
    _burst(runner, harness, tmp_path)
    harness.started[0][1].finish(0, state="paused")
    _settled(runner, "t", "paused")
    tokens = TokenStore(tmp_path / "tokens.json")
    app = create_app(FakeBackend(), tasks=runner, tokens=tokens)
    _, token = tokens.create("ci")
    api = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": f"Bearer {token}"})
    assert api.get("/api/tasks/t/refusals").json()["sandbox"] == "pool-1"
    assert api.post("/api/tasks/t/allow", json={"host": "docs.rs", "scope": "task"}).status_code == 200
    refused = api.post("/api/tasks/t/allow", json={"host": "docs.rs", "scope": "everywhere"})
    assert refused.status_code == 400 and "not an API token" in refused.json()["error"]
    browser = TestClient(app, base_url="http://127.0.0.1")
    assert browser.post("/api/tasks/t/allow", json={"host": "docs.rs", "scope": "everywhere"},
                        headers=CHANGE).status_code == 200
    assert egress.HOST.fullmatch("docs.rs") and "docs.rs" in containers.approved(runner.base)


def test_a_sandbox_gives_the_agents_commands_its_targets_environment():
    target = DockerTarget(repo="/workspace/repo", container="c", binary="podman",
                          env=(("CARGO_HOME", "/cache/cargo"),))
    sandbox = target.sandbox()
    assert sandbox.env == {"CARGO_HOME": "/cache/cargo"}
    assert sandbox._shell("cargo build", cwd=None) == "cd /workspace/repo && export CARGO_HOME=/cache/cargo; cargo build"
    shell = CommandSandbox(["podman", "exec", "c", "--"], env={"A": "1"})._shell("x", cwd=None, env={"A": "2", "B": "3"})
    assert shell == "export A=2; export B=3; x"  # a command's own over the sandbox's


def test_build_cache_points_cargo_at_the_shared_volume_when_it_is_there():
    from harness.cli import _build_cache
    from harness.exec_target import CommandResult

    class Target:
        def __init__(self, code):
            self.code, self.ran = code, []

        def run_shell(self, command, **kwargs):
            self.ran.append(command)
            return CommandResult(self.code, "", "")

    there = Target(0)
    assert _build_cache(there, "/cache", "github.com_me_proj") == (
        ("CARGO_HOME", "/cache/cargo"), ("CARGO_TARGET_DIR", "/cache/target/github.com_me_proj"))
    assert there.ran == ["test -d /cache && mkdir -p /cache/cargo /cache/target/github.com_me_proj"]
    assert _build_cache(Target(1), "/cache", "k") == ()  # no volume there: builds as before
