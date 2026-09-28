from harness.config import HarnessConfig
from harness.models import build_model


def test_llamacpp_from_env(monkeypatch):
    """The documented env names reach the request; llama.cpp gets a real timeout."""
    monkeypatch.setenv("HARNESS_PROVIDER", "llamacpp")
    monkeypatch.setenv("HARNESS_BASE_URL", "http://llm.example:8080/")
    monkeypatch.setenv("HARNESS_MODEL_ID", "qwen")
    monkeypatch.setenv("HARNESS_CONTEXT_WINDOW_LIMIT", "65536")
    monkeypatch.setenv("HARNESS_MODEL_PARAMS", '{"max_tokens": 8192, "temperature": 0.7}')
    monkeypatch.setenv("HARNESS_MODEL_TIMEOUT", "900")
    model = build_model(HarnessConfig.load(None, user_config=None))
    assert model.base_url == "http://llm.example:8080"
    assert model.config == {
        "model_id": "qwen",
        "context_window_limit": 65536,
        "params": {"max_tokens": 8192, "temperature": 0.7},
    }
    assert model.client.timeout.read == 900


def test_anthropic_params_merge_over_native():
    model = build_model(HarnessConfig(model_params='{"temperature": 1.0}'))
    assert model.config["params"]["temperature"] == 1.0
    assert model.config["params"]["output_config"] == {"effort": "high"}
