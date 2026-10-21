"""Layered configuration: defaults -> user file -> repo .harness.toml -> env -> CLI."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

CONFIG_FILENAME = ".harness.toml"
USER_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "burst-harness" / "config.toml"

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


def _shaped(current: Any, value: Any, *, name: str, source: str) -> Any:
    """`value` in the type the field already has, or a ValueError naming it.

    A quoted number in a TOML file ("65536") is read as the number, as it would
    be from the environment; anything else of the wrong type is refused here
    rather than failing deep inside a run.
    """
    if isinstance(current, tuple):
        # TOML has no tuple type; match the shape of the existing value.
        if isinstance(value, (list, tuple)):
            return tuple(value)
        if isinstance(value, str):
            return _coerce(current, value, name=f"{name} in {source}")
    elif isinstance(current, bool):
        if isinstance(value, bool):
            return value
    elif isinstance(current, (int, float)):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and (isinstance(current, float) or isinstance(value, int)):
            return value
    elif isinstance(current, str):
        if isinstance(value, str):
            return value
    else:
        return value
    if isinstance(value, str) and not isinstance(current, str):
        return _coerce(current, value, name=f"{name} in {source}")
    raise ValueError(f"{name} in {source} must be {_kind(current)} (got {value!r}).")


def _kind(current: Any) -> str:
    if isinstance(current, bool):
        return "true or false"
    if isinstance(current, int):
        return "a whole number"
    if isinstance(current, float):
        return "a number"
    if isinstance(current, tuple):
        return "a list"
    return "a string"


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
    model_params: str = ""
    # Seconds to wait on a model request. Only llama.cpp needs it set: Strands'
    # LlamaCppModel defaults to 30s, and prompt prefill on a modest GPU can take
    # minutes. The other providers' SDKs already default to ~10 minutes.
    model_timeout: int = 600
    # How often a model request that timed out is tried again (0: give up at
    # the first). Throttling (429, 503) is retried by Strands' own backoff.
    # Every retry of either kind is logged.
    model_retries: int = 2
    model_id: str = "claude-opus-5-5"
    max_tokens: int = 32000
    effort: str = "high"
    thinking: bool = True
    # Strands falls back to 200K when this is unset, which would trip every
    # utilization gate at a fifth of the real window on a 1M-context model.
    context_window_limit: int = 1_000_000

    # --- which agent loop ---
    # strands: the harness's own Strands agent, driven from outside the target.
    # claude-code: Claude Code running inside the target, headless or in tmux.
    engine: str = "strands"

    # --- where work happens ---
    target: str = "local"  # local | docker | podman | kubectl | agent-sandbox
    # Clone this into the target rather than expecting a checkout to be there.
    # Required for agent-sandbox, whose sandboxes start empty.
    repo_url: str = ""
    pod: str = ""
    namespace: str = ""
    container: str = ""
    kube_context: str = ""
    # target=docker/podman: attach to this container, or else start one from
    # `image` for the run and remove it afterwards.
    docker_container: str = ""
    image: str = "ghcr.io/brianporeilly/burst-harness-sandbox:main"
    # Extra `run` flags for a started container (resource limits, --network...).
    container_args: str = ""
    # Run a started container as root, with the few capabilities a package
    # manager needs, so the agent can `dnf install`. Rootless podman only: there
    # root in the container is your own unprivileged user outside it.
    container_root: bool = False
    # Network access for a started container. "open": its normal network.
    # "log" and "allowlist": no route out at all except a gateway sidecar (see
    # gateway/), which records everything that passes. "log" lets it all
    # through; "allowlist" reaches only egress_allow (plus the repo's host and
    # ANTHROPIC_BASE_URL's), over HTTPS.
    egress: str = "open"
    gateway_image: str = "ghcr.io/brianporeilly/burst-harness-gateway:main"
    # Credentials the gateway adds on the way out, so the target never holds
    # them. Needs egress = log or allowlist.
    # "git": the driver's git token, on requests to the repo being worked on.
    # Without it the harness passes the token itself, per command.
    # "model": engine=claude-code's API key or token. The target is given
    # placeholders, made real at the gateway. Without it they are written to a
    # file in the target for the run.
    egress_inject: tuple[str, ...] = ()
    # A URL the gateway POSTs each refusal to, as JSON (an ntfy topic, a chat
    # webhook). Once per destination every few minutes.
    egress_webhook: str = ""
    egress_allow: tuple[str, ...] = (
        "api.anthropic.com",
        "github.com", "*.githubusercontent.com",
        "pypi.org", "files.pythonhosted.org",
        "registry.npmjs.org",
    )
    kubectl: str = "kubectl"
    # target=agent-sandbox (kubernetes-sigs/agent-sandbox)
    pool: str = ""
    claim_name: str = ""
    # Hard deadline on a claim: the controller deletes it after this even if the
    # driver died mid-burst. Longer than any burst, shorter than forgotten.
    claim_ttl: int = 4 * 3600
    # Keep the claim or started container after a terminal state (to inspect it).
    keep_claim: bool = False

    # --- engine=claude-code ---
    # Claude Code permission mode for headless runs: default | acceptEdits | plan.
    # What it may run beyond that comes from your settings (claude_settings).
    claude_permission_mode: str = "acceptEdits"
    # Driver-side JSON files merged into the target's ~/.claude.json and
    # ~/.claude/settings.json (over the image's defaults) before the run.
    claude_json: str = ""
    claude_settings: str = ""
    # Extra arguments for every `claude` invocation, e.g. "--model opus".
    claude_args: str = ""

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
    def load(
        cls,
        repo_toml: str | None = None,
        overrides: dict[str, Any] | None = None,
        *,
        user_config: Path | None = USER_CONFIG,
        job: dict[str, Any] | None = None,
    ) -> "HarnessConfig":
        """Build config in increasing order of specificity.

        defaults -> the user's config file -> the repo's .harness.toml ->
        HARNESS_* env vars -> CLI flags. Env beats the repo file so a deployment
        can override a repo's checked-in preference; CLI beats everything because
        it is the most specific.

        The repo file is limited to REPO_SETTABLE: it is written by whoever
        controls the repo, and settings like token_command or base_url would let
        it run commands on, or redirect credentials from, the driver.

        A job's settings (`job`, from its job.toml) go between the user's file
        and the repo's. They are not limited: a job is the user's own
        configuration, like their config file.
        """
        cfg = cls()
        if user_config is not None and user_config.is_file():
            cfg = cfg._merge(tomllib.loads(user_config.read_text()), source=str(user_config))
        if job:
            cfg = cfg._merge(job, source="job.toml")
        if repo_toml:
            values = tomllib.loads(repo_toml)
            if refused := sorted(set(values) - REPO_SETTABLE):
                raise ValueError(
                    f"{CONFIG_FILENAME} may not set: {', '.join(refused)}. Those are driver "
                    f"settings; put them in {USER_CONFIG}, HARNESS_* env vars, or flags."
                )
            cfg = cfg._merge(values, source=CONFIG_FILENAME)
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
            coerced[key] = _shaped(getattr(self, key), value, name=key, source=source)
        return replace(self, **coerced)

    def resolve_state_dir(self, name: str) -> Path:
        """Where snapshots and offloaded blobs live. Always harness-side.

        The conversation snapshot belongs to whoever runs the harness, not to the
        pod being driven -- a pod is disposable and the snapshot must outlive it.
        """
        return Path(self.state_dir).expanduser() / name


# What a repo's own .harness.toml may set: how work on that repo is done, never
# where it runs, what it authenticates with, or what the driver executes.
REPO_SETTABLE = frozenset({
    "model_id", "max_tokens", "effort", "thinking", "context_window_limit",
    "base_branch", "branch_prefix", "protected_branches", "remote", "pr_draft",
    "git_user_name", "git_user_email", "auto_push",
    "docs_dir", "doc_max_bytes", "skills_dir",
    "offload_max_result_tokens", "offload_preview_tokens", "window_size", "pin_first",
    "proactive_compression", "cache_ttl",
    "max_fixup_attempts", "require_clean_tree",
})
