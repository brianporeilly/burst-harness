"""Settings for every task (task_settings.py): Claude Code's attribution, set in the page and given to bursts."""

import json
from dataclasses import replace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from harness.config import HarnessConfig
from orchestrator.app import create_app
from orchestrator.task_settings import TaskSettings, TaskSettingsError
from orchestrator.tokens import TokenStore

from .test_orchestrator import CHANGE, FakeBackend
from .test_tasks import _runner, _settled, harness  # noqa: F401 (harness: a fixture)


def _store(tmp_path, attribution=True):
    base = replace(HarnessConfig(), claude_attribution=attribution)
    return TaskSettings(tmp_path / "orchestrator" / "task-settings.json", base)


def test_attribution_is_the_configurations_until_set_here(tmp_path):
    store = _store(tmp_path)
    listed = store.listing()
    assert listed["claude_attribution"] is True and listed["claude_attribution_source"] == "configuration"
    assert store.environment() == {}  # a burst inherits the orchestrator's own
    store.set_attribution(False, user="me")
    listed = store.listing()
    assert listed["claude_attribution"] is False and listed["claude_attribution_source"] == "settings"
    assert listed["updated_by"] == "me"
    assert store.environment() == {"HARNESS_CLAUDE_ATTRIBUTION": "false"}
    assert json.loads((tmp_path / "orchestrator" / "task-settings.json").read_text())["claude_attribution"] is False
    store.set_attribution(True)
    assert store.environment() == {"HARNESS_CLAUDE_ATTRIBUTION": "true"}  # on here, over a configuration's off
    store.set_attribution(None)
    assert store.listing()["claude_attribution_source"] == "configuration" and store.environment() == {}
    with pytest.raises(TaskSettingsError, match="true, false, or null"):
        store.set_attribution("no")
    assert _store(tmp_path / "other", attribution=False).listing()["claude_attribution"] is False


def test_bursts_that_start_from_now_on_are_given_it_and_the_harness_reads_it(tmp_path, harness, monkeypatch):  # noqa: F811
    runner = _runner(tmp_path)
    runner.settings = store = _store(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "t", "engine": "claude-code"})
    assert "HARNESS_CLAUDE_ATTRIBUTION" not in harness.started[0][1].env
    harness.started[0][1].finish(0, state="paused")
    _settled(runner, "t", "paused")
    store.set_attribution(False)
    runner.resume("t", "go on")  # a continued task gets what is set now
    env = harness.started[1][1].env
    assert env["HARNESS_CLAUDE_ATTRIBUTION"] == "false"
    monkeypatch.setenv("HARNESS_CLAUDE_ATTRIBUTION", env["HARNESS_CLAUDE_ATTRIBUTION"])
    assert HarnessConfig.load(None, {}, user_config=None).claude_attribution is False


def test_the_task_settings_api_is_for_the_browser_and_never_for_a_token(tmp_path):
    store = _store(tmp_path)
    tokens = TokenStore(tmp_path / "tokens.json")
    app = create_app(FakeBackend(), task_settings=store, tokens=tokens)
    browser = TestClient(app, base_url="http://127.0.0.1")
    assert browser.get("/api/settings/tasks").json()["claude_attribution_source"] == "configuration"
    assert browser.put("/api/settings/tasks", json={"claude_attribution": False}).status_code == 403  # no change header
    said = browser.put("/api/settings/tasks", json={"claude_attribution": False}, headers=CHANGE).json()
    assert said["message"] == "Claude Code leaves out its attribution in tasks' commits and PRs from now on."
    assert store.environment() == {"HARNESS_CLAUDE_ATTRIBUTION": "false"}
    for bad in ({"claude_attribution": "off"}, {}):
        refused = browser.put("/api/settings/tasks", json=bad, headers=CHANGE)
        assert refused.status_code == 400 and "claude_attribution" in refused.json()["error"]
    said = browser.put("/api/settings/tasks", json={"claude_attribution": None}, headers=CHANGE).json()
    assert said["message"].endswith("from now on (the configuration's).") and store.environment() == {}

    _, token = tokens.create("ci")
    api = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": f"Bearer {token}"})
    for method in ("GET", "PUT"):
        assert api.request(method, "/api/settings/tasks").status_code == 403


def test_without_the_store_the_api_says_settings_are_off(tmp_path):
    browser = TestClient(create_app(FakeBackend()), base_url="http://127.0.0.1")
    refused = browser.get("/api/settings/tasks")
    assert refused.status_code == 400 and "not on" in refused.json()["error"]
