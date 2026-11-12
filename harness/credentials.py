"""Credentials: where the harness and the orchestrator get secrets, and what each run is handed.

Everything that needs a token or key asks `get(NAME)`, never the environment
directly. The answer is, in order: an `overlay()` around the code asking (a
request, a run; where per-user credentials go once there are users), then
what `use_stored()` was given (the orchestrator's Settings: values set in its
page), then the process's environment. The callers do not change.

A run the orchestrator starts (a task's burst, a job's run) is a subprocess.
It gets `for_run(...)`: the orchestrator's environment with every credential
taken out, and then only the ones that run is granted put back. A task's
burst gets the git token and the model's keys, not a job's Slack token or the
cluster watcher's KUBE_TOKEN; a job gets what its job.toml names. Anything
that looks like a credential counts (a name ending in _TOKEN, _KEY, _SECRET,
_PASSWORD..., and those listed below), so a new one is withheld until it is
granted. A provider plugin's own credentials are granted to the runs that may
use it, when it declares them (harness/models.py). PASS_ENV names more to pass
to every run.
"""

from __future__ import annotations

import contextlib
import functools
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import HarnessConfig

GIT = ("HARNESS_GIT_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
MODEL = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK",
    "GOOGLE_APPLICATION_CREDENTIALS",
)
GATEWAY = ("HARNESS_GATEWAY_TOKEN",)
SLACK = ("SLACK_WEBHOOK_URL", "SLACK_BOT_TOKEN")
# Names that are credentials though they do not look like one.
KNOWN = frozenset((*GIT, *MODEL, *GATEWAY, *SLACK, "AWS_ACCESS_KEY_ID"))
LOOKS_SECRET = re.compile(r"(?:^|_)(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD|PASS|CREDENTIALS?|AUTH)(?:_|$)")
PASS_ENV = "ORCHESTRATOR_PASS_ENV"

_overlay: ContextVar[Mapping[str, str]] = ContextVar("credentials_overlay", default={})
# For the whole process, every thread: the orchestrator's stored credentials.
_stored: Mapping[str, str] = {}


def get(name: str) -> str:
    """A credential's value, or "" if there is none: the overlay's, else the stored, else the environment's."""
    layer = _overlay.get()
    if name in layer:
        return layer[name].strip()
    if name in _stored:
        return _stored[name].strip()
    return os.environ.get(name, "").strip()


def source(name: str) -> str:
    """Where get(name) finds a value: "stored", "environment", or "" for nowhere."""
    if _stored.get(name, "").strip():
        return "stored"
    return "environment" if os.environ.get(name, "").strip() else ""


def use_stored(values: Mapping[str, str]) -> None:
    """Put these in front of the environment's values, for the whole process, replacing what was there."""
    global _stored
    _stored = dict(values)


def first(names: Iterable[str]) -> str:
    """The first of these that has a value, or ""."""
    return next((value for name in names if (value := get(name))), "")


@contextlib.contextmanager
def overlay(values: Mapping[str, str]) -> Iterator[None]:
    """Use these values in front of the environment's, for the code inside (and its threads' copies
    of the context). An empty string hides the environment's value."""
    token = _overlay.set({**_overlay.get(), **values})
    try:
        yield
    finally:
        _overlay.reset(token)


def is_secret(name: str) -> bool:
    """A known credential, or a name that looks like one and is not one of the harness's own
    settings (HARNESS_TOKEN_COMMAND is a command, not a token)."""
    return name in KNOWN or (bool(LOOKS_SECRET.search(name.upper())) and name not in _settings())


@functools.cache
def _settings() -> frozenset[str]:
    from .config import HarnessConfig  # config reads MCP settings, and mcp asks this module for keys

    return frozenset(HarnessConfig.env_var_names())


def scrubbed() -> dict[str, str]:
    """The process's environment without any credential, except those PASS_ENV names."""
    passed = passed_names()
    return {k: v for k, v in os.environ.items() if not is_secret(k) or k in passed}


def passed_names() -> set[str]:
    return {n.strip() for n in os.environ.get(PASS_ENV, "").split(",") if n.strip()}


def for_task(cfg: HarnessConfig) -> list[str]:
    """What a task's burst is granted: the git token (the driver pushes and opens the PR), the
    model's keys (with those its providers' plugins declare), the cluster gateway's token, and the
    keys of its MCP servers."""
    from . import models  # asks this module for keys

    return [*GIT, *forge_tokens(cfg), *MODEL, *models.credentials_for(cfg), *GATEWAY, *mcp_keys(cfg)]


def forge_tokens(cfg: HarnessConfig) -> list[str]:
    """The tokens of the forges besides GitHub (forge.py): a task's repo may be on any of them."""
    from . import forge

    return [var for found in forge.forges(cfg) if found.kind != "github" for var in found.token_vars]


def for_job(cfg: HarnessConfig, secrets: Iterable[str], outputs: Iterable[str]) -> list[str]:
    """What a job's run is granted: the model's keys, the gateway's token, its MCP servers' keys,
    the secrets its job.toml names, and Slack's if it posts there. Not the git token: a job that
    needs one names it."""
    from . import models

    slack = [*SLACK] if any(o == "slack" or o.startswith("slack:") for o in outputs) else []
    return [*MODEL, *models.credentials_for(cfg), *GATEWAY, *mcp_keys(cfg), *secrets, *slack]


def mcp_keys(cfg: HarnessConfig) -> list[str]:
    """Both credentials of each MCP server, a bearer key and Basic's user:secret: whichever is set is passed."""
    from . import mcp

    return [var for name in mcp.servers(cfg) for var in mcp.key_vars(name)]


def for_run(granted: Iterable[str], extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """A subprocess's environment: scrubbed, then the granted credentials that have a value."""
    env = scrubbed()
    for name in granted:
        if value := get(name):
            env[name] = value
    return {**env, **(extra or {})}
