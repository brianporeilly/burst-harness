"""Model suggestions (orchestrator/model_list.py, POST /api/models): each provider asked for its models with the
orchestrator's keys, on a local fake server standing in for every provider; and OpenAI's own API as provider
openai with no base URL."""

import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from harness import credentials, models
from harness.config import HarnessConfig
from orchestrator import model_list
from orchestrator.app import create_app
from orchestrator.backend import SessionSpec, SpecError, model_choice

from .test_orchestrator import CHANGE, FakeBackend

REPLIES = {
    "/v1/models?limit=1000": {"data": [{"id": "claude-opus-5-5"}, {"id": "claude-haiku-5-5"}]},
    "/v1beta/models?pageSize=1000": {"models": [
        {"name": "models/gemini-pro-x", "supportedGenerationMethods": ["generateContent", "countTokens"]},
        {"name": "models/text-embedding-x", "supportedGenerationMethods": ["embedContent"]}],
        "nextPageToken": "p2"},
    "/v1beta/models?pageSize=1000&pageToken=p2": {"models": [
        {"name": "models/gemini-flash-x", "supportedGenerationMethods": ["generateContent"]}]},
    "/openai/models": {"data": [{"id": "gpt-x"}, {"id": "text-embedding-3-large"}, {"id": "tts-1"},
                                {"id": "o-mini-x"}]},
    "/v1/models": {"data": [{"id": "/models/Qwen3.8-27B/Qwen3.8-27B.gguf"}]},  # llama-server, no --alias
    "/api/tags": {"models": [{"name": "llama4:8b"}]},
}


class Fake(BaseHTTPRequestHandler):
    seen: list[tuple[str, dict[str, str]]] = []

    def do_GET(self):  # noqa: N802
        Fake.seen.append((self.path, self.headers))  # its .get ignores case
        if "refuse" in self.path:
            self.send_response(401)
            self.end_headers()
            return
        body = json.dumps(REPLIES.get(self.path, {})).encode()
        self.send_response(200 if self.path in REPLIES else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    for name, path in (("ANTHROPIC", ""), ("GEMINI", ""), ("OPENAI", "/openai"), ("OLLAMA", "")):
        monkeypatch.setattr(model_list, name, url + path)
    monkeypatch.setattr(model_list, "_cache", {})
    Fake.seen = []
    keys = {"ANTHROPIC_API_KEY": "a-key", "GOOGLE_API_KEY": "g-key", "GEMINI_API_KEY": "", "OPENAI_API_KEY": "o-key"}
    with credentials.overlay(keys):
        yield url
    server.shutdown()


def test_each_provider_lists_its_models_with_its_key(fake):
    assert model_list.models("anthropic") == {"models": ["claude-haiku-5-5", "claude-opus-5-5"], "why": ""}
    assert Fake.seen[-1][1]["x-api-key"] == "a-key"
    # Gemini's, every page, the ones that generate content, without "models/"
    assert model_list.models("gemini")["models"] == ["gemini-flash-x", "gemini-pro-x"]
    assert Fake.seen[-1][1]["x-goog-api-key"] == "g-key"
    # OpenAI's own: its chat models; a compatible server's: all it lists
    assert model_list.models("openai")["models"] == ["gpt-x", "o-mini-x"]
    assert Fake.seen[-1][1]["Authorization"] == "Bearer o-key"
    assert model_list.models("openai", fake + "/v1")["models"] == ["Qwen3.8-27B"]
    assert model_list.models("llamacpp", fake)["models"] == ["Qwen3.8-27B"]  # the server's root: /v1/models
    assert model_list.models("ollama", fake)["models"] == ["llama4:8b"]


def test_no_list_says_why_and_the_field_is_left_for_typing(fake):
    assert model_list.models("bedrock") == {"models": [], "why": "no list of models for provider bedrock: type its id"}
    assert model_list.models("llamacpp")["why"] == "give the server's base URL"
    assert "answered 401 (is the key right?)" in model_list.models("openai", fake + "/refuse")["why"]
    assert "could not ask 127.0.0.1:9" in model_list.models("llamacpp", "http://127.0.0.1:9")["why"]
    with credentials.overlay({"GOOGLE_API_KEY": "", "GEMINI_API_KEY": ""}):
        model_list._cache.clear()
        assert model_list.models("gemini")["why"] == "no GOOGLE_API_KEY in Settings"


def test_an_answer_is_kept_a_while(fake):
    model_list.models("anthropic")
    model_list.models("anthropic")
    assert len(Fake.seen) == 1


def test_the_page_asks_with_a_post(fake):
    app = TestClient(create_app(FakeBackend()), base_url="http://127.0.0.1")
    assert app.post("/api/models", json={"provider": "anthropic"}).status_code == 403  # no change header
    said = app.post("/api/models", json={"provider": "anthropic"}, headers=CHANGE).json()
    assert said["models"] == ["claude-haiku-5-5", "claude-opus-5-5"]
    refused = app.post("/api/models", json={"provider": "openai", "base_url": "file:///etc"}, headers=CHANGE)
    assert refused.status_code == 400 and "not a base URL" in refused.json()["error"]


def test_openai_is_openais_own_api_without_a_base_url():
    base = HarnessConfig(provider="llamacpp", base_url="http://typhon:8080")
    with pytest.raises(SpecError, match="OpenAI's own API needs a model id"):
        model_choice(base, "openai", "", "", 0)
    chosen = model_choice(base, "openai", "", "gpt-x", 0)
    assert (chosen["provider"], chosen["base_url"], chosen["model_id"]) == ("openai", "", "gpt-x")
    assert model_choice(base, "openai", "http://vllm:8000/v1", "", 0)["base_url"] == "http://vllm:8000/v1"
    # A session's opencode reaches openai only at a server's URL.
    with pytest.raises(SpecError, match="needs a base URL"):
        SessionSpec.parse({"name": "box", "provider": "openai", "model": "gpt-x"}).overrides(base)
    # The harness: a Claude model id is no model of OpenAI's.
    with credentials.overlay({"OPENAI_API_KEY": "k"}), pytest.raises(models.ProviderError, match="a Claude model"):
        models.build_model(replace(HarnessConfig(), provider="openai"))
