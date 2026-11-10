"""Our own llama.cpp servers, one run at a time and only once idle (orchestrator/servers.py), across jobs and
tasks. No model is called: a fake /health, and runs faked as processes."""

import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("fastapi")

from harness.config import HarnessConfig
from orchestrator import launch as launch_module
from orchestrator.jobs import JobRunner, ServerBusy
from orchestrator.servers import ModelServers, busy_slots, servers_for

from .test_tasks import _runner, _settled, harness  # noqa: F401 -- the fixture

SERVER = "http://127.0.0.1:9"  # nothing listens: no run here reaches a model


def test_a_run_needs_the_llamacpp_servers_of_its_model_and_of_whom_it_may_hand_work_to():
    base = HarnessConfig()
    assert servers_for(replace(base, provider="llamacpp", base_url="http://LLM.lan:8080/v1")) == ["http://llm.lan:8080"]
    assert servers_for(replace(base, provider="anthropic")) == []
    profiles = {"local": {"provider": "llamacpp", "base_url": "http://typhon:8080", "use_for": "x"},
                "claude": {"provider": "anthropic", "use_for": "y"}}
    lead = replace(base, engine="claude-code", provider="llamacpp", base_url="http://unused:1", agents=profiles,
                   delegate_to=("local", "claude"))
    assert servers_for(lead) == ["http://typhon:8080"]  # Claude Code is its own model; its delegate is not
    assert servers_for(replace(lead, engine="strands")) == ["http://unused:1", "http://typhon:8080"]


def test_a_server_is_one_runs_at_a_time_and_only_once_it_is_idle():
    busy = {"http://a:1": 0, "http://b:1": 2}
    servers = ModelServers(wait_for_idle=True, busy=lambda s: busy[s])
    woken = []
    servers.on_release.append(lambda: woken.append(True))
    assert servers.take("job x", ["http://a:1"]) == ""
    assert servers.take("job x", ["http://a:1"]) == ""  # its own again
    assert servers.take("task y", ["http://a:1"]) == "http://a:1 is in use by job x"
    assert servers.take("task y", ["http://b:1"]) == "http://b:1 is busy (2 slots processing)"
    assert servers.status() == {"http://a:1": "job x"}  # nothing taken by a refusal
    servers.give_back("task y")
    assert woken == []  # it had nothing
    servers.give_back("job x")
    assert woken == [True] and servers.status() == {}
    servers.hold("job z", ["http://b:1"])  # followed again after a restart: busy or not, it is ours
    assert servers.take("task y", ["http://b:1"]) == "http://b:1 is in use by job z"
    servers.give_back("job z", wake=False)
    assert woken == [True]
    # Not asked to wait for idle (the default): only our own runs take turns, and /health is not asked.
    assert ModelServers(busy=lambda s: 1 / 0).take("task y", ["http://b:1"]) == ""


def test_busy_is_what_llama_servers_health_says_and_nothing_when_it_says_nothing():
    answers = iter([{"status": "ok", "slots_idle": 1, "slots_processing": 1}, {"status": "ok"}])

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(next(answers)).encode()
            self.send_response(200 if self.path == "/health" else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        assert busy_slots(url) == 1
        assert busy_slots(url) == 0  # not llama.cpp, or an older one
    finally:
        server.shutdown()
        server.server_close()
    assert busy_slots(SERVER) == 0  # unreachable: the run finds out for itself


def _local_jobs(tmp_path, trigger="manual"):
    root = tmp_path / "jobs"
    for name in ("brief", "watcher"):
        (root / name).mkdir(parents=True)
        (root / name / "prompt.md").write_text("do it")
        (root / name / "job.toml").write_text(f'provider = "llamacpp"\nbase_url = "{SERVER}"\n'
                                              f'[job]\ntrigger = "{trigger}"\n')
    return root


class Runs:
    """Stands in for `harness job run` processes, each ended by the test."""

    def __init__(self, bursts=None):
        self.started, self.ends, self.bursts = [], [], bursts  # a task's bursts go to the tasks' fake

    def __call__(self, argv, **kwargs):
        if self.bursts is not None and argv[-3:-1] != ["job", "run"]:
            return self.bursts(argv, **kwargs)
        done = threading.Event()
        self.started.append(argv[-1])
        self.ends.append(done)
        runs = self

        class Process:
            pid = 4242

            def wait(self):
                done.wait(10)
                return 0

            def poll(self):
                return 0 if done.is_set() else None

        runs.last = Process()
        return runs.last


def test_a_job_waits_while_another_run_has_its_server_and_a_task_starts_once_it_is_given_back(
        tmp_path, monkeypatch, harness):  # noqa: F811 -- the fixture
    runs = Runs(bursts=harness)  # one subprocess module: both fakes are behind its Popen
    monkeypatch.setattr(launch_module.subprocess, "Popen", runs)
    servers = ModelServers(busy=lambda s: 0)
    jobs = JobRunner([_local_jobs(tmp_path)], servers=servers)
    tasks = _runner(tmp_path, servers=servers)
    tasks.base = replace(tasks.base, provider="llamacpp", base_url=SERVER)
    servers.on_release.append(tasks.dispatch)

    jobs.run("brief")
    with pytest.raises(ServerBusy, match=r"watcher waits for its model server: http://127.0.0.1:9 is in use by job brief"):
        jobs.run("watcher")
    made = tasks.create({"repo": "me/proj", "prompt": "x"})
    assert made["state"] == "queued" and "in use by job brief" in tasks.task(made["slug"])["server_wait"]
    assert harness.started == []

    runs.ends[0].set()  # the job's run ends: the server is given back, and the queued task takes it
    _settled(tasks, made["slug"], "running")
    assert len(harness.started) == 1 and servers.status() == {SERVER: f"task {made['slug']}"}
    assert not tasks.task(made["slug"]).get("server_wait")
    with pytest.raises(ServerBusy, match="in use by task"):
        jobs.run("watcher")
    harness.started[0][1].finish(0, state="pr", pr="https://github.com/me/proj/pull/1")
    _settled(tasks, made["slug"], "pr")
    assert servers.status() == {}
    jobs.run("watcher")


def test_a_scheduled_run_whose_server_is_busy_is_tried_again_on_each_tick(tmp_path, monkeypatch):
    runs = Runs()
    monkeypatch.setattr(launch_module.subprocess, "Popen", runs)
    busy = {"slots": 1}
    servers = ModelServers(wait_for_idle=True, busy=lambda s: busy["slots"])
    jobs = JobRunner([_local_jobs(tmp_path, trigger="0 8 * * *")], schedule=True, zone=ZoneInfo("America/Anchorage"),
                     catch_up=timedelta(hours=1), servers=servers)
    at = datetime(2026, 11, 10, 17, 5, tzinfo=UTC)  # 08:05 in Anchorage
    first = jobs.tick(at)
    assert all("waiting" in line and "1 slot processing" in line for line in first) and len(first) == 2
    assert jobs.tick(at + timedelta(seconds=30)) == []  # still waiting, not said again
    assert jobs.job("brief")["waiting"].endswith("busy (1 slot processing).")
    busy["slots"] = 0  # the benchmark is done
    started = jobs.tick(at + timedelta(minutes=1))
    assert [line.split(":")[0] for line in started if "started run" in line] == ["brief"]  # one at a time
    assert any(line.startswith("watcher:") and "in use by job brief" in line for line in started)  # a new reason
    assert "in use by job brief" in jobs.job("watcher")["waiting"] and jobs.job("brief")["waiting"] == ""
