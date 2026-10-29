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


def _llamacpp(params: str):
    return build_model(HarnessConfig(provider="llamacpp", base_url="http://llm.example:8080", model_params=params))


def test_llamacpp_sends_params_strands_does_not_know(caplog):
    model = _llamacpp('{"temperature": 0.2, "chat_template_kwargs": {"enable_thinking": false}, "n_predict": 64}')
    with caplog.at_level("WARNING"):
        request = model._format_request([{"role": "user", "content": [{"text": "hi"}]}])
        model._format_request([{"role": "user", "content": [{"text": "again"}]}])
    assert request["temperature"] == 0.2
    assert request["chat_template_kwargs"] == {"enable_thinking": False} and request["n_predict"] == 64
    announced = [r.getMessage() for r in caplog.records if "Passing to llama.cpp" in r.getMessage()]
    assert announced == ["Passing to llama.cpp as given (Strands does not know them): chat_template_kwargs, n_predict"]


def test_llamacpp_params_never_replace_what_strands_builds():
    request = _llamacpp('{"messages": [], "stream": false}')._format_request(
        [{"role": "user", "content": [{"text": "hi"}]}])
    assert request["stream"] is True and request["messages"]


def test_replies_are_capped_as_a_request_param_where_strands_has_no_setting(caplog):
    from typing import TypedDict

    from harness.models import model_kwargs

    class OpenAIModel:  # as Strands' (the openai package is optional): params, and no max_tokens
        class OpenAIConfig(TypedDict, total=False):
            model_id: str
            params: dict

    with caplog.at_level("WARNING"):
        model = build_model(HarnessConfig(provider="llamacpp", base_url="http://l:8080", context_window_limit=40000))
        openai = model_kwargs(HarnessConfig(max_tokens=8192), OpenAIModel, cap_in_params=True)
    assert model.config["params"] == {"max_tokens": 20000}  # half the window, under max_tokens
    assert openai["params"] == {"max_tokens": 8192}
    assert not [r for r in caplog.records if "max_tokens" in r.getMessage()]
    # model_params' own cap wins, in whichever name the server takes it.
    assert _llamacpp('{"max_tokens": 8192}').config["params"] == {"max_tokens": 8192}
    assert _llamacpp('{"n_predict": 64}').config["params"] == {"n_predict": 64}
    assert model_kwargs(HarnessConfig(model_params='{"max_completion_tokens": 900}'), OpenAIModel,
                        cap_in_params=True)["params"] == {"max_completion_tokens": 900}


def _after(exception):
    from types import SimpleNamespace

    return SimpleNamespace(retry=False, stop_response=None, exception=exception)


def test_timeouts_are_retried_model_retries_times_and_logged(caplog, monkeypatch):
    import asyncio

    import httpx

    from harness.models import retry_strategy

    strategy = retry_strategy(HarnessConfig(model_retries=2, model_timeout=900))
    monkeypatch.setattr(strategy, "_calculate_delay", lambda attempt: 0)
    events = [_after(httpx.ReadTimeout("timed out")) for _ in range(3)]
    with caplog.at_level("WARNING"):
        for event in events:
            asyncio.run(strategy._handle_after_model_call(event))
    assert [e.retry for e in events] == [True, True, False]
    messages = [r.getMessage() for r in caplog.records]
    assert "retry 1 of 2" in messages[0] and "retry 2 of 2" in messages[1]
    assert "no retries left (model_retries=2)" in messages[2]

    # A success resets the count.
    asyncio.run(strategy._handle_after_model_call(_after(None)))
    event = _after(httpx.ReadTimeout("timed out"))
    asyncio.run(strategy._handle_after_model_call(event))
    assert event.retry


def test_no_timeout_retries_and_throttling_still_retried(caplog, monkeypatch):
    import asyncio

    import httpx
    from strands.types.exceptions import ModelThrottledException

    from harness.models import retry_strategy

    strategy = retry_strategy(HarnessConfig(model_retries=0))
    monkeypatch.setattr(strategy, "_calculate_delay", lambda attempt: 0)
    timeout = _after(httpx.ReadTimeout("timed out"))
    throttled = _after(ModelThrottledException("llama.cpp server is busy or overloaded"))
    other = _after(ValueError("bad request"))
    with caplog.at_level("WARNING"):
        for event in (timeout, throttled, other):
            asyncio.run(strategy._handle_after_model_call(event))
    assert (timeout.retry, throttled.retry, other.retry) == (False, True, False)
    assert any("throttled" in r.getMessage() and "retry 1 of 7" in r.getMessage() for r in caplog.records)


def test_a_server_that_went_away_is_backed_off_and_retried(caplog, monkeypatch):
    """A restarting llama-server: the reply is cut, then connections are refused, then it loads (503)."""
    import asyncio

    import httpx
    from strands.types.exceptions import ModelThrottledException

    from harness.models import retry_strategy

    strategy = retry_strategy(HarnessConfig(model_retries=0))
    delays = []
    monkeypatch.setattr(strategy, "_calculate_delay", lambda attempt: delays.append(attempt) or 0)
    cut = httpx.RemoteProtocolError("peer closed connection without sending complete message body")
    wrapped = RuntimeError("stream failed")
    wrapped.__cause__ = cut
    events = [_after(e) for e in (wrapped, httpx.ConnectError("refused"), ConnectionResetError("reset"),
                                  ModelThrottledException("llama.cpp server is busy or overloaded"))]
    with caplog.at_level("WARNING"):
        for event in events:
            asyncio.run(strategy._handle_after_model_call(event))
    assert all(e.retry for e in events)
    messages = [r.getMessage() for r in caplog.records]
    assert "lost the server" in messages[0] and "RemoteProtocolError" not in messages[0]  # the wrapper is named
    assert "retry 1 of 7" in messages[0] and "retry 3 of 7" in messages[2] and "throttled" in messages[3]

    # Eight attempts in all, then it gives up.
    for _ in range(3):
        asyncio.run(strategy._handle_after_model_call(_after(httpx.ConnectError("refused"))))
    last = _after(httpx.ConnectError("refused"))
    with caplog.at_level("WARNING"):
        asyncio.run(strategy._handle_after_model_call(last))
    assert not last.retry and "giving up after 8 attempts" in caplog.records[-1].getMessage()

    # A timeout is still a timeout, not a lost server (model_retries=0: not retried).
    timeout = _after(httpx.ConnectTimeout("timed out"))
    asyncio.run(strategy._handle_after_model_call(timeout))
    assert not timeout.retry
