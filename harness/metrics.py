"""A burst's numbers, for whatever launched it to read rather than scrape: HARNESS_RESULT's and HARNESS_JOB's
`metrics`, and each delegate's in `delegated`.

  schema               METRICS_SCHEMA; raised when a field changes meaning or goes, never when one is added
  seconds              the run's wall time (in `metrics` only, not a delegate's)
  model_seconds        time spent waiting on the model: each call, from sending it to its reply (or failure)
  tool_seconds         time spent running tools
  model_calls          model calls (Strands' event-loop cycles; Claude Code's turns)
  tool_calls           tool calls, and tool_errors those that failed
  tokens               {input, output, cache_read, cache_write}, the lead's own (a delegate's, in its entry). `input`
                       is as the provider counts it: the OpenAI-style ones (llama.cpp too) include the cached part
  peak_prompt_tokens   the largest single request's prompt, cached part included: how near the context limit it got
  cut_off              replies that ended at the output limit (and were discarded)
  window_drops         messages the sliding window dropped (each drop breaks the prompt cache's prefix)
  window_cuts          how many times it cut (a note says so where it did: agent.ChunkedWindow)
  repeats              replies that restated what recent ones said, the model told so (loops.Repeats)
  model_retries        model calls retried (a timeout, throttling); lost_server those of them for a server gone
  cost_usd             approximate, from PRICES; 0 for a local llama.cpp model; null when the price is not known.
                       In `metrics` it is the lead's and its delegates' together, null if any of them is not known
  fixups, handoffs     re-prompts for work left uncommitted (or a job's missing report); questions to the user
  checks               check_command before a PR: {run, failed}, or None when none ran
  setup                setup_command before the model: {exit, seconds}, or None when none ran
  review               review_with before the PR: {by, outcome}, or None when none ran

What is not known for an engine (Claude Code reports no timings) is null, never a guess. Nothing here is a
setting's value or a secret: `config` (cli._result_line) names the --config layers and a hash of the settings.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import weakref
from dataclasses import asdict
from pathlib import Path
from typing import Any

METRICS_SCHEMA = 1

# Approximate per-million-token input/output prices. Cache reads bill at ~0.1x input, writes at ~1.25x.
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5-1": (10.0, 50.0),
}
FREE = frozenset(("llamacpp",))  # a model run locally costs nothing per token

# Providers whose input count leaves cache reads out. The OpenAI-style ones
# (llama.cpp, OpenAI, Gemini) count the cached prefix in the prompt's tokens.
CACHE_OUTSIDE_INPUT = frozenset(("anthropic", "bedrock"))

COUNTERS = ("cut_off", "window_drops", "window_cuts", "repeats", "model_retries", "lost_server")

# What the harness counts itself, by agent, alongside Strands' own metrics.
_counted: "weakref.WeakKeyDictionary[Any, dict[str, int]]" = weakref.WeakKeyDictionary()


def _of(agent: Any) -> dict[str, Any] | None:
    try:
        return _counted.setdefault(agent, {**dict.fromkeys(COUNTERS, 0), "model_seconds": 0.0, "since": None})
    except TypeError:  # not weakly referenceable (a test's stand-in): nothing to keep it on
        return None


def count(agent: Any, name: str, by: int = 1) -> None:
    """Add to one of COUNTERS for this agent."""
    if (found := _of(agent)) is not None:
        found[name] += by


def watch(agent: Any) -> None:
    """Time this agent's model calls (model_seconds). Once per agent: agent.call() does it."""
    from strands.hooks.events import AfterModelCallEvent, BeforeModelCallEvent

    def before(event: Any) -> None:
        if (found := _of(event.agent)) is not None:
            found["since"] = time.monotonic()

    def after(event: Any) -> None:
        if (found := _of(event.agent)) is not None and found["since"] is not None:
            found["model_seconds"] += time.monotonic() - found["since"]
            found["since"] = None

    agent.hooks.add_callback(BeforeModelCallEvent, before)
    agent.hooks.add_callback(AfterModelCallEvent, after)


def cost(usage: dict[str, Any], model_id: str, provider: str) -> float | None:
    """Approximate dollars for Strands-style usage, or None when the model's price is not known."""
    if provider in FREE:
        return 0.0
    price = PRICES.get(model_id)
    if price is None:
        return None
    inp, out = usage.get("inputTokens", 0) or 0, usage.get("outputTokens", 0) or 0
    cache_read = usage.get("cacheReadInputTokens", 0) or 0
    cache_write = usage.get("cacheWriteInputTokens", 0) or 0
    if provider not in CACHE_OUTSIDE_INPUT:
        inp = max(inp - cache_read, 0)  # the uncached part
    return (inp * price[0] + cache_read * price[0] * 0.1 + cache_write * price[0] * 1.25 + out * price[1]) / 1e6


def tokens(usage: dict[str, Any]) -> dict[str, int]:
    return {"input": usage.get("inputTokens", 0) or 0, "output": usage.get("outputTokens", 0) or 0,
            "cache_read": usage.get("cacheReadInputTokens", 0) or 0,
            "cache_write": usage.get("cacheWriteInputTokens", 0) or 0}


def _prompt(usage: dict[str, Any], provider: str) -> int:
    """One request's whole prompt, cached part included."""
    inp = usage.get("inputTokens", 0) or 0
    if provider in CACHE_OUTSIDE_INPUT:
        inp += (usage.get("cacheReadInputTokens", 0) or 0) + (usage.get("cacheWriteInputTokens", 0) or 0)
    return inp


def of_agent(agent: Any, model_id: str, provider: str) -> dict[str, Any]:
    """A Strands agent's numbers so far (all its calls: a burst's, or a delegate's)."""
    loop = agent.event_loop_metrics
    usage = dict(loop.accumulated_usage)
    tools = list(loop.tool_metrics.values())
    counted = _counted.get(agent) or {}
    peak = max((_prompt(dict(cycle.usage), provider) for run in loop.agent_invocations for cycle in run.cycles),
               default=0)
    return {
        "model_seconds": round(counted.get("model_seconds", 0.0), 1),
        "tool_seconds": round(sum(t.total_time for t in tools), 1),
        "model_calls": loop.cycle_count,
        "tool_calls": sum(t.call_count for t in tools),
        "tool_errors": sum(t.error_count for t in tools),
        "tokens": tokens(usage),
        "peak_prompt_tokens": peak,
        **{name: counted.get(name, 0) for name in COUNTERS},
        "cost_usd": cost(usage, model_id, provider),
    }


def add(total: dict[str, Any] | None, more: dict[str, Any]) -> dict[str, Any]:
    """Two runs' numbers together: a delegate called more than once."""
    if not total:
        return {**more, "tokens": dict(more.get("tokens") or {})}
    summed = dict(total)
    for key, value in more.items():
        if key == "tokens":
            summed["tokens"] = {k: (total.get("tokens") or {}).get(k, 0) + v for k, v in value.items()}
        elif key == "peak_prompt_tokens":
            summed[key] = max(total.get(key) or 0, value or 0)
        elif key == "cost_usd":
            summed[key] = None if value is None or total.get(key) is None else total[key] + value
        elif isinstance(value, int | float) and isinstance(total.get(key), int | float):
            summed[key] = round(total[key] + value, 1) if isinstance(value, float) else total[key] + value
        else:
            summed[key] = value if total.get(key) is None else total[key]
    return summed


def burst(lead: dict[str, Any] | None, delegated: dict[str, dict[str, Any]], *, seconds: float, fixups: int,
          handoffs: int, checks: dict[str, int] | None = None,
          setup: dict[str, Any] | None = None, review: dict[str, Any] | None = None) -> dict[str, Any]:
    """`metrics` for a run: the lead's numbers (None when the engine recorded none), its wall time and loop
    counts, and the cost of the lead and its delegates together."""
    lead = lead or {}
    standard = ("model_seconds", "tool_seconds", "model_calls", "tool_calls", "tool_errors", "tokens",
                "peak_prompt_tokens", *COUNTERS)
    parts = [lead.get("cost_usd") if lead else None,
             *((used.get("metrics") or {}).get("cost_usd") for used in delegated.values())]
    return {
        "schema": METRICS_SCHEMA,
        "seconds": round(seconds, 1),
        **{key: lead.get(key) for key in standard},
        "cost_usd": None if any(part is None for part in parts) else round(sum(parts), 4),
        "fixups": fixups,
        "handoffs": handoffs,
        # check_command: how often it ran before a PR, and failed
        "checks": {"run": checks.get("run", 0), "failed": checks.get("failed", 0)} if checks else None,
        "setup": setup or None,  # setup_command: its exit code and seconds
        # review_with before the PR: who reviewed, and what came of it (findings, no findings, failed)
        "review": {"by": review["by"], "outcome": review["outcome"]} if review else None,
        # What only an engine reports (Claude Code's: per model, the plan; claude_usage.py), as it reports it.
        **{key: value for key, value in lead.items() if key not in standard and key != "cost_usd"},
    }


def config_label(cfg: Any) -> dict[str, Any]:
    """Which harness variant ran: the --config layers by name, and a hash of the settings that shape the agent's
    work, so two runs under the same names but different settings are not compared by mistake. Where the run
    goes (target, sandbox, repo) and what it authenticates with are left out of the hash."""
    layers = [Path(p).stem for p in filter(None, os.environ.get("HARNESS_CONFIG", "").split(os.pathsep))]
    settings = {key: value for key, value in sorted(asdict(cfg).items()) if key in SHAPING}
    digest = hashlib.sha256(json.dumps(settings, sort_keys=True, default=str).encode()).hexdigest()
    return {"layers": layers, "hash": digest[:16]}


# The settings that shape how the agent works, for config_label's hash.
SHAPING = frozenset({
    "engine", "provider", "model_id", "model_params", "max_tokens", "effort", "thinking", "context_window_limit",
    "model_timeout", "model_retries", "agents", "delegate_to", "router", "mcp",
    "docs_dir", "doc_max_bytes", "skills_dir", "repo_instructions", "repo_instructions_max_bytes", "resume_from",
    "offload_max_result_tokens", "offload_preview_tokens",
    "window_size", "pin_first", "proactive_compression", "cache_ttl", "max_fixup_attempts", "require_clean_tree",
    "check_command", "check_timeout", "setup_command", "setup_timeout",
    "review_with", "review_max_minutes", "review_max_calls",
    "loop_warn", "loop_stop", "repeat_notice",
})
