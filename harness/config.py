"""Layered configuration: dataclass defaults -> repo .harness.toml -> CLI overrides."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

CONFIG_FILENAME = ".harness.toml"

# Every field is settable as HARNESS_<FIELD_NAME_UPPERCASED>, which is how a
# container image gets configured: the CLI subcommand is the entrypoint, and
# everything else arrives through the pod spec.
ENV_PREFIX = "HARNESS_"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _coerce(current: Any, raw: str, *, name: str) -> Any:
    """Parse an environment string into the shape the field already has."""
    # bool before int: bool is a subclass of int.
    if isinstance(current, bool):
        lowered = raw.strip().lower()
        if lowered in _TRUTHY:
            return True
        if lowered in _FALSY:
            return False
        raise ValueError(f"{name} must be a boolean (got {raw!r}).")
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError as err:
            raise ValueError(f"{name} must be an integer (got {raw!r}).") from err
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError as err:
            raise ValueError(f"{name} must be a number (got {raw!r}).") from err
    if isinstance(current, tuple):
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    return raw


@dataclass(frozen=True)
class HarnessConfig:
    """Every knob the harness exposes. Add a field here and it is automatically
    settable from .harness.toml; add a flag in cli.py to expose it per-run."""

    # --- model ---
    # A built-in name, an installed plugin's entry-point name, or module:function.
    provider: str = "anthropic"
    # Endpoint override. Empty means the provider's own default.
    base_url: str = ""
    # Bearer-token source for an Anthropic-compatible proxy, in place of an API
    # key. A command whose stdout is the token, or a file holding it.
    token_command: str = ""
    token_file: str = ""
    # Raw JSON merged into the provider's sampling params, last. The escape
    # hatch for any knob the harness does not model (temperature, top_p, ...).
    model_params_json: str = ""
    model_id: str = "claude-opus-5-5"
    max_tokens: int = 32000
    effort: str = "high"
    thinking: bool = True
    # Strands falls back to 200K when this is unset, which would trip every
    # utilization gate at a fifth of the real window on a 1M-context model.
    context_window_limit: int = 1_000_000

    # --- where work happens ---
    target: str = "local"
    pod: str = ""
    namespace: str = ""
    container: str = ""
    kube_context: str = ""
    docker_container: str = ""
    kubectl: str = "kubectl"
    # target=sandbox (kubernetes-sigs/agent-sandbox)
    warmpool: str = ""
    claim_name: str = ""
    sandbox_connection: str = "local-tunnel"
    gateway_name: str = ""
    command_timeout: int = 900

    # --- git / PR ---
    base_branch: str = "main"
    branch_prefix: str = "harness/"
    protected_branches: tuple[str, ...] = ("main", "master")
    remote: str = "origin"
    pr_draft: bool = False
    # A fresh container has no git identity, so `git commit` fails outright.
    # Empty means "use whatever git already resolves".
    git_user_name: str = ""
    git_user_email: str = ""
    auto_push: bool = True

    # --- state ---
    docs_dir: str = "docs/tasks"
    doc_max_bytes: int = 6144
    # Absolute by default so the harness never writes into the target repo and
    # snapshots can't be committed by accident. A relative value resolves
    # against the repo root instead.
    state_dir: str = "~/.burst-harness"
    skills_dir: str = ".harness/skills"

    # --- context / tokens ---
    offload_max_result_tokens: int = 2500
    offload_preview_tokens: int = 1000
    window_size: int = 40
    pin_first: int = 2
    proactive_compression: bool = True
    cache_ttl: str = "1h"

    # --- loop ---
    max_fixup_attempts: int = 3
    require_clean_tree: bool = True

    @classmethod
    def load(cls, repo: Path, overrides: dict[str, Any] | None = None) -> "HarnessConfig":
        """Build config in increasing order of specificity.

        defaults -> the repo's .harness.toml -> HARNESS_* env vars -> CLI flags.
        Env beats the repo file so a deployment can override a repo's checked-in
        preference; CLI beats everything because it is the most specific.
        """
        cfg = cls()
        path = repo / CONFIG_FILENAME
        if path.is_file():
            cfg = cfg._merge(tomllib.loads(path.read_text()), source=str(path))
        cfg = cfg._merge(cfg._env_values(), source="environment")
        if overrides:
            cfg = cfg._merge({k: v for k, v in overrides.items() if v is not None}, source="command line")
        return cfg

    def _env_values(self) -> dict[str, Any]:
        """Read HARNESS_* overrides, coerced to each field's type."""
        found: dict[str, Any] = {}
        for field in fields(self):
            raw = os.environ.get(ENV_PREFIX + field.name.upper())
            if raw is not None:
                found[field.name] = _coerce(getattr(self, field.name), raw, name=ENV_PREFIX + field.name.upper())
        return found

    @classmethod
    def env_var_names(cls) -> list[str]:
        """Every recognised environment variable, for documentation and --help."""
        return [ENV_PREFIX + f.name.upper() for f in fields(cls())]

    def _merge(self, values: dict[str, Any], *, source: str) -> "HarnessConfig":
        known = {f.name: f for f in fields(self)}
        unknown = set(values) - set(known)
        if unknown:
            raise ValueError(
                f"Unknown setting(s) in {source}: {', '.join(sorted(unknown))}. "
                f"Valid settings: {', '.join(sorted(known))}"
            )
        coerced: dict[str, Any] = {}
        for key, value in values.items():
            # TOML has no tuple type; match the shape of the existing value.
            current = getattr(self, key)
            coerced[key] = tuple(value) if isinstance(current, tuple) and isinstance(value, list) else value
        return replace(self, **coerced)

    def resolve_state_dir(self, name: str) -> Path:
        """Where snapshots and offloaded blobs live. Always harness-side.

        The conversation snapshot belongs to whoever runs the harness, not to the
        pod being driven -- a pod is disposable and the snapshot must outlive it.
        """
        return Path(self.state_dir).expanduser() / name
