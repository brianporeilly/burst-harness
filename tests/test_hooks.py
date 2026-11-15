"""Webhooks (orchestrator/hooks.py): signed deliveries from a forge, taken as hints to look now."""

import hashlib
import hmac
import json
from dataclasses import replace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from harness import credentials
from harness.config import HarnessConfig
from orchestrator.app import create_app
from orchestrator.hooks import HookError, Hooks
from orchestrator.sources import Source

from .test_orchestrator import FakeBackend
from .test_tasks import _runner, _settled, harness  # noqa: F401 -- harness is a fixture

FORGEJO = "https://forgejo.example.com"
BASE = replace(HarnessConfig(), forges=(f"forgejo={FORGEJO}",))


class Tasks:
    def __init__(self, prs):
        self.prs, self.checked = prs, []

    def by_pr(self, url):
        return [slug for slug, pr in self.prs.items() if pr == url]

    def check_pr(self, slug, fresh):
        self.checked.append((slug, fresh))


class Sources:
    def __init__(self, sources):
        self.found, self.synced = {s.name: s for s in sources}, []

    def sources(self):
        return self.found

    def sync(self, name):
        self.synced.append(name)


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    monkeypatch.setattr(credentials, "_stored", {"GITHUB_WEBHOOK_SECRET": "gh-secret",
                                                 "FORGEJO_WEBHOOK_SECRET": "fj-secret"})


def _signed(secret, payload):
    body = json.dumps(payload).encode()
    return body, hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _github(event, payload, delivery="d1", secret="gh-secret"):
    body, sig = _signed(secret, payload)
    return {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": f"sha256={sig}"}, body


def _forgejo(event, payload, delivery="f1", secret="fj-secret"):
    body, sig = _signed(secret, payload)
    return {"X-Forgejo-Event": event, "X-Forgejo-Delivery": delivery, "X-Forgejo-Signature": sig}, body


def _hooks(**kwargs):
    return Hooks(BASE, background=False, **kwargs)


def test_a_merged_pr_makes_its_task_look_now_and_a_redelivery_does_nothing():
    tasks = Tasks({"fix-a1b2": "https://github.com/me/proj/pull/7", "other": "https://github.com/me/proj/pull/8"})
    hooks = _hooks(tasks=tasks)
    merged = {"action": "closed", "pull_request": {"html_url": "https://github.com/me/proj/pull/7", "merged": True},
              "repository": {"html_url": "https://github.com/me/proj"}}
    assert hooks.receive("github", *_github("pull_request", merged)) == {
        "did": "pull_request closed: looking at the PR of fix-a1b2"}
    assert tasks.checked == [("fix-a1b2", 0)]  # the PR's state is asked of GitHub, not taken from the delivery
    assert hooks.receive("github", *_github("pull_request", merged))["did"] == "nothing: delivered before"
    assert tasks.checked == [("fix-a1b2", 0)]
    opened = {**merged, "action": "opened"}
    assert "nothing to do" in hooks.receive("github", *_github("pull_request", opened, delivery="d2"))["did"]
    assert hooks.receive("github", *_github("ping", {"zen": "x"}, delivery="d3"))["did"] == "ping: nothing to do"
    row = next(f for f in hooks.listing()["forges"] if f["name"] == "github")
    assert [d["event"] for d in row["deliveries"]] == ["ping", "pull_request", "pull_request", "pull_request"]


def test_a_push_to_a_job_repos_branch_syncs_it():
    sources = Sources([Source("mine", f"{FORGEJO}/me/jobs.git", path="jobs"),  # its default branch
                       Source("rel", f"{FORGEJO}/me/jobs.git", branch="release"),
                       Source("gh", "https://github.com/me/jobs.git")])
    hooks = _hooks(sources=sources)
    push = {"ref": "refs/heads/main", "repository": {"html_url": f"{FORGEJO}/me/jobs",
                                                     "clone_url": f"{FORGEJO}/me/jobs.git", "default_branch": "main"}}
    assert hooks.receive("forgejo", *_forgejo("push", push))["did"] == "push to main: syncing job repo mine"
    assert sources.synced == ["mine"]
    assert "no job repo" in hooks.receive("forgejo", *_forgejo("push", {**push, "ref": "refs/heads/dev"}, "f2"))["did"]
    # Gitea's headers do too.
    body, sig = _signed("fj-secret", {**push, "ref": "refs/heads/release"})
    assert "rel" in hooks.receive("forgejo", {"X-Gitea-Event": "push", "X-Gitea-Signature": sig}, body)["did"]
    # A delivery about another host's repo is not this forge's to say.
    elsewhere = {**push, "repository": {**push["repository"], "html_url": "https://github.com/me/jobs"}}
    assert hooks.receive("forgejo", *_forgejo("push", elsewhere, "f4"))["did"].startswith("ignored")
    assert sources.synced == ["mine", "rel"]


def test_unsigned_wrongly_signed_or_secretless_deliveries_are_refused(monkeypatch):
    hooks = _hooks(tasks=Tasks({}))
    headers, body = _github("ping", {}, secret="wrong")
    with pytest.raises(HookError, match="signature") as refused:
        hooks.receive("github", headers, body)
    assert refused.value.status == 401
    with pytest.raises(HookError, match="signature"):  # Forgejo's secret is not GitHub's
        hooks.receive("forgejo", *_forgejo("ping", {}, secret="gh-secret"))
    with pytest.raises(HookError, match="No forge"):
        hooks.receive("gitlab", *_github("ping", {}))
    monkeypatch.setattr(credentials, "_stored", {})
    with pytest.raises(HookError, match="GITHUB_WEBHOOK_SECRET is not set") as unset:
        hooks.receive("github", *_github("ping", {}, delivery="d9"))
    assert unset.value.status == 403
    assert next(f for f in hooks.listing()["forges"] if f["name"] == "github")["deliveries"][0]["outcome"].startswith("refused")


def test_a_hook_needs_no_sign_in_and_the_hooks_port_answers_nothing_else():
    hooks = _hooks(tasks=Tasks({}), port=8701)
    app = create_app(FakeBackend(), hooks=hooks, user_header="Remote-User")
    outside = TestClient(app, base_url="http://burst-orchestrator.svc:8701")  # a forge, on the hooks port
    headers, body = _github("ping", {})
    answered = outside.post("/hooks/github", content=body, headers=headers)
    assert answered.status_code == 202 and answered.json() == {"did": "ping: nothing to do"}
    for path in ("/api/settings/hooks", "/api/tasks", "/"):
        assert outside.get(path, headers={"Remote-User": "brian"}).status_code == 404  # no one to be, here
    assert outside.post("/hooks/github", content=body, headers={**headers, "X-Hub-Signature-256": "sha256=0"}).status_code == 401
    page = TestClient(app, base_url="http://127.0.0.1:8700")
    assert page.get("/api/settings/hooks", headers={"Remote-User": "brian"}).json()["port"] == 8701


def test_tasks_are_found_by_their_pr(tmp_path, harness):  # noqa: F811 -- the fixture
    runner = _runner(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "t"})
    harness.started[0][1].finish(0, state="pr", pr="https://github.com/me/proj/pull/7")
    _settled(runner, "t", "pr")
    assert runner.by_pr("https://github.com/Me/proj/pull/7/") == ["t"] and runner.by_pr("https://github.com/me/proj/pull/8") == []
