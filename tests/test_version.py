"""What is running: /version, /api/info's version and the page's, from the package and the image's commit."""

from importlib import metadata

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from orchestrator import version
from orchestrator.app import create_app
from orchestrator.tokens import TokenStore

from .test_orchestrator import FakeBackend


@pytest.fixture(autouse=True)
def _fresh():
    version.about.cache_clear()
    yield
    version.about.cache_clear()


def test_an_image_says_its_release_and_the_commit_it_was_built_from(tmp_path, monkeypatch):
    monkeypatch.setenv(version.REVISION_ENV, "0123456789abcdef0123456789abcdef01234567")
    tokens = TokenStore(tmp_path / "tokens.json")
    app = create_app(FakeBackend(), tokens=tokens)
    expected = {"version": metadata.version("burst-harness"), "revision": "0123456789abcdef0123456789abcdef01234567"}
    assert TestClient(app, base_url="http://127.0.0.1").get("/version").json() == expected
    assert TestClient(app, base_url="http://127.0.0.1").get("/api/info").json()["version"] == expected
    assert version.line() == f"{expected['version']} (0123456)"
    _, token = tokens.create("ci")
    with_token = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": f"Bearer {token}"})
    assert with_token.get("/version").json() == expected  # what an API token may read, besides /api/tasks
    assert with_token.get("/api/info").status_code == 403


def test_a_checkout_says_gits_commit_and_whether_it_has_changes(tmp_path, monkeypatch):
    monkeypatch.delenv(version.REVISION_ENV, raising=False)
    monkeypatch.setattr(version, "REPO", tmp_path)
    assert version.about()["revision"] == ""  # not a checkout, and no image's commit: nothing made up
    (tmp_path / ".git").mkdir()
    answers = {("rev-parse", "HEAD"): "fedcba9876543210fedcba9876543210fedcba98",
               ("status", "--porcelain", "--untracked-files=no"): " M orchestrator/app.py"}
    monkeypatch.setattr(version, "_git", lambda *args: answers[args])
    version.about.cache_clear()
    assert version.about()["revision"].startswith("fedcba9") and version.about()["dirty"] is True
    assert version.line().endswith("(fedcba9+changes)")
