"""A target that ran out of memory is named as the reason a run ended (ExecTarget.out_of_memory, cli._killed)."""

import json
import sys

from harness.exec_target import DockerTarget, KubectlTarget, LocalTarget, oom_killed

SINCE = 1_800_000_000.0  # 2027-01-15T08:00:00Z
AFTER, BEFORE = "2027-01-15T08:30:00Z", "2027-01-15T07:00:00Z"


def _pod(last=None, now=None, limit="8Gi", name="agent"):
    status = {"name": name, "lastState": {"terminated": last} if last else {}, "state": {"terminated": now} if now else {"running": {}}}
    return {"spec": {"containers": [{"name": name, "resources": {"limits": {"memory": limit}} if limit else {}}]},
            "status": {"containerStatuses": [status]}}


def test_a_pods_container_killed_for_memory_during_the_run_is_named_with_its_limit():
    said = oom_killed(_pod(last={"reason": "OOMKilled", "exitCode": 137, "finishedAt": AFTER}), None, SINCE, "sandbox x")
    assert said.startswith("sandbox x ran out of memory: its container was killed at its 8Gi memory limit (OOMKilled)")
    assert "fewer build jobs" in said
    # Still down, not yet restarted.
    assert "8Gi" in oom_killed(_pod(now={"reason": "OOMKilled", "finishedAt": AFTER}), "agent", SINCE, "x")
    assert "the memory it had" in oom_killed(_pod(last={"reason": "OOMKilled", "finishedAt": AFTER}, limit=None),
                                              None, SINCE, "x")


def test_anything_else_is_not_called_running_out_of_memory():
    assert oom_killed(_pod(last={"reason": "OOMKilled", "finishedAt": BEFORE}), None, SINCE, "x") == ""  # an earlier run's
    assert oom_killed(_pod(last={"reason": "Error", "exitCode": 137, "finishedAt": AFTER}), None, SINCE, "x") == ""
    assert oom_killed(_pod(), None, SINCE, "x") == ""
    assert oom_killed(_pod(last={"reason": "OOMKilled", "finishedAt": AFTER}), "other", SINCE, "x") == ""
    assert oom_killed({}, None, SINCE, "x") == ""


def _fake(tmp_path, name, script):
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\n{script}")
    path.chmod(0o755)
    return str(path)


def test_a_pod_is_asked_with_kubectl(tmp_path):
    pod = _pod(last={"reason": "OOMKilled", "finishedAt": AFTER})
    kubectl = _fake(tmp_path, "kubectl", f"import sys\nassert sys.argv[1:] == ['-n', 'ns', 'get', 'pod', 'p1', '-o', 'json']\n"
                                         f"print({json.dumps(json.dumps(pod))})\n")
    target = KubectlTarget(repo="/w", pod="p1", namespace="ns", kubectl=kubectl)
    assert "8Gi memory limit" in target.out_of_memory(SINCE)
    gone = KubectlTarget(repo="/w", pod="p1", kubectl=_fake(tmp_path, "k2", "import sys\nsys.exit(1)\n"))
    assert gone.out_of_memory(SINCE) == ""


def test_a_container_is_asked_with_inspect(tmp_path):
    podman = _fake(tmp_path, "podman", "import sys\nprint('{\"OOMKilled\": %s}|%s' % (sys.argv[-1] == 'oom' and 'true' or 'false', 8 * 2**30))\n")
    killed = DockerTarget(repo="/w", container="oom", binary=podman).out_of_memory(SINCE)
    assert "ran out of memory: it was killed at its 8Gi memory limit (OOMKilled)" in killed
    assert DockerTarget(repo="/w", container="fine", binary=podman).out_of_memory(SINCE) == ""
    assert LocalTarget(repo="/w").out_of_memory(SINCE) == ""


def test_a_run_that_ends_short_says_its_target_ran_out_of_memory(tmp_path, origin, capsys, monkeypatch):
    from harness import cli

    from . import fake_llama
    from .conftest import git

    asked = []
    monkeypatch.setattr(LocalTarget, "out_of_memory",
                        lambda self, since: asked.append(since) or "the sandbox ran out of memory at 8Gi")
    server, _ = fake_llama.serve([{"text": "I stopped."}] * 4)
    try:
        work = tmp_path / "work"
        git(tmp_path, "clone", "-q", str(origin), str(work))
        args = ["--provider", "llamacpp", "--base-url", f"http://127.0.0.1:{server.server_port}", "--repo", str(work)]
        cli.main([*args, "start", "t1", "--task", "Fix it."])
        out = capsys.readouterr().out
        assert "! the sandbox ran out of memory at 8Gi" in out
        result = json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])
        assert result["said"] == {"reason": "the sandbox ran out of memory at 8Gi"} and len(asked) == 1

        # A run that reached its end is not asked.
        asked.clear()
        finish = {"tool": "finish_with_pr", "args": {"title": "t", "body": "b"}}
        server.shutdown()
        server, _ = fake_llama.serve([finish, {"text": "Done."}])
        other = tmp_path / "other"
        git(tmp_path, "clone", "-q", str(origin), str(other))
        args[3], args[-1] = f"http://127.0.0.1:{server.server_port}", str(other)
        cli.main([*args, "start", "t2", "--task", "Fix it."])
        assert asked == [] and "ran out of memory" not in capsys.readouterr().out
    finally:
        server.shutdown()
