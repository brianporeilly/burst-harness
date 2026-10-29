"""Model provider selection.

Pick with HARNESS_PROVIDER: a built-in name (see BUILDERS), the name of an
installed plugin, or ``module:function``. A plugin is any callable taking a
HarnessConfig and returning a Strands Model, registered under the
``burst_harness.providers`` entry-point group::

    [project.entry-points."burst_harness.providers"]
    myproxy = "myproxy_harness:build"

A plugin should build its kwargs with :func:`model_kwargs` so it gets the same
schema fitting, sampling params, and prompt caching as the built-ins.

Two things are deliberately provider-scoped rather than global:

* **Sampling params.** ``thinking: adaptive`` and ``output_config.effort`` are
  Anthropic Messages API features. Sending them to an OpenAI-compatible or
  llama.cpp endpoint is an error, so they are only applied when the model
  speaks the Messages API. ``HARNESS_MODEL_PARAMS`` (raw JSON) is the escape hatch for
  everything else, and merges last.
* **Prompt caching.** Likewise only for the Messages API.

``context_window_limit`` is passed to every provider, because Strands otherwise
assumes 200K and every utilization gate misfires on a larger model.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import time
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover
    from strands.models.model import Model

from . import credentials as secrets_
from .auth import credentials
from .config import HarnessConfig

PLUGIN_GROUP = "burst_harness.providers"

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """The provider is unknown, or its optional dependency is not installed."""


def _require(module: str, cls: str, extra: str) -> Any:
    """Import a provider class, turning a missing extra into actionable advice."""
    try:
        return getattr(importlib.import_module(module), cls)
    except ModuleNotFoundError as err:
        raise ProviderError(
            f"{cls} needs an optional dependency: pip install 'strands-agents[{extra}]' "
            f"(missing module: {err.name})"
        ) from err
    except Exception as err:  # metadata errors surface the same way
        raise ProviderError(f"Could not load {module}.{cls}: {err}") from err


def _json_overrides(cfg: HarnessConfig) -> dict[str, Any]:
    if not cfg.model_params:
        return {}
    try:
        extra = json.loads(cfg.model_params)
    except json.JSONDecodeError as err:
        raise ProviderError(f"HARNESS_MODEL_PARAMS is not valid JSON: {err}") from err
    if not isinstance(extra, dict):
        raise ProviderError("HARNESS_MODEL_PARAMS must be a JSON object.")
    return extra


def _native_params(cfg: HarnessConfig, messages_api: bool) -> dict[str, Any]:
    """Anthropic Messages API sampling settings. Not valid on other surfaces."""
    if not messages_api:
        return {}
    params: dict[str, Any] = {"output_config": {"effort": cfg.effort}}
    if cfg.thinking:
        params["thinking"] = {"type": "adaptive"}
    return params


def _config_keys(model_cls: Any) -> set[str]:
    """The config keys a provider actually accepts, from its nested Config TypedDict."""
    for name, value in vars(model_cls).items():
        if name.endswith("Config") and hasattr(value, "__required_keys__"):
            return set(value.__required_keys__) | set(value.__optional_keys__)
    return set()


def model_kwargs(cfg: HarnessConfig, model_cls: Any, *, messages_api: bool = False) -> dict[str, Any]:
    """Build provider kwargs against that provider's own schema.

    ``messages_api`` says the endpoint speaks the Anthropic Messages API, which
    turns on effort, adaptive thinking, and prompt caching.

    Providers disagree about where settings live -- Bedrock has no ``params`` and
    takes ``temperature`` at the top level; llama.cpp has no ``max_tokens``. Strands
    only *warns* about unknown keys and then ignores them, so passing one blanket
    set of kwargs silently drops settings. Anything unsupported is reported here
    rather than vanishing.
    """
    keys = _config_keys(model_cls)
    overrides = _json_overrides(cfg)
    kwargs: dict[str, Any] = {"model_id": cfg.model_id}
    if "context_window_limit" in keys:
        kwargs["context_window_limit"] = cfg.context_window_limit
    if "max_tokens" in keys:
        kwargs["max_tokens"] = cfg.max_tokens
    elif cfg.max_tokens and "max_tokens" not in overrides:
        logger.warning(
            "%s has no max_tokens setting; if this provider caps output, set it via "
            "HARNESS_MODEL_PARAMS.",
            model_cls.__name__,
        )

    if "params" in keys:
        params = _native_params(cfg, messages_api)
        params.update(overrides)
        if params:
            kwargs["params"] = params
    else:
        # No nested params: settings like temperature are top-level config keys.
        kwargs.update(overrides)

    if messages_api and "cache_config" in keys:
        from strands.models.model import CacheConfig

        kwargs["cache_config"] = CacheConfig(
            strategy="auto", system_prompt_ttl=cfg.cache_ttl, tools_ttl=cfg.cache_ttl
        )

    unsupported = sorted(set(kwargs) - keys) if keys else []
    if unsupported:
        logger.warning("%s does not accept: %s", model_cls.__name__, ", ".join(unsupported))
        kwargs = {k: v for k, v in kwargs.items() if k in keys}
    return kwargs


def _anthropic(cfg: HarnessConfig) -> "Model":
    """Anthropic, or any Messages-API endpoint via base_url.

    Credentials come from token_command/token_file when set (a proxy wanting a
    bearer token), else the SDK's own chain (ANTHROPIC_API_KEY).
    """
    AnthropicModel = _require("strands.models.anthropic", "AnthropicModel", "anthropic")
    client_args: dict[str, Any] = {}
    if cfg.base_url:
        client_args["base_url"] = cfg.base_url
    if provider := credentials(cfg):
        client_args["credentials"] = provider
    elif key := secrets_.get("ANTHROPIC_API_KEY"):
        client_args["api_key"] = key
    return AnthropicModel(client_args=client_args, **model_kwargs(cfg, AnthropicModel, messages_api=True))


def _openai(cfg: HarnessConfig) -> "Model":
    """Any OpenAI-compatible endpoint: OpenAI, vLLM, LM Studio, llama-server's /v1."""
    OpenAIModel = _require("strands.models.openai", "OpenAIModel", "openai")
    base_url = cfg.base_url or os.environ.get("OPENAI_BASE_URL", "")
    api_key = secrets_.get("OPENAI_API_KEY")
    client_args: dict[str, Any] = {"api_key": api_key or "not-needed"}
    if base_url:
        client_args["base_url"] = base_url
    return OpenAIModel(client_args=client_args, **model_kwargs(cfg, OpenAIModel))


def _bedrock(cfg: HarnessConfig) -> "Model":
    BedrockModel = _require("strands.models.bedrock", "BedrockModel", "bedrock")
    kwargs = model_kwargs(cfg, BedrockModel)
    if region := os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"):
        kwargs["region_name"] = region
    return BedrockModel(**kwargs)


def _llamacpp(cfg: HarnessConfig) -> "Model":
    """A self-hosted llama.cpp server."""
    LlamaCppModel = _require("strands.models.llamacpp", "LlamaCppModel", "llamacpp")
    base_url = cfg.base_url or os.environ.get("LLAMACPP_BASE_URL", "http://localhost:8080")
    return _passing_through(LlamaCppModel)(base_url=base_url, timeout=cfg.model_timeout,
                                           **model_kwargs(cfg, LlamaCppModel))


def _passing_through(LlamaCppModel: Any) -> Any:
    """LlamaCppModel, sending every param it is given rather than its own list of them.

    Strands copies only the params it knows into the request body and drops the
    rest without a word, but llama-server reads many more from there:
    ``chat_template_kwargs`` (``{"enable_thinking": false}``), ``n_predict``,
    ``dry_multiplier``, ``reasoning_format``... Those go in as they are, and are
    named once so it is clear they were sent.
    """

    class PassThroughLlamaCppModel(LlamaCppModel):  # type: ignore[misc, valid-type]
        _announced = False

        def _format_request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            request = super()._format_request(*args, **kwargs)
            params = self.config.get("params") or {}
            extra = {k: v for k, v in params.items() if k not in request} if isinstance(params, dict) else {}
            if extra and not self._announced:
                logger.warning("Passing to llama.cpp as given (Strands does not know them): %s",
                               ", ".join(sorted(extra)))
                self._announced = True
            request.update(extra)
            return request

    return PassThroughLlamaCppModel


def _ollama(cfg: HarnessConfig) -> "Model":
    OllamaModel = _require("strands.models.ollama", "OllamaModel", "ollama")
    host = cfg.base_url or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    return OllamaModel(host=host, **model_kwargs(cfg, OllamaModel))


def _gemini(cfg: HarnessConfig) -> "Model":
    GeminiModel = _require("strands.models.gemini", "GeminiModel", "gemini")
    client_args: dict[str, Any] = {}
    if key := secrets_.first(("GOOGLE_API_KEY", "GEMINI_API_KEY")):
        client_args["api_key"] = key
    return GeminiModel(client_args=client_args, **model_kwargs(cfg, GeminiModel))


BUILDERS: dict[str, Callable[[HarnessConfig], "Model"]] = {
    "anthropic": _anthropic,
    "openai": _openai,
    "bedrock": _bedrock,
    "llamacpp": _llamacpp,
    "ollama": _ollama,
    "gemini": _gemini,
}


def _plugin(name: str) -> Callable[[HarnessConfig], "Model"] | None:
    """Resolve ``module:function``, or an installed entry point by name."""
    if ":" in name:
        module, _, attr = name.partition(":")
        try:
            return getattr(importlib.import_module(module), attr)
        except (ImportError, AttributeError) as err:
            raise ProviderError(f"Could not load provider {name!r}: {err}") from err
    for ep in entry_points(group=PLUGIN_GROUP, name=name):
        return ep.load()
    return None


def available() -> list[str]:
    """Built-in and installed plugin provider names."""
    return sorted({*BUILDERS, *(ep.name for ep in entry_points(group=PLUGIN_GROUP))})


def _timed_out(err: BaseException | None) -> bool:
    """Whether a model call failed by timing out (httpx's, or an SDK's wrapping of it)."""
    seen = 0
    while err is not None and seen < 10:
        if isinstance(err, TimeoutError) or "Timeout" in type(err).__name__:
            return True
        err, seen = err.__cause__ or err.__context__, seen + 1
    return False


# A connection that was refused, reset or cut mid-reply: what a server that is
# restarting looks like (httpx's names, and the SDKs' wrapping of them).
_UNREACHABLE = {"RemoteProtocolError", "ConnectError", "ReadError", "WriteError", "APIConnectionError"}


def _unreachable(err: BaseException | None) -> bool:
    """Whether a model call failed because the server could not be reached or went away mid-reply."""
    seen = 0
    while err is not None and seen < 10:
        if isinstance(err, ConnectionError) or type(err).__name__ in _UNREACHABLE:
            return True
        err, seen = err.__cause__ or err.__context__, seen + 1
    return False


def retry_strategy(cfg: HarnessConfig) -> Any:
    """Strands' retries, plus `model_retries` on timeouts and backoff on a server
    that went away, with every retry logged.

    Strands retries throttling (429, 503) with backoff and says so only at debug
    level, and does not retry a timeout or a dropped connection at all. A server
    that is restarting drops the call, then refuses connections, then answers
    503 while it loads the model; all three are backed off and retried, for up
    to about eight minutes. A model that stalls should be visible: each retry,
    and each giving up, gets a warning with how long the call had been running.
    """
    from strands.event_loop._retry import ModelRetryStrategy
    from strands.hooks.events import BeforeModelCallEvent
    from strands.types.exceptions import ModelThrottledException

    class LoggedRetries(ModelRetryStrategy):
        def __init__(self) -> None:
            super().__init__(max_attempts=max(8, cfg.model_retries + 1))
            self._timeouts = 0
            self._started = 0.0

        def register_hooks(self, registry: Any, **kwargs: Any) -> None:
            super().register_hooks(registry, **kwargs)
            registry.add_callback(BeforeModelCallEvent, self._before_model_call)

        def _before_model_call(self, event: Any) -> None:
            self._started = time.monotonic()

        def _reset_retry_state(self) -> None:
            super()._reset_retry_state()
            self._timeouts = 0

        def is_retryable(self, exception: Exception) -> bool:
            took = time.monotonic() - self._started if self._started else 0.0
            what = f"{type(exception).__name__}: {exception}".rstrip(": ")
            if _timed_out(exception):
                pass  # below; an httpx timeout is not a dropped connection
            elif isinstance(exception, ModelThrottledException) or _unreachable(exception):
                why = "throttled" if isinstance(exception, ModelThrottledException) else "lost the server"
                if self._current_attempt + 1 >= self._max_attempts:
                    logger.warning("Model call %s after %.0fs (%s); giving up after %d attempts.",
                                   why, took, what, self._max_attempts)
                else:
                    logger.warning("Model call %s after %.0fs (%s); retry %d of %d in %ds.", why, took, what,
                                   self._current_attempt + 1, self._max_attempts - 1,
                                   self._calculate_delay(self._current_attempt))
                return True
            if _timed_out(exception):
                if self._timeouts < cfg.model_retries:
                    self._timeouts += 1
                    logger.warning("Model call timed out (%s): nothing received for model_timeout=%ds, %.0fs "
                                   "into the call; retry %d of %d.", what, cfg.model_timeout, took,
                                   self._timeouts, cfg.model_retries)
                    return True
                logger.warning("Model call timed out (%s): nothing received for model_timeout=%ds, %.0fs into the "
                               "call; no retries left (model_retries=%d).", what, cfg.model_timeout, took,
                               cfg.model_retries)
            return False

    return LoggedRetries()


def build_model(cfg: HarnessConfig) -> "Model":
    """Construct the configured provider's model. Built-ins win over plugins."""
    builder = BUILDERS.get(cfg.provider) or _plugin(cfg.provider)
    if builder is None:
        raise ProviderError(
            f"Unknown provider {cfg.provider!r}. Available: {', '.join(available())}, "
            "or module:function."
        )
    return builder(cfg)
