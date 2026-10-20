import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "examples/jobs/repo-digest/scripts/renovate_prs.py"
spec = importlib.util.spec_from_file_location("renovate_prs", SCRIPT)
renovate_prs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renovate_prs)


@pytest.mark.parametrize("title,labels,expected", [
    ("fix(container): update image ghcr.io/paperless-ngx/paperless-ngx (3.3.2 ➔ 3.3.3)", [], "patch"),
    ("feat(helm): update chart rook-ceph (v1.20.8 ➔ v1.21.0)", [], "minor"),
    ("feat(container)!: update image tdarr (2.91 ➔ 3.01)", [], "major"),
    ("chore(renovate-config)!: Update config presets", [], "major"),
    ("fix(container): update image itzg/minecraft-server (2a23f74 ➔ 0ce9423)", [], "digest"),
    ("Update dependency foo to v2", ["type/major"], "major"),
    ("Update dependency foo (patch)", [], "patch"),
    ("chore(deps): lock file maintenance", [], "lockfile"),
    ("Something else", [], "unknown"),
])
def test_kind(title, labels, expected):
    assert renovate_prs.kind(title, labels) == expected


def test_bucket_sorts_what_needs_no_judgement():
    assert renovate_prs.bucket("patch", "failing", 0) == "blocked: CI failing"
    assert renovate_prs.bucket("minor", "pending", 2) == "blocked: CI pending over a day"
    assert renovate_prs.bucket("major", "green", 0) == "check: major update"
    assert renovate_prs.bucket("minor", "none", 0) == "check: no CI result"
    assert renovate_prs.bucket("minor", "green", 30) == "review"  # age alone blocks nothing


WATCHER = Path(__file__).parent.parent / "examples/jobs/cluster-watcher/scripts/cluster_state.py"
_watcher_spec = importlib.util.spec_from_file_location("cluster_state", WATCHER)
cluster_state = importlib.util.module_from_spec(_watcher_spec)
_watcher_spec.loader.exec_module(cluster_state)


def _pod(name, phase, *, job="", restarts=0, finished=None, waiting=None, ready=True):
    status = {"restartCount": restarts, "ready": ready, "name": "c", "state": {}, "lastState": {}}
    if finished:
        status["lastState"] = {"terminated": {"finishedAt": finished}}
    if waiting:
        status["state"] = {"waiting": {"reason": waiting}}
    meta = {"namespace": "ns", "name": name, "creationTimestamp": "2026-10-01T00:00:00Z",
            "labels": {"job-name": job} if job else {}}
    return {"metadata": meta, "status": {"phase": phase, "containerStatuses": [status]}}


def test_pods_groups_failed_jobs_and_ignores_old_restarts(monkeypatch):
    recent = cluster_state.NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    items = [
        _pod("ok", "Running"),
        _pod("old-restarts", "Running", restarts=300, finished="2026-01-01T00:00:00Z"),
        _pod("flapping", "Running", restarts=4, finished=recent),
        _pod("crash", "Running", waiting="CrashLoopBackOff", ready=False),
        _pod("bootstrap-29869620-a", "Failed", job="bootstrap-29869620"),
        _pod("bootstrap-29869621-b", "Failed", job="bootstrap-29869621"),
        _pod("done", "Succeeded"),
    ]
    monkeypatch.setattr(cluster_state, "get", lambda api, path: {"items": items})
    found = cluster_state.pods("http://api")
    assert [p["pod"] for p in found["items"]] == ["ns/crash", "ns/flapping"]
    assert found["failed_jobs"] == [{"job": "ns/bootstrap", "failed_pods": 2, "latest": found["failed_jobs"][0]["latest"]}]
