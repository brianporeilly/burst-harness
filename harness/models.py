"""Model provider selection.

Pick with HARNESS_PROVIDER: a built-in name (see BUILDERS), the name of an
installed plugin, or ``module:function``. A plugin is any callable taking a
HarnessConfig and returning a Strands Model, registered under the
``burst_harness.providers`` entry-point group::

    [project.entry-points."burst_harness.providers"]
    myproxy = "myproxy_harness:build"

A plugin should build its kwargs with :func:`model_kwargs` so it gets the same
schema fitting, sampling params, and prompt caching as the built-ins.

A plugin may say more about itself with optional attributes on the callable
(see :func:`provider_attr`); a plain function needs none, and the built-ins set
none:

* ``credentials``: the names of the environment variables (or the
  orchestrator's stored credentials) it reads, such as a gateway's client id
  and secret. A run the orchestrator starts is handed only the credentials it
  is granted (harness/credentials.py), and these are granted to a run that may
  use the provider: its own, or a profile's. They can be set in the
  orchestrator's Settings tab too::

      def build(cfg): ...
      build.credentials = ("EXAMPLE_CLIENT_ID", "EXAMPLE_CLIENT_SECRET")

* ``target_endpoint``: a callable taking the HarnessConfig and returning a
  :class:`TargetEndpoint`, for an engine whose agent loop runs in the target
  (engine=claude-code, on a container or a claimed sandbox). Claude Code
  there talks to that endpoint, with the endpoint's token, in place of the
  driver's Anthropic settings; the token is kept current while it runs (see
  claude_code.py)::

      build.target_endpoint = lambda cfg: TargetEndpoint(
          base_url="https://ai.example.com/anthropic", token=my_token)

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
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover
    from strands.models.model import Model

from . import credentials as secrets_
from .auth import credentials
from .config import HarnessConfig

PLUGIN_GROUP = "burst_harness.providers"

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """The provider is unknown, or its optional dependency is not installed."""


@dataclass(frozen=True)
class TargetEndpoint:
    """Where an engine running in the target (Claude Code) sends its model calls, for a provider plugin.

    `token` has the shape of an Anthropic ``credentials=`` provider: called (with
    ``force_refresh`` as a keyword, which it may ignore), it returns an
    AccessToken, whose ``expires_at`` (epoch seconds, or None) says when to ask
    again; a plain string is taken as a token that does not expire. `env` is
    set in the target as it is, so it holds nothing secret.
    """

    base_url: str  # the Anthropic Messages API's root, as ANTHROPIC_BASE_URL takes it
    token: Callable[..., Any]
    env: Mapping[str, str] = field(default_factory=dict)

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""


# Each built-in provider's model class, and the extra that installs its SDK.
REQUIRES = {
    "anthropic": ("strands.models.anthropic", "AnthropicModel", "anthropic"),
    "openai": ("strands.models.openai", "OpenAIModel", "openai"),
    "bedrock": ("strands.models.bedrock", "BedrockModel", "bedrock"),
    "llamacpp": ("strands.models.llamacpp", "LlamaCppModel", "llamacpp"),
    "ollama": ("strands.models.ollama", "OllamaModel", "ollama"),
    "gemini": ("strands.models.gemini", "GeminiModel", "gemini"),
}


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


# Request params that cap one reply's length, for providers that take it there rather than as a setting.
OUTPUT_CAPS = ("max_tokens", "max_completion_tokens", "n_predict")


def model_kwargs(cfg: HarnessConfig, model_cls: Any, *, messages_api: bool = False,
                 cap_in_params: bool = False) -> dict[str, Any]:
    """Build provider kwargs against that provider's own schema.

    ``messages_api`` says the endpoint speaks the Anthropic Messages API, which
    turns on effort, adaptive thinking, and prompt caching.

    Providers disagree about where settings live -- Bedrock has no ``params`` and
    takes ``temperature`` at the top level; llama.cpp has no ``max_tokens``. Strands
    only *warns* about unknown keys and then ignores them, so passing one blanket
    set of kwargs silently drops settings. Anything unsupported is reported here
    rather than vanishing.

    ``cap_in_params`` says the endpoint takes ``max_tokens`` as a request param
    (OpenAI-compatible servers, llama-server), which Strands has no setting for:
    it goes there, at most half the context window, unless model_params caps
    replies itself. Without a cap, a model looping in its reasoning runs on
    until the server's own limit, if it has one.
    """
    keys = _config_keys(model_cls)
    overrides = _json_overrides(cfg)
    kwargs: dict[str, Any] = {"model_id": cfg.model_id}
    if "context_window_limit" in keys:
        kwargs["context_window_limit"] = cfg.context_window_limit
    if "max_tokens" in keys:
        kwargs["max_tokens"] = cfg.max_tokens
    elif cap_in_params and cfg.max_tokens:
        if not set(OUTPUT_CAPS) & set(overrides):
            overrides = {"max_tokens": min(cfg.max_tokens, cfg.context_window_limit // 2), **overrides}
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
    AnthropicModel = _require(*REQUIRES["anthropic"])
    client_args: dict[str, Any] = {}
    if cfg.base_url:
        client_args["base_url"] = cfg.base_url
    if provider := credentials(cfg):
        client_args["credentials"] = provider
    elif key := secrets_.get("ANTHROPIC_API_KEY"):
        client_args["api_key"] = key
    return AnthropicModel(client_args=client_args, **model_kwargs(cfg, AnthropicModel, messages_api=True))


class _ReasoningWarnedOnce(logging.Filter):
    """Strands' OpenAI model warns, on every request, once for each message in the history that
    holds reasoning, then drops the reasoning (Chat Completions has no place for it): by a run's
    fiftieth call, fifty lines a request. The first is kept, saying so; the rest are dropped."""

    MESSAGE = "reasoningContent is not supported"

    def __init__(self) -> None:
        super().__init__()
        self.said = False

    def filter(self, record: logging.LogRecord) -> bool:
        if self.MESSAGE not in record.getMessage():
            return True
        if self.said:
            return False
        self.said = True
        record.msg = f"{record.getMessage()} It is left out of what is sent; said once, not for every message."
        record.args = None
        return True


def _openai(cfg: HarnessConfig) -> "Model":
    """Any OpenAI-compatible endpoint: OpenAI, vLLM, LM Studio, llama-server's /v1."""
    OpenAIModel = _require(*REQUIRES["openai"])
    strands_openai = logging.getLogger("strands.models.openai")
    if not any(isinstance(found, _ReasoningWarnedOnce) for found in strands_openai.filters):
        strands_openai.addFilter(_ReasoningWarnedOnce())
    base_url = cfg.base_url or os.environ.get("OPENAI_BASE_URL", "")
    api_key = secrets_.get("OPENAI_API_KEY")
    client_args: dict[str, Any] = {"api_key": api_key or "not-needed"}
    if base_url:
        client_args["base_url"] = base_url
    return OpenAIModel(client_args=client_args, **model_kwargs(cfg, OpenAIModel, cap_in_params=True))


def _bedrock(cfg: HarnessConfig) -> "Model":
    BedrockModel = _require(*REQUIRES["bedrock"])
    kwargs = model_kwargs(cfg, BedrockModel)
    if region := os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"):
        kwargs["region_name"] = region
    return BedrockModel(**kwargs)


def llamacpp_url(cfg: HarnessConfig) -> str:
    """Where a llamacpp model is served."""
    return cfg.base_url or os.environ.get("LLAMACPP_BASE_URL", "http://localhost:8080")



def _llamacpp(cfg: HarnessConfig) -> "Model":
    """A self-hosted llama.cpp server."""
    LlamaCppModel = _require(*REQUIRES["llamacpp"])
    cfg = replace(cfg, model_id=model_name(cfg))
    return _passing_through(LlamaCppModel)(base_url=llamacpp_url(cfg), timeout=cfg.model_timeout,
                                           **model_kwargs(cfg, LlamaCppModel, cap_in_params=True))


# What a llama.cpp server said it serves, by its URL: (when asked, the name).
_SERVED: dict[str, tuple[float, str]] = {}
SERVED_FOR = 60  # seconds before it is asked again: the server may have been restarted with another model


def model_name(cfg: HarnessConfig) -> str:
    """The model `cfg` uses, as it is sent and shown.

    That is model_id, except for a llama.cpp server with no model chosen: the
    harness's default is a Claude model, so the server is asked what it serves
    instead (a model switcher in front of llama.cpp needs the right name;
    llama.cpp itself answers to any). Only the file's name is kept, as llama.cpp
    names its model by the file's path; "llama.cpp" if it cannot be asked.
    """
    if cfg.provider != "llamacpp" or cfg.model_id != HarnessConfig.model_id:
        return cfg.model_id
    url = llamacpp_url(cfg).rstrip("/")
    # Never asked is not asked just now: a monotonic clock may start near 0, on a machine just started.
    known = _SERVED.get(url)
    if known is not None and time.monotonic() - known[0] <= SERVED_FOR:
        name = known[1]
    else:
        from .session import served_model
        name = (served_model(url + "/v1") or "llama.cpp").rsplit("/", 1)[-1].removesuffix(".gguf")
        _SERVED[url] = (time.monotonic(), name)
    return name


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
    OllamaModel = _require(*REQUIRES["ollama"])
    host = cfg.base_url or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    return OllamaModel(host=host, **model_kwargs(cfg, OllamaModel))


def _gemini(cfg: HarnessConfig) -> "Model":
    GeminiModel = _require(*REQUIRES["gemini"])
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


def provider_attr(name: str, attr: str, default: Any = None) -> Any:
    """A plugin provider's optional attribute (see the module's docstring), or `default`: for a built-in,
    a plugin without it, and a provider that is unknown or cannot be loaded (build_model says why)."""
    if name in BUILDERS:
        return default
    try:
        builder = _plugin(name)
    except ProviderError:
        return default
    return getattr(builder, attr, default) if builder is not None else default


def target_endpoint(cfg: HarnessConfig) -> TargetEndpoint | None:
    """The run's provider's TargetEndpoint, or None when it has none (every built-in)."""
    make = provider_attr(cfg.provider, "target_endpoint")
    if make is None:
        return None
    endpoint = make(cfg) if callable(make) else make
    if endpoint is None:
        return None
    if not isinstance(endpoint, TargetEndpoint) or not callable(endpoint.token):
        raise ProviderError(f"Provider {cfg.provider!r}: target_endpoint must give a models.TargetEndpoint.")
    parts = urlsplit(endpoint.base_url)
    if parts.scheme != "https" or not parts.hostname:
        raise ProviderError(f"Provider {cfg.provider!r}: its target endpoint must be https:// "
                            f"(got {endpoint.base_url!r}).")
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in endpoint.env.items()):
        raise ProviderError(f"Provider {cfg.provider!r}: its target endpoint's env must map strings to strings.")
    return endpoint


def _declared(name: str) -> tuple[str, ...]:
    names = provider_attr(name, "credentials", ())
    if isinstance(names, str) or not isinstance(names, (tuple, list)):
        return ()
    return tuple(n for n in names if isinstance(n, str) and n)


def credentials_for(cfg: HarnessConfig) -> list[str]:
    """The credentials the providers a run may use declare: its own, and every profile's (a router, a
    delegation or --agent may pick any). A profile without a provider has the default, not the run's."""
    providers = {cfg.provider, *(str(profile.get("provider") or HarnessConfig().provider)
                                 for profile in cfg.agents.values() if isinstance(profile, dict))}
    return list(dict.fromkeys(name for provider in sorted(providers) for name in _declared(provider)))


def plugin_credentials() -> dict[str, list[str]]:
    """Each credential an installed plugin declares, with the plugins that declare it."""
    found: dict[str, list[str]] = {}
    for provider in available():
        for name in _declared(provider):
            found.setdefault(name, []).append(provider)
    return found


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


def check_provider(name: str) -> None:
    """Raise ProviderError unless `name` could build a model here: a built-in whose SDK is installed, or
    a plugin that loads. What build_model would fail on later, found before a run starts."""
    if name in BUILDERS:
        _require(*REQUIRES[name])
    elif _plugin(name) is None:
        raise ProviderError(f"Unknown provider {name!r}. Available: {', '.join(available())}, or module:function.")


def build_model(cfg: HarnessConfig) -> "Model":
    """Construct the configured provider's model. Built-ins win over plugins."""
    builder = BUILDERS.get(cfg.provider) or _plugin(cfg.provider)
    if builder is None:
        raise ProviderError(
            f"Unknown provider {cfg.provider!r}. Available: {', '.join(available())}, "
            "or module:function."
        )
    return builder(cfg)
