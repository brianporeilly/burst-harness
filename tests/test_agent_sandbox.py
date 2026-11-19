from datetime import datetime, timezone

from harness.agent_sandbox import LABEL_TASK, claim_manifest, label_value
from harness.exec_target import KubectlTarget


def test_claim_manifest():
    m = claim_manifest(
        pool="coding-agent", slug="Fix the/thing!", branch="harness/x",
        repo_url="https://user:tok@github.com/o/r", ttl_seconds=3600,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert m["spec"] == {
        "warmPoolRef": {"name": "coding-agent"},
        "lifecycle": {"shutdownTime": "2026-01-01T01:00:00Z", "shutdownPolicy": "Delete"},
    }
    assert "env" not in m["spec"] and "additionalPodMetadata" not in m["spec"]  # keep claims warm
    assert m["metadata"]["labels"][LABEL_TASK] == "Fix-the-thing"
    assert "tok" not in str(m)


def test_label_value_limits():
    assert len(label_value("a" * 100)) == 63
    assert label_value("--") == "task"


def test_kubectl_target_argv():
    t = KubectlTarget(repo="/workspace/repo", pod="p", namespace="ns")
    assert t._wrap("git status", cwd="/") == ["kubectl", "-n", "ns", "exec", "p", "--", "sh", "-c", "cd / && git status"]
    assert t.exec_prefix(stdin=True, tty=True) == ["kubectl", "-n", "ns", "exec", "-i", "-t", "p", "--"]


def test_docker_target_argv():
    from harness.exec_target import DockerTarget
    t = DockerTarget(repo="/workspace/repo", container="c", binary="podman")
    assert t._wrap("ls") == ["podman", "exec", "c", "sh", "-c", "cd /workspace/repo && ls"]


def test_a_claimed_sandbox_knows_its_deadline(monkeypatch):
    """So the burst's budget can pause before the controller deletes it (budget.py)."""
    import time
    from dataclasses import replace

    from harness import agent_sandbox
    from harness.config import HarnessConfig

    monkeypatch.setattr(agent_sandbox, "create_claim", lambda cfg, **kw: "pool-abc")
    monkeypatch.setattr(agent_sandbox, "wait_ready", lambda cfg, claim: "pod-abc")
    monkeypatch.setattr(agent_sandbox.AgentSandboxTarget, "missing_commands", lambda self, names: [])
    cfg = replace(HarnessConfig(), target="agent-sandbox", pool="pool", claim_ttl=3600, gateway_control="")
    before = time.time()
    target = agent_sandbox.acquire(cfg, repo="/workspace/repo", slug="t", branch="b")
    assert before + 3600 - 1 <= target.deadline <= time.time() + 3600

    monkeypatch.setattr(agent_sandbox, "claim_exists", lambda cfg, claim: True)
    monkeypatch.setattr(agent_sandbox, "renew_claim", lambda cfg, claim: 600)  # a kept one, renewed: 10 minutes
    target = agent_sandbox.acquire(replace(cfg, claim_name="kept"), repo="/workspace/repo", slug="t", branch="b")
    assert time.time() + 590 <= target.deadline <= time.time() + 600
