"""Profiles set in Settings: kept apart from bursts, added to the configuration's, and given to a task's bursts."""

import json
from dataclasses import replace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from harness.config import HarnessConfig
from orchestrator.app import create_app
from orchestrator.profiles import ProfilesError, ProfileStore
from orchestrator.tasks import TasksError
from orchestrator.tokens import TokenStore

from .test_orchestrator import CHANGE, FakeBackend
from .test_tasks import _runner, _settled, harness  # noqa: F401 (harness: a fixture)

CODER = {"provider": "llamacpp", "base_url": "http://127.0.0.1:9", "model_id": "coder-model",
         "context_window_limit": 65536, "use_for": "Writing code and running tests"}
# One in the orchestrator's config file, as [agents.reviewer] and delegate_to = ["reviewer"].
CONFIGURED = {"reviewer": {"provider": "anthropic", "model_id": "claude-haiku-5-5", "use_for": "Reviewing changes"}}


def _store(tmp_path, agents=None, delegate_to=()):
    base = replace(HarnessConfig(), agents=dict(agents or {}), delegate_to=tuple(delegate_to))
    return ProfileStore(tmp_path / "orchestrator" / "profiles.json", base)


def test_profiles_set_here_go_over_the_configurations_and_removing_one_goes_back(tmp_path):
    store = _store(tmp_path, CONFIGURED, ("reviewer",))
    store.set("coder", CODER, user="me")
    store.set("reviewer", {"provider": "anthropic", "model_id": "claude-sonnet-5-5", "use_for": "Reviewing"})
    assert store.profiles() == {"reviewer": {"provider": "anthropic", "model_id": "claude-sonnet-5-5",
                                             "use_for": "Reviewing"}, "coder": CODER}
    rows = {row["name"]: row for row in store.listing()["profiles"]}
    assert rows["coder"]["source"] == "settings" and rows["coder"]["updated_by"] == "me"
    assert rows["reviewer"]["configuration_too"]
    assert store.remove("reviewer") == "the configuration's"
    assert store.profiles()["reviewer"] == CONFIGURED["reviewer"]
    with pytest.raises(ProfilesError, match="only a config file changes"):
        store.remove("reviewer")
    assert json.loads((tmp_path / "orchestrator" / "profiles.json").read_text())["profiles"]["coder"]["model_id"] \
        == "coder-model"


def test_a_profile_is_a_model_and_what_it_is_good_at_and_nothing_that_runs_on_the_driver(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ProfilesError, match="needs a base URL"):
        store.set("coder", {**CODER, "base_url": ""})
    with pytest.raises(ProfilesError, match="good at"):
        store.set("coder", {**CODER, "use_for": " "})
    with pytest.raises(ProfilesError, match="provider is one of"):
        store.set("coder", {**CODER, "provider": "no-such-provider"})
    with pytest.raises(ProfilesError, match="not a profile name"):
        store.set("Coder!", CODER)
    store.set("coder", {**CODER, "token_command": "cat /etc/shadow", "delegate_to": ["x"], "read_only": True})
    assert store.profiles()["coder"] == {**CODER, "read_only": True}


def test_the_default_delegates_are_the_configurations_until_set_here(tmp_path):
    store = _store(tmp_path, CONFIGURED, ("reviewer",))
    assert store.default() == ("reviewer",) and store.listing()["delegate_to_source"] == "configuration"
    store.set("coder", CODER)
    store.set_default(["coder"])
    assert store.default() == ("coder",)
    with pytest.raises(ProfilesError, match="names no profile: nobody"):
        store.set_default(["coder", "nobody"])
    with pytest.raises(ProfilesError, match="take it out of them first"):
        store.remove("coder")
    store.set_default([])
    assert store.default() == ()  # no one, rather than the configuration's
    store.set_default(None)
    assert store.default() == ("reviewer",)


def test_a_tasks_bursts_are_given_the_profiles_and_its_own_delegates(tmp_path, harness, monkeypatch):  # noqa: F811
    runner = _runner(tmp_path, limit=3)
    runner.profiles = store = _store(tmp_path, CONFIGURED)
    store.set("coder", CODER)
    store.set_default(["coder"])
    made = runner.create({"repo": "me/proj", "prompt": "x", "slug": "lead", "engine": "claude-code"})
    assert made["delegate_to"] == ["coder"]
    env = harness.started[0][1].env
    assert json.loads(env["HARNESS_AGENTS"]) == {**CONFIGURED, "coder": CODER} and env["HARNESS_DELEGATE_TO"] == "coder"
    # Read as the burst's harness reads them, with no config file of the orchestrator's.
    for name in ("HARNESS_AGENTS", "HARNESS_DELEGATE_TO"):
        monkeypatch.setenv(name, env[name])
    cfg = HarnessConfig.load(None, {}, user_config=None)
    assert cfg.delegate_to == ("coder",) and cfg.agents["coder"]["base_url"] == "http://127.0.0.1:9"

    runner.create({"repo": "me/proj", "prompt": "x", "slug": "alone", "delegate_to": []})
    assert harness.started[1][1].env["HARNESS_DELEGATE_TO"] == ""  # no one, whatever the default
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "both", "delegate_to": ["coder", "reviewer"]})
    assert harness.started[2][1].env["HARNESS_DELEGATE_TO"] == "coder,reviewer"
    with pytest.raises(TasksError, match="names no profile: nobody"):
        runner.create({"repo": "me/proj", "prompt": "x", "slug": "bad", "delegate_to": ["nobody"]})


def test_continuing_a_task_keeps_the_delegates_it_was_made_with(tmp_path, harness):  # noqa: F811
    runner = _runner(tmp_path)
    runner.profiles = store = _store(tmp_path)
    store.set("coder", CODER)
    store.set_default(["coder"])
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "t"})
    harness.started[0][1].finish(0, state="paused")
    _settled(runner, "t", "paused")
    store.set_default([])
    runner.resume("t", "go on")
    assert harness.started[1][1].env["HARNESS_DELEGATE_TO"] == "coder"


def test_without_settings_a_task_keeps_the_configurations_delegates(tmp_path, harness):  # noqa: F811
    runner = _runner(tmp_path)
    made = runner.create({"repo": "me/proj", "prompt": "x", "slug": "t"})
    assert made["delegate_to"] is None
    assert not {"HARNESS_AGENTS", "HARNESS_DELEGATE_TO"} & set(harness.started[0][1].env)


def test_the_profiles_api_is_for_the_browser_and_never_for_a_token(tmp_path):
    store = _store(tmp_path, CONFIGURED)
    tokens = TokenStore(tmp_path / "tokens.json")
    app = create_app(FakeBackend(), profiles=store, tokens=tokens)
    browser = TestClient(app, base_url="http://127.0.0.1")
    assert browser.put("/api/settings/profiles/coder", json=CODER).status_code == 403  # no change header
    assert browser.put("/api/settings/profiles/coder", json=CODER, headers=CHANGE).status_code == 200
    refused = browser.put("/api/settings/profiles/bad", json={**CODER, "base_url": ""}, headers=CHANGE)
    assert refused.status_code == 400 and "needs a base URL" in refused.json()["error"]
    said = browser.put("/api/settings/delegate_to", json={"names": ["coder"]}, headers=CHANGE).json()
    assert said["message"] == "New tasks hand work to coder."
    listed = browser.get("/api/settings/profiles").json()
    assert [p["name"] for p in listed["profiles"]] == ["reviewer", "coder"] and listed["delegate_to"] == ["coder"]
    assert browser.delete("/api/settings/profiles/coder", headers=CHANGE).status_code == 400  # a default delegate

    _, token = tokens.create("ci")
    api = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": f"Bearer {token}"})
    for method, path in (("GET", "/api/settings/profiles"), ("PUT", "/api/settings/profiles/coder"),
                         ("PUT", "/api/settings/delegate_to")):
        assert api.request(method, path).status_code == 403
