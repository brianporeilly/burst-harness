"""Warm pools kept on their templates: idle sandboxes out of date are replaced (harness/warm_pool.py)."""

import json
from dataclasses import replace

import pytest

from harness import warm_pool
from harness.agent_sandbox import AgentSandboxError
from harness.config import HarnessConfig
from orchestrator.recycle import Recycler

NEW = "ghcr.io/me/burst-harness-sandbox:0.29.0@sha256:" + "b" * 64
OLD = "ghcr.io/me/burst-harness-sandbox:0.26.4@sha256:" + "a" * 64
LIMITS = {"limits": {"memory": "8Gi", "cpu": "6"}}


def _spec(image, resources=LIMITS):
    return {"podTemplate": {"spec": {"containers": [{"name": "agent", "image": image, "resources": resources}]}}}


def _sandbox(name, image, *, owner="SandboxWarmPool", template="coding-agent", resources=LIMITS):
    return {"metadata": {"name": name, "uid": f"uid-{name}", "resourceVersion": "42",
                         "annotations": {warm_pool.TEMPLATE_REF: template},
                         "ownerReferences": [{"kind": owner, "name": template, "controller": True}]},
            "spec": _spec(image, resources)}


class Kubectl:
    def __init__(self, sandboxes, refuse=()):
        self.sandboxes, self.refuse, self.calls = sandboxes, refuse, []

    def __call__(self, cfg, *args, stdin=None, namespaced=True):
        self.calls.append((args, stdin))
        if args[:2] == ("get", "sandboxes"):
            return json.dumps({"items": self.sandboxes})
        if args[:2] == ("get", "sandboxtemplate"):
            return json.dumps({"spec": {"volumeClaimTemplates": [], **_spec(NEW)}})
        if args[:2] == ("delete", "--raw"):
            if any(args[2].endswith(name) for name in self.refuse):
                raise AgentSandboxError("kubectl delete --raw failed: Conflict")
            return ""
        raise AssertionError(args)


@pytest.fixture
def cfg():
    return replace(HarnessConfig(), namespace="agent-sandbox-system")


def test_only_idle_sandboxes_out_of_date_with_their_template_are_replaced(cfg, monkeypatch):
    fake = Kubectl([
        _sandbox("old", OLD),
        _sandbox("current", NEW),
        _sandbox("claimed", OLD, owner="SandboxClaim"),  # a task's: never touched
        _sandbox("other", OLD, template="someone-elses"),
        _sandbox("small", NEW, resources={"limits": {"memory": "4Gi"}}),
    ])
    monkeypatch.setattr(warm_pool, "_kubectl", fake)
    done = warm_pool.recycle(cfg, ("coding-agent", "coding-agent-root"))
    assert done == [
        "Replaced old, an idle sandbox of coding-agent out of date (image burst-harness-sandbox:0.26.4 -> "
        "burst-harness-sandbox:0.29.0): the pool makes a new one from the template.",
        "Replaced small, an idle sandbox of coding-agent out of date (agent's resources): the pool makes a new one "
        "from the template.",
    ]
    deletes = [(args, json.loads(stdin)) for args, stdin in fake.calls if args[0] == "delete"]
    assert deletes[0][0] == ("delete", "--raw", "/apis/agents.x-k8s.io/v1beta1/namespaces/agent-sandbox-system/"
                             "sandboxes/old", "-f", "-")
    # Only as it was read: one a claim took meanwhile is refused, not deleted.
    assert deletes[0][1]["preconditions"] == {"uid": "uid-old", "resourceVersion": "42"}
    assert sum(1 for args, _ in fake.calls if args[:2] == ("get", "sandboxtemplate")) == 1  # read once


def test_a_sandbox_taken_meanwhile_is_left_and_said(cfg, monkeypatch):
    monkeypatch.setattr(warm_pool, "_kubectl", Kubectl([_sandbox("old", OLD)], refuse=("old",)))
    (line,) = warm_pool.recycle(cfg, ("coding-agent",))
    assert line.startswith("old (coding-agent) is out of date") and "Conflict" in line
    with pytest.raises(AgentSandboxError, match="namespace"):
        warm_pool.recycle(HarnessConfig(), ("coding-agent",))


def test_the_orchestrator_says_a_failure_once_until_it_changes(cfg, monkeypatch):
    def forbidden(cfg, *args, **kwargs):
        raise AgentSandboxError('kubectl get sandboxes failed: sandboxes is forbidden')

    monkeypatch.setattr(warm_pool, "_kubectl", forbidden)
    recycler = Recycler(replace(cfg, pool="coding-agent", root_pool="coding-agent-root"))
    assert recycler.pools == ("coding-agent", "coding-agent-root")
    (line,) = recycler.once()
    assert "forbidden" in line and recycler.once() == []
    monkeypatch.setattr(warm_pool, "_kubectl", Kubectl([_sandbox("current", NEW)]))
    assert recycler.once() == [] and recycler.last_error == ""
