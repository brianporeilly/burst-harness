"""API tokens: made and revoked by someone signed in, kept only as a hash, and reaching /api/tasks alone."""

import json
import stat

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from orchestrator.app import create_app
from orchestrator.tokens import TokenError, TokenStore

from .test_orchestrator import CHANGE, FakeBackend
from .test_tasks import _runner, harness  # noqa: F401 (the fixture)


def test_a_token_is_shown_once_and_kept_only_as_a_hash(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    record, token = store.create("laptop script", user="brian")
    assert token.startswith(f"orch_{record['id']}_") and "hash" not in record
    on_disk = (tmp_path / "tokens.json").read_text()
    assert token not in on_disk and token.split("_", 2)[2] not in on_disk
    assert stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode) == 0o600
    checked = store.check(token)
    assert checked["name"] == "laptop script" and checked["last_used"] and "hash" not in checked
    assert store.check(token[:-1] + ("A" if token[-1] != "A" else "B")) is None
    assert store.check("orch_00000000_" + "x" * 43) is None and store.check("nonsense") is None
    with pytest.raises(TokenError, match="already"):
        store.create("laptop script")
    with pytest.raises(TokenError, match="Name the token"):
        store.create("")
    store.revoke(record["id"])
    assert store.check(token) is None and store.tokens() == []


def _app(tmp_path, **options):
    store = TokenStore(tmp_path / "tokens.json")
    app = create_app(FakeBackend(), tasks=_runner(tmp_path), tokens=store, **options)
    return app, store


@pytest.mark.usefixtures("harness")
def test_a_token_reaches_the_tasks_api_and_nothing_else(tmp_path):
    app, _ = _app(tmp_path, hosts=("orchestrator.lan",), user_header="Remote-User")
    browser = TestClient(app, base_url="http://orchestrator.lan")
    made = browser.post("/api/tokens", json={"name": "ci"}, headers={**CHANGE, "Remote-User": "brian"})
    assert made.status_code == 201 and made.json()["created_by"] == "brian"
    token = made.json()["token"]
    # From anywhere, by any name: no Host check, no X-Orchestrator header, no proxy's sign-in.
    api = TestClient(app, base_url="http://10.0.0.5:8700", headers={"Authorization": f"Bearer {token}"})
    task = api.post("/api/tasks", json={"repo": "me/proj", "prompt": "x", "slug": "t"})
    assert task.status_code == 202 and task.json()["created_by"] == "brian via ci"
    assert [t["slug"] for t in api.get("/api/tasks").json()["tasks"]] == ["t"]
    assert api.get("/api/tasks/t").status_code == 200
    for method, path in [("GET", "/api/sessions"), ("GET", "/api/info"), ("GET", "/"), ("GET", "/api/jobs"),
                         ("GET", "/api/tokens"), ("POST", "/api/tokens"), ("DELETE", f"/api/tokens/{made.json()['id']}")]:
        assert api.request(method, path).status_code == 403, path
    bad = TestClient(app, base_url="http://10.0.0.5:8700", headers={"Authorization": "Bearer orch_nope"})
    assert bad.get("/api/tasks").status_code == 401
    listed = browser.get("/api/tokens", headers={"Remote-User": "brian"}).json()
    assert [t["name"] for t in listed] == ["ci"] and "token" not in listed[0] and listed[0]["last_used"]
    assert browser.delete(f"/api/tokens/{made.json()['id']}", headers={**CHANGE, "Remote-User": "brian"}).status_code == 200
    assert api.get("/api/tasks").status_code == 401


@pytest.mark.usefixtures("harness")
def test_listening_beyond_this_machine_without_a_proxy_the_browser_is_this_machines(tmp_path):
    app, store = _app(tmp_path, local_browser=True)
    here = TestClient(app, base_url="http://127.0.0.1:8700", client=("127.0.0.1", 50000))
    assert here.get("/api/tasks").status_code == 200
    _, token = store.create("other machine")
    away = TestClient(app, base_url="http://192.168.1.20:8700", client=("192.168.1.30", 50000))
    refused = away.get("/api/tasks")
    assert refused.status_code == 401 and "API token" in refused.json()["error"]
    assert away.get("/api/tasks", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert json.loads(away.get("/").text)["error"]  # not the page either
