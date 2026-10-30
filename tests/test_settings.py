"""Settings: credentials set in the page, kept encrypted, used before the environment's, never shown."""

import stat

import pytest

pytest.importorskip("fastapi")
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from harness import credentials
from orchestrator.app import create_app
from orchestrator.settings import KEY_ENV, CredentialStore, SettingsError, load_key
from orchestrator.tokens import TokenStore

from .test_orchestrator import CHANGE, FakeBackend


@pytest.fixture(autouse=True)
def _nothing_stored(monkeypatch):
    monkeypatch.setattr(credentials, "_stored", {})
    monkeypatch.delenv(KEY_ENV, raising=False)


def test_a_stored_credential_is_used_before_the_environments_and_kept_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    key, _ = load_key(tmp_path)
    store = CredentialStore(tmp_path / "credentials.enc", key)
    store.set("ANTHROPIC_API_KEY", "  sk-stored-value  ", user="brian")
    assert credentials.get("ANTHROPIC_API_KEY") == "sk-stored-value"
    assert credentials.for_run(["ANTHROPIC_API_KEY"])["ANTHROPIC_API_KEY"] == "sk-stored-value"  # a task's burst
    on_disk = (tmp_path / "credentials.enc").read_bytes()
    assert b"sk-stored-value" not in on_disk and b"ANTHROPIC" not in on_disk
    for name in ("credentials.enc", "secrets.key"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600
    row = next(r for r in store.listing() if r["name"] == "ANTHROPIC_API_KEY")
    assert row["source"] == "stored" and row["environment_too"] and row["updated_by"] == "brian"
    assert "sk-stored-value" not in repr(store.listing())

    # A restart reads it back, with the same key.
    credentials.use_stored({})
    CredentialStore(tmp_path / "credentials.enc", key)
    assert credentials.get("ANTHROPIC_API_KEY") == "sk-stored-value"

    assert store.clear("ANTHROPIC_API_KEY") and not store.clear("ANTHROPIC_API_KEY")
    assert credentials.get("ANTHROPIC_API_KEY") == "from-env"


def test_a_key_that_does_not_open_the_store_stops_it(tmp_path, monkeypatch):
    key, _ = load_key(tmp_path)
    CredentialStore(tmp_path / "credentials.enc", key).set("HARNESS_GIT_TOKEN", "t")
    with pytest.raises(SettingsError, match="does not open with this key"):
        CredentialStore(tmp_path / "credentials.enc", Fernet.generate_key())
    monkeypatch.setenv(KEY_ENV, "not-a-key")
    with pytest.raises(SettingsError, match="not a Fernet key"):
        load_key(tmp_path)
    given = Fernet.generate_key().decode()
    monkeypatch.setenv(KEY_ENV, given)
    assert load_key(tmp_path)[0] == given.encode()


def test_only_credentials_and_one_line_values(tmp_path):
    store = CredentialStore(tmp_path / "credentials.enc", load_key(tmp_path)[0])
    for name, value, match in [("HARNESS_PROVIDER", "anthropic", "not a credential"),
                               ("lower_TOKEN", "x", "capitals"), ("MY_TOKEN", "", "Give a value"),
                               ("MY_TOKEN", "a\nb", "one line")]:
        with pytest.raises(SettingsError, match=match):
            store.set(name, value)
    store.set("MY_TOKEN", "x")
    assert credentials.get("MY_TOKEN") == "x"


def test_the_settings_api_is_write_only_and_never_for_a_token(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = CredentialStore(tmp_path / "credentials.enc", load_key(tmp_path)[0])
    tokens = TokenStore(tmp_path / "tokens.json")
    app = create_app(FakeBackend(), settings=store, tokens=tokens)
    browser = TestClient(app, base_url="http://127.0.0.1")
    assert browser.put("/api/settings/credentials/OPENAI_API_KEY", json={"value": "sk-x"}).status_code == 403
    assert browser.put("/api/settings/credentials/OPENAI_API_KEY", json={"value": "sk-x"},
                       headers=CHANGE).status_code == 200
    listed = browser.get("/api/settings/credentials")
    assert "sk-x" not in listed.text
    assert next(r for r in listed.json() if r["name"] == "OPENAI_API_KEY")["source"] == "stored"
    refused = browser.put("/api/settings/credentials/HARNESS_TARGET", json={"value": "docker"}, headers=CHANGE)
    assert refused.status_code == 400 and "not a credential" in refused.json()["error"]

    _, token = tokens.create("ci")
    api = TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": f"Bearer {token}"})
    for method in ("GET", "PUT", "DELETE"):
        assert api.request(method, "/api/settings/credentials/OPENAI_API_KEY").status_code == 403

    cleared = browser.delete("/api/settings/credentials/OPENAI_API_KEY", headers=CHANGE)
    assert cleared.status_code == 200 and "no value" in cleared.json()["message"]
    assert credentials.get("OPENAI_API_KEY") == ""
