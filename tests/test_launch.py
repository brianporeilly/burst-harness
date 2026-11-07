"""Bursts as Kubernetes Jobs (orchestrator/launch.py), against a fake cluster: the Job made, its credentials
handed over once, followed to its end, stopped, and followed again by a restarted orchestrator."""

import json
import os
import threading
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from harness.config import HarnessConfig
from orchestrator import launch as launch_module
from orchestrator.app import create_app
from orchestrator.launch import HANDOFF_PATH, Jobs, LaunchError
from orchestrator.tasks import TaskRunner

from .test_orchestrator import FakeBackend
from .test_tasks import _settled


class FakeCluster:
    """Jobs as kubectl would show them: created, then ended by the test, or deleted."""

    def __init__(self):
        self.jobs, self.calls, self.fail = {}, [], None

    def __call__(self, *args, stdin=None):
        self.calls.append(args)
        if self.fail:
            raise LaunchError(self.fail)
        if args[:3] == ("create", "-f", "-"):
            manifest = json.loads(stdin)
            self.jobs[manifest["metadata"]["name"]] = {"manifest": manifest, "status": {}, "exit": None}
            return ""
        if args[:2] == ("get", "job"):
            job = self.jobs.get(args[2])
            return json.dumps({"metadata": {"name": args[2]}, "status": job["status"]}) if job else ""
        if args[:2] == ("get", "pods"):
            job = self.jobs.get(args[3].removeprefix("job-name="))
            terminated = {"state": {"terminated": {"exitCode": job["exit"]}}} if job and job["exit"] is not None \
                else {"state": {"running": {}}}
            return json.dumps({"items": [{"status": {"containerStatuses": [terminated]}}] if job else []})
        if args[:2] == ("delete", "job"):
            self.jobs.pop(args[2], None)
            return ""
        raise AssertionError(f"unexpected kubectl {args}")

    def only(self):
        (name,) = self.jobs
        return name, self.jobs[name]

    def end(self, name, code, output, **result):
        """The burst's pod writes its output and exits."""
        with open(output, "a") as handle:
            handle.write("working...\n" + (f"HARNESS_RESULT {json.dumps(result)}\n" if result else ""))
        self.jobs[name].update(exit=code, status={"succeeded": 1} if code == 0 else {"failed": 1})


@pytest.fixture
def cluster(monkeypatch):
    fake = FakeCluster()
    monkeypatch.setattr(Jobs, "_kubectl", lambda self, *args, stdin=None: fake(*args, stdin=stdin))
    monkeypatch.setattr("orchestrator.tasks.git.default_branch", lambda url: "main")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("HARNESS_NAMESPACE", "agent-sandbox-system")  # the orchestrator's settings go to the burst
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")  # the orchestrator's pod's, not for a burst
    return fake


def _jobs(tmp_path):
    base = replace(HarnessConfig.load(None, {}), state_dir=str(tmp_path / "state"), target="agent-sandbox",
                   provider="anthropic", engine="strands", branch_prefix="harness/")
    return Jobs(base, namespace="agent-runner", image="ghcr.io/example/burst-harness:1.2.3@sha256:abc",
                volume="burst-state", handoff_url="http://10.1.2.3:8700" + HANDOFF_PATH, poll=0.02)


def _runner(tmp_path, jobs=None):
    jobs = jobs or _jobs(tmp_path)
    return TaskRunner(jobs.cfg, launcher=jobs)


def _output(runner, slug):
    return runner.root / slug / "output.log"


def test_a_burst_is_a_job_with_no_credentials_in_it_only_a_token_for_them(tmp_path, cluster):
    runner = _runner(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "Fix it", "slug": "fix"})
    name, job = cluster.only()
    assert name.startswith("burst-fix-1-")
    spec = job["manifest"]["spec"]
    assert spec["backoffLimit"] == 0 and job["manifest"]["metadata"]["labels"]["burst-harness/task"] == "fix"
    pod = spec["template"]["spec"]
    assert pod["serviceAccountName"] == "burst-task" and pod["terminationGracePeriodSeconds"] == launch_module.CANCEL_GRACE
    (container,) = pod["containers"]
    assert container["image"] == "ghcr.io/example/burst-harness:1.2.3@sha256:abc"
    command = container["command"]
    assert command[1:4] == ["-m", "orchestrator.burst", str(_output(runner, "fix"))] and command[4] == "--"
    assert command[6:8] == ["-m", "harness"] and command[-4:] == ["run", "fix", "--task", "Fix it"]
    # The state, at the path the orchestrator has it, from the shared volume.
    assert {"name": "state", "mountPath": str(tmp_path / "state")} in container["volumeMounts"]
    assert {"name": "state", "persistentVolumeClaim": {"claimName": "burst-state"}} in pod["volumes"]

    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["HARNESS_NAMESPACE"] == "agent-sandbox-system" and env["PYTHONUNBUFFERED"] == "1"
    assert "ANTHROPIC_API_KEY" not in env and "KUBERNETES_SERVICE_HOST" not in env
    assert not any("sk-test-not-real" in value for value in env.values())
    assert env["BURST_HANDOFF_URL"] == "http://10.1.2.3:8700" + HANDOFF_PATH
    # The token is good once, for what the task is granted.
    token = env["BURST_HANDOFF_TOKEN"]
    assert runner.handoff(token)["ANTHROPIC_API_KEY"] == "sk-test-not-real"
    assert runner.handoff(token) is None


def test_the_pod_trades_its_token_at_the_orchestrator_once(tmp_path, cluster):
    runner = _runner(tmp_path)
    client = TestClient(create_app(FakeBackend(), tasks=runner, hosts=("orchestrator.lan",), user_header="Remote-User"),
                        base_url="http://10.1.2.3:8700")
    token = runner.launcher.handoffs.offer({"ANTHROPIC_API_KEY": "sk-test-not-real"})
    # From the pod: no signed-in user, not the orchestrator's host name, and no change header.
    assert client.post(HANDOFF_PATH, headers={"Authorization": f"Bearer {token}"}).json() == \
        {"env": {"ANTHROPIC_API_KEY": "sk-test-not-real"}}
    assert client.post(HANDOFF_PATH, headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert client.post(HANDOFF_PATH).status_code == 401
    # The way past the sign-in is that one path: the rest still asks for it.
    assert client.get("/api/tasks").status_code == 403


def test_expired_tokens_are_not_taken():
    handoffs = launch_module.Handoffs(ttl=-1)
    assert handoffs.take(handoffs.offer({"A_TOKEN": "x"})) is None


def test_a_job_is_followed_to_its_end_and_read_like_a_subprocess(tmp_path, cluster):
    runner = _runner(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "a"})
    assert runner.task("a")["state"] == "running" and "job" in json.loads((runner.root / "a" / "task.json").read_text())
    name, _ = cluster.only()
    cluster.end(name, 0, _output(runner, "a"), state="paused", said={"summary": "s"})
    record = _settled(runner, "a", "paused")
    assert record["bursts"][-1]["exit"] == 0 and "job" not in json.loads((runner.root / "a" / "task.json").read_text())


def test_stop_deletes_the_job_whose_pod_then_has_its_grace(tmp_path, cluster):
    runner = _runner(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "a"})
    name, _ = cluster.only()
    runner.cancel("a")
    assert ("delete", "job", name, "--cascade=foreground", "--wait=false", "--ignore-not-found") in cluster.calls
    assert _settled(runner, "a", "cancelled")["bursts"][-1]["exit"] == launch_module.GONE


def _stopped(tmp_path):
    """An orchestrator that starts a burst and then stops (its thread never looks at the Job again)."""
    return _runner(tmp_path, replace(_jobs(tmp_path), poll=3600))


def test_a_restarted_orchestrator_follows_a_job_still_running_instead_of_starting_another(tmp_path, cluster):
    _stopped(tmp_path).create({"repo": "me/proj", "prompt": "x", "slug": "a"})
    name, _ = cluster.only()

    second = _runner(tmp_path)  # the orchestrator again, after a rollout
    assert second.recover() == [f"a: its burst is still there ({name}), followed again"]
    second.dispatch()
    assert list(cluster.jobs) == [name] and second.task("a")["state"] == "running"
    cluster.end(name, 0, _output(second, "a"), state="pr", pr="https://github.com/me/proj/pull/1")
    assert _settled(second, "a", "pr")["pr"] == "https://github.com/me/proj/pull/1"


def test_a_job_that_ended_while_the_orchestrator_was_down_is_read_on_its_return(tmp_path, cluster):
    _stopped(tmp_path).create({"repo": "me/proj", "prompt": "x", "slug": "a"})
    name, _ = cluster.only()
    cluster.end(name, 0, _output(_runner(tmp_path), "a"), state="paused", said={"summary": "s"})
    second = _runner(tmp_path)
    second.recover()
    assert _settled(second, "a", "paused")["said"] == {"summary": "s"}


def test_a_job_gone_while_the_orchestrator_was_down_is_queued_again(tmp_path, cluster):
    _stopped(tmp_path).create({"repo": "me/proj", "prompt": "x", "slug": "a"})
    cluster.jobs.clear()  # its node lost, and the Job with it
    second = _runner(tmp_path)
    assert second.recover() == ["a: cut short by a restart, queued again"]
    assert second.task("a")["state"] == "queued"


def test_a_stop_asked_before_a_restart_is_made_sure_of_after_it(tmp_path, cluster, monkeypatch):
    stopped = _stopped(tmp_path)
    stopped.create({"repo": "me/proj", "prompt": "x", "slug": "a"})
    name, _ = cluster.only()
    monkeypatch.setattr(Jobs, "stop", lambda self, launched: None)  # the delete never made it
    stopped.cancel("a")
    monkeypatch.undo()
    monkeypatch.setattr(Jobs, "_kubectl", lambda self, *args, stdin=None: cluster(*args, stdin=stdin))
    second = _runner(tmp_path)
    second.recover()
    assert _settled(second, "a", "cancelled") and name not in cluster.jobs


def test_a_kubectl_failure_fails_the_burst_and_says_why(tmp_path, cluster):
    runner = _runner(tmp_path)
    cluster.fail = "the server is unreachable"
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "a"})
    assert runner.task("a")["state"] == "failed" and not runner.running
    assert "could not start the burst: the server is unreachable" in _output(runner, "a").read_text()
    # Nor is a Job that kubectl cannot see taken for one that is gone, on a restart.
    record = json.loads((runner.root / "a" / "task.json").read_text())
    with pytest.raises(LaunchError):
        runner.launcher.reattach({**record, "job": "burst-a-1-abcd"})


def test_a_job_s_settings_come_from_the_environment(tmp_path, monkeypatch, cluster):
    base = replace(HarnessConfig.load(None, {}), state_dir=str(tmp_path / "state"), target="agent-sandbox")
    with pytest.raises(LaunchError, match="ORCHESTRATOR_BURST_VOLUME"):
        Jobs.from_env(base, 8700)
    with pytest.raises(LaunchError, match="agent-sandbox"):
        Jobs.from_env(replace(base, target="podman"), 8700)
    monkeypatch.setenv("ORCHESTRATOR_BURST_VOLUME", "burst-state")
    monkeypatch.setenv("ORCHESTRATOR_BURST_NAMESPACE", "agent-runner")
    monkeypatch.setenv("ORCHESTRATOR_BURST_IMAGE", "img@sha256:abc")
    monkeypatch.setenv("ORCHESTRATOR_BURST_HANDOFF_URL", "http://orchestrator:8700" + HANDOFF_PATH)
    monkeypatch.setenv("ORCHESTRATOR_BURST_MEMORY", "3Gi")
    monkeypatch.setenv("ORCHESTRATOR_BURST_DEADLINE", "2h")
    jobs = Jobs.from_env(base, 8700)
    assert (jobs.namespace, jobs.image, jobs.memory, jobs.deadline) == ("agent-runner", "img@sha256:abc", "3Gi", 7200)
    monkeypatch.delenv("ORCHESTRATOR_BURST_IMAGE")
    cluster.fail = None
    calls = []
    monkeypatch.setattr(Jobs, "_kubectl", lambda self, *args, stdin=None: calls.append(args) or "own-image:1")
    assert Jobs.from_env(base, 8700).image == "own-image:1" and calls[0][:2] == ("get", "pod")  # its own pod's


def test_the_wrapper_fetches_its_credentials_and_hands_them_to_the_harness_alone(tmp_path):
    import subprocess
    import sys
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []

    class Orchestrator(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers["Authorization"]))
            body = json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-test-not-real"}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Orchestrator)
    threading.Thread(target=server.handle_request, daemon=True).start()
    env = {"PATH": "/usr/bin:/bin", "BURST_HANDOFF_URL": f"http://127.0.0.1:{server.server_port}{HANDOFF_PATH}",
           "BURST_HANDOFF_TOKEN": "t0ken"}
    show = "import os; print(sorted(k for k in os.environ if k.startswith(('BURST', 'ANTHROPIC'))))"
    burst = subprocess.run([sys.executable, "-m", "orchestrator.burst", str(tmp_path / "out"), "--", sys.executable,
                            "-c", show], env=env, cwd=Path(__file__).resolve().parent.parent, capture_output=True,
                           timeout=30)
    server.server_close()
    assert burst.returncode == 0 and burst.stdout == b"['ANTHROPIC_API_KEY']\n"  # the token is not passed on
    assert seen == [(HANDOFF_PATH, "Bearer t0ken")]


def test_a_wrapper_that_cannot_fetch_its_credentials_does_not_start_the_harness(tmp_path):
    import subprocess
    import sys

    env = {"PATH": "/usr/bin:/bin", "BURST_HANDOFF_URL": "http://127.0.0.1:9" + HANDOFF_PATH,
           "BURST_HANDOFF_TOKEN": "t"}
    burst = subprocess.run([sys.executable, "-m", "orchestrator.burst", str(tmp_path / "out"), "--", sys.executable,
                            "-c", "print('started')"], env=env, cwd=Path(__file__).resolve().parent.parent,
                           capture_output=True, timeout=60)
    assert burst.returncode == 1 and b"started" not in burst.stdout
    assert "could not fetch the burst's credentials" in (tmp_path / "out").read_text()


# --- a job's runs, on the same launcher ---

def _job_dirs(tmp_path):
    """One job as a ConfigMap mounts it: each file a link into ..data, which links to a timestamped directory."""
    watcher = tmp_path / "jobs" / "watcher"
    if watcher.is_dir():
        return watcher.parent  # the same orchestrator's, after a restart
    (watcher / "..2026_11_06_01").mkdir(parents=True)
    (watcher / "..2026_11_06_01" / "prompt.md").write_text("Look at the cluster.")
    (watcher / "..2026_11_06_01" / "job.toml").write_text(
        '[job]\ntrigger = "0 8 * * *"\n[job.secrets.gh]\nenv = "GITHUB_TOKEN"\nhost = "api.github.com"\n')
    (watcher / "..data").symlink_to("..2026_11_06_01")
    for name in ("prompt.md", "job.toml"):
        (watcher / name).symlink_to(f"..data/{name}")
    return tmp_path / "jobs"


def _job_runner(tmp_path, jobs=None):
    from orchestrator.jobs import JobRunner

    return JobRunner([_job_dirs(tmp_path)], launcher=jobs or _jobs(tmp_path))


def _job_settled(runner, running):
    import time

    for _ in range(200):
        if ("watcher" in runner.running) == running:
            return
        time.sleep(0.02)
    raise AssertionError(f"watcher is {'not ' if running else ''}running")


def test_a_job_run_is_a_job_of_its_own_with_the_job_copied_beside_its_record(tmp_path, cluster, monkeypatch):
    from harness import job

    monkeypatch.setenv("GITHUB_TOKEN", "ghp-test-not-real")
    runner = _job_runner(tmp_path)
    run_id = runner.run("watcher")
    name, made = cluster.only()
    assert name.startswith("job-watcher-") and made["manifest"]["metadata"]["labels"]["burst-harness/job"] == "watcher"
    (container,) = made["manifest"]["spec"]["template"]["spec"]["containers"]
    # Run from a copy on the state volume: the pod has no ConfigMap of the orchestrator's.
    run_dir = job.runs_dir(runner._loaded(runner._path("watcher"))[1], "watcher") / run_id
    assert container["command"][-3:] == ["job", "run", str(run_dir / "watcher")]
    assert container["command"][3] == str(run_dir / "output.log")
    assert sorted(p.name for p in (run_dir / "watcher").iterdir()) == ["job.toml", "prompt.md"]
    assert not (run_dir / "watcher" / "job.toml").is_symlink()
    assert job.load(run_dir / "watcher").secrets[0].env == "GITHUB_TOKEN"
    # Its credentials only under a token, as a task's.
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env[job.RUN_ID_ENV] == run_id and "GITHUB_TOKEN" not in env
    # What its job.toml names, and its model's key (the job's model is the default, Anthropic's).
    assert runner.handoff(env["BURST_HANDOFF_TOKEN"]) == {"GITHUB_TOKEN": "ghp-test-not-real",
                                                          "ANTHROPIC_API_KEY": "sk-test-not-real"}
    assert "sk-test-not-real" not in json.dumps(made["manifest"])
    assert json.loads((run_dir / "launch.json").read_text()) == {"job": name}

    assert runner.job("watcher")["running"]
    with pytest.raises(Exception, match="already running"):
        runner.run("watcher")
    cluster.end(name, 0, run_dir / "output.log")
    _job_settled(runner, running=False)


def test_a_restarted_orchestrator_follows_a_job_run_still_under_way(tmp_path, cluster, monkeypatch):
    from harness import job

    monkeypatch.setenv("GITHUB_TOKEN", "ghp-test-not-real")
    first = _job_runner(tmp_path, replace(_jobs(tmp_path), poll=3600))
    run_id = first.run("watcher")
    name, _ = cluster.only()
    cfg = first._loaded(first._path("watcher"))[1]
    # The harness, in the pod, says it is under way, with a pid of its own pod's.
    job._write_run(job.runs_dir(cfg, "watcher") / run_id, {"id": run_id, "job": "watcher", "state": "running",
                                                           "pid": os.getpid(), "started": "2026-11-06T16:00:00Z"})

    second = _job_runner(tmp_path)  # after a rollout, before it has looked
    assert second.job("watcher")["runs"][0]["state"] == "interrupted"
    assert second.recover() == [f"watcher: run {run_id} is still under way ({name}), followed again"]
    assert second.job("watcher")["runs"][0]["state"] == "running" and second.job("watcher")["running"]
    with pytest.raises(Exception, match="already running"):
        second.run("watcher")  # nor would the schedule start another beside it
    cluster.end(name, 0, job.runs_dir(cfg, "watcher") / run_id / "output.log")
    _job_settled(second, running=False)
    assert list(cluster.jobs) == [name]


def test_a_job_run_whose_job_is_gone_is_interrupted_not_followed(tmp_path, cluster, monkeypatch):
    from harness import job

    monkeypatch.setenv("GITHUB_TOKEN", "ghp-test-not-real")
    first = _job_runner(tmp_path, replace(_jobs(tmp_path), poll=3600))
    run_id = first.run("watcher")
    name, _ = cluster.only()
    cfg = first._loaded(first._path("watcher"))[1]
    job._write_run(job.runs_dir(cfg, "watcher") / run_id, {"id": run_id, "job": "watcher", "state": "running"})
    cluster.jobs.clear()  # its node lost
    second = _job_runner(tmp_path)
    assert second.recover() == [f"watcher: run {run_id}'s Job ({name}) is gone, left interrupted"]
    run = second.run_detail("watcher", run_id)
    assert run["state"] == "interrupted" and "Job is gone" in run["reason"]
    assert not second.job("watcher")["running"]


def test_a_job_run_that_cannot_be_made_says_why(tmp_path, cluster, monkeypatch):
    from orchestrator.jobs import JobsError

    monkeypatch.setenv("GITHUB_TOKEN", "ghp-test-not-real")
    runner = _job_runner(tmp_path)
    cluster.fail = "the server is unreachable"
    with pytest.raises(JobsError, match="the server is unreachable"):
        runner.run("watcher")
    assert not runner.running


def test_a_job_runs_pod_trades_its_token_with_tasks_off(tmp_path, cluster):
    runner = _job_runner(tmp_path)
    client = TestClient(create_app(FakeBackend(), runner, hosts=("orchestrator.lan",), user_header="Remote-User"),
                        base_url="http://10.1.2.3:8700")
    token = runner.launcher.handoffs.offer({"GITHUB_TOKEN": "ghp-test-not-real"})
    assert client.post(HANDOFF_PATH, headers={"Authorization": f"Bearer {token}"}).json() == \
        {"env": {"GITHUB_TOKEN": "ghp-test-not-real"}}
