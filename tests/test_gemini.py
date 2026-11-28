"""provider gemini: Google's Gemini API, with an AI Studio key. Its SDK is in the image; a task or profile with it
names a Gemini model (the default model is a Claude one); its key is offered in the Settings tab."""

from dataclasses import replace

import pytest

from harness import credentials, models
from harness.config import HarnessConfig

pytest.importorskip("google.genai")


def test_a_gemini_model_is_built_with_the_key():
    cfg = replace(HarnessConfig(), provider="gemini", model_id="gemini-test-model")
    with credentials.overlay({"GOOGLE_API_KEY": "", "GEMINI_API_KEY": "k-test"}):
        model = models.build_model(cfg)
    assert type(model).__name__ == "GeminiModel" and model.config["model_id"] == "gemini-test-model"
    assert model.client_args == {"api_key": "k-test"}


def test_a_claude_model_id_is_refused_for_gemini():
    with pytest.raises(models.ProviderError, match="a Claude model"):
        models.build_model(replace(HarnessConfig(), provider="gemini"))


def test_a_gemini_task_or_profile_names_its_model(tmp_path):
    pytest.importorskip("fastapi")
    from orchestrator.backend import SpecError, model_choice
    from orchestrator.profiles import ProfilesError, ProfileStore

    base = HarnessConfig()
    with pytest.raises(SpecError, match="needs a model id"):
        model_choice(base, "gemini", "", "", 0)
    chosen = model_choice(base, "gemini", "", "gemini-test-model", 0)
    assert (chosen["provider"], chosen["model_id"], chosen["base_url"]) == ("gemini", "gemini-test-model", "")

    store = ProfileStore(tmp_path / "profiles.json", base)
    with pytest.raises(ProfilesError, match="needs a model id"):
        store.set("g", {"provider": "gemini", "use_for": "Reading code"})
    store.set("g", {"provider": "gemini", "model_id": "gemini-test-model", "use_for": "Reading code"})


def test_the_key_is_offered_in_settings():
    pytest.importorskip("fastapi")
    from orchestrator import settings

    assert "GOOGLE_API_KEY" in settings.SUGGESTED and "AI Studio" in settings.USES["GOOGLE_API_KEY"]
