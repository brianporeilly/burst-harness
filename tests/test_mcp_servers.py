"""MCP servers saved in Settings, for sessions and tasks to choose from (orchestrator/mcp_servers.py). No
server is connected to: only their names and URLs are handled here."""

from dataclasses import replace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from harness.config import HarnessConfig
from orchestrator.app import create_app
from orchestrator.mcp_servers import McpServersError, McpServerStore
from orchestrator.tasks import TasksError

from .test_orchestrator import CHANGE, FakeBackend
from .test_tasks import _runner, harness  # noqa: F401 -- the fixture

CONTEXT7 = "context7=https://mcp.context7.com/mcp"


def _store(tmp_path, mcp=(CONTEXT7,)):
    return McpServerStore(tmp_path / "mcp.json", replace(HarnessConfig(), mcp=mcp))


def test_saved_servers_are_listed_with_the_configuration_s_and_chosen_by_default(tmp_path, monkeypatch):
    store = _store(tmp_path)
    (configured,) = store.listing()["servers"]
    assert configured | {"updated": None} == {"name": "context7", "url": "https://mcp.context7.com/mcp",
                                              "default": True, "source": "configuration", "configuration_too": True,
                                              "key": "CONTEXT7_API_KEY", "basic_key": "CONTEXT7_BASIC_AUTH",
                                              "auth": "", "key_set": False, "key_problem": "", "updated": None,
                                              "updated_by": ""}
    assert store.choose(None) == (CONTEXT7,)  # nothing saved: what new sessions and tasks got before

    store.set("docs", {"url": "https://mcp.example.com/mcp"}, user="brian")  # on by default unless said
    store.set("search", {"url": "https://search.example.com/mcp", "default": False})
    store.set("context7", {"default": False})  # the configuration's, turned off by default
    monkeypatch.setenv("DOCS_API_KEY", "k")
    rows = {row["name"]: row for row in store.listing()["servers"]}
    assert [(n, r["default"], r["source"]) for n, r in rows.items()] == [
        ("context7", False, "configuration"), ("docs", True, "settings"), ("search", False, "settings")]
    assert rows["docs"]["key_set"] and rows["docs"]["updated_by"] == "brian"
    monkeypatch.setenv("SEARCH_BASIC_AUTH", "me@example.com:tok")
    rows = {row["name"]: row for row in store.listing()["servers"]}
    assert (rows["search"]["key"], rows["search"]["auth"], rows["search"]["key_set"]) == ("SEARCH_BASIC_AUTH", "basic", True)
    monkeypatch.setenv("SEARCH_BASIC_AUTH", "already-encoded")
    assert "no colon" in {row["name"]: row for row in store.listing()["servers"]}["search"]["key_problem"]
    assert store.choose(None) == ("docs=https://mcp.example.com/mcp",)

    # Named, or given for this one alone; a name not saved, or a URL the gateway can't pass, is refused.
    assert store.choose(["search", "context7", "own=https://own.example.com/mcp"]) == (
        "search=https://search.example.com/mcp", CONTEXT7, "own=https://own.example.com/mcp")
    assert store.choose("docs\nsearch") == ("docs=https://mcp.example.com/mcp", "search=https://search.example.com/mcp")
    assert store.choose([]) == ()
    for asked, refused in [(["nope"], "No MCP server named nope"), (["x=http://plain.example.com/mcp"], "HTTPS"),
                           (["x=https://a.example.com/mcp,y"], "no commas"), ([3], "a list")]:
        with pytest.raises(McpServersError, match=refused):
            store.choose(asked)

    # The configuration's can be replaced by one saved here, and goes back when that is removed; it can't be
    # removed itself.
    store.set("context7", {"url": "https://mine.example.com/mcp"})
    assert store.servers()["context7"] == {"url": "https://mine.example.com/mcp", "default": False, "source": "settings"}
    assert store.remove("context7") == "the configuration's"
    assert store.servers()["context7"]["url"] == "https://mcp.context7.com/mcp"
    with pytest.raises(McpServersError, match="turn it off by default instead"):
        store.remove("context7")
    assert store.remove("docs") == "gone" and "docs" not in store.servers()
    for name, raw, refused in [("new", {}, "give its URL"), ("x", {"url": "http://x.example.com"}, "HTTPS"),
                               ("bad name", {"url": "https://x.example.com"}, "NAME=URL"),
                               ("x", {"url": "https://x.example.com", "default": "yes"}, "true or false")]:
        with pytest.raises(McpServersError, match=refused):
            store.set(name, raw)


def test_a_task_keeps_the_servers_it_was_given_and_its_burst_gets_only_those_and_their_keys(
        tmp_path, harness, monkeypatch):  # noqa: F811 -- the fixture
    store = _store(tmp_path)
    store.set("docs", {"url": "https://mcp.example.com/mcp", "default": False})
    monkeypatch.setenv("DOCS_API_KEY", "docs-key")
    monkeypatch.setenv("CONTEXT7_API_KEY", "c7-key")
    monkeypatch.setenv("HARNESS_MCP", CONTEXT7)  # the orchestrator's own, as a deployment sets it
    runner = _runner(tmp_path, limit=10, mcp_servers=store)

    made = runner.create({"repo": "me/proj", "prompt": "x", "slug": "defaults"})
    assert made["mcp"] == [CONTEXT7]
    env = harness.started[-1][1].env
    assert env["HARNESS_MCP"] == CONTEXT7 and env["CONTEXT7_API_KEY"] == "c7-key" and "DOCS_API_KEY" not in env

    made = runner.create({"repo": "me/proj", "prompt": "x", "slug": "docs-only", "mcp": ["docs"]})
    assert made["mcp"] == ["docs=https://mcp.example.com/mcp"]
    env = harness.started[-1][1].env
    assert env["HARNESS_MCP"] == "docs=https://mcp.example.com/mcp" and env["DOCS_API_KEY"] == "docs-key"
    assert "CONTEXT7_API_KEY" not in env

    monkeypatch.delenv("DOCS_API_KEY")
    monkeypatch.setenv("DOCS_BASIC_AUTH", "me@example.com:tok")  # granted the same way as a bearer key
    runner.create({"repo": "me/proj", "prompt": "x", "slug": "docs-basic", "mcp": ["docs"]})
    env = harness.started[-1][1].env
    assert env["DOCS_BASIC_AUTH"] == "me@example.com:tok" and "DOCS_API_KEY" not in env

    runner.create({"repo": "me/proj", "prompt": "x", "slug": "none", "mcp": []})
    env = harness.started[-1][1].env
    assert env["HARNESS_MCP"] == "" and "CONTEXT7_API_KEY" not in env  # none: not the orchestrator's either

    store.set("docs", {"url": "https://moved.example.com/mcp"})  # what a task was given stays with it
    assert runner.task("docs-only")["mcp"] == ["docs=https://mcp.example.com/mcp"]
    with pytest.raises(TasksError, match="No MCP server named nope"):
        runner.create({"repo": "me/proj", "prompt": "x", "mcp": ["nope"]})
    with pytest.raises(TasksError, match="no Settings"):
        _runner(tmp_path / "plain").create({"repo": "me/proj", "prompt": "x", "mcp": ["docs"]})


def test_sessions_get_the_defaults_or_the_servers_named_and_settings_change_them(tmp_path):
    store = _store(tmp_path)
    backend = FakeBackend()
    client = TestClient(create_app(backend, mcp_servers=store), base_url="http://127.0.0.1:8700")

    assert client.post("/api/sessions", json={"name": "plain"}, headers=CHANGE).status_code == 202
    assert backend.created[-1].mcp == (CONTEXT7,)
    saved = client.put("/api/settings/mcp/docs", json={"url": "https://mcp.example.com/mcp", "default": False},
                       headers=CHANGE)
    assert saved.status_code == 200 and "off by default" in saved.json()["message"]
    assert client.put("/api/settings/mcp/docs", json={"default": True}).status_code == 403  # no X-Orchestrator
    assert client.put("/api/settings/mcp/context7", json={"default": False}, headers=CHANGE).status_code == 200
    assert [s["name"] for s in client.get("/api/settings/mcp").json()["servers"] if s["default"]] == []
    assert client.post("/api/sessions", json={"name": "chosen", "mcp": ["docs", "own=https://own.example.com/mcp"]},
                       headers=CHANGE).status_code == 202
    assert backend.created[-1].mcp == ("docs=https://mcp.example.com/mcp", "own=https://own.example.com/mcp")
    assert client.post("/api/sessions", json={"name": "none"}, headers=CHANGE).status_code == 202
    assert backend.created[-1].mcp == ()
    refused = client.post("/api/sessions", json={"name": "bad", "mcp": ["nope"]}, headers=CHANGE)
    assert refused.status_code == 400 and "nope" in refused.json()["error"]
    assert client.delete("/api/settings/mcp/docs", headers=CHANGE).json()["message"] == "Removed docs."
    # Removing what was saved of the configuration's server goes back to it as configured: on by default.
    assert "configuration's is used again" in client.delete("/api/settings/mcp/context7", headers=CHANGE).json()["message"]
    assert store.servers()["context7"]["default"] is True
    assert client.delete("/api/settings/mcp/context7", headers=CHANGE).status_code == 400
