"""What Claude Code says of its own usage, beyond its result's totals: for HARNESS_RESULT's `metrics`.

Read from its stream-json (claude_code.ClaudeCodeEngine._on_line):
  assistant messages   each call's `usage`: the largest prompt (its input, cache reads and cache writes)
  rate_limit_event     the plan's state: `unifiedWindows`, each window's utilization (0-1), reset time and
                       status (five_hour, seven_day...), and whether overage is in use. Sent as a run starts
                       and when it changes; on an API key, never
  result               per model (`model_usage_since_last_result`, else `modelUsage`): tokens, web searches,
                       cost and context window; why it stopped (`terminal_reason`); an API error's status;
                       the subagents it spawned

Its cost is at list prices (`costBasis`): on a subscription what the same tokens would cost on the API, not
what is spent, but a fixed yardstick. The plan's utilization is what measures a subscription's limits: `plan`
says each window's at the burst's start and end, so the same work over time can be compared by both.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


class ClaudeUsage:
    def __init__(self) -> None:
        self.models: dict[str, dict[str, Any]] = {}
        self.peak = 0
        self.first: dict[str, Any] | None = None  # the plan's state as first seen, and as last seen
        self.last: dict[str, Any] | None = None
        self.terminal_reason: str | None = None
        self.api_errors = 0
        self.subagents = 0

    def message(self, usage: dict[str, Any]) -> None:
        """An assistant message's usage: one call's prompt."""
        prompt = sum(int(usage.get(key) or 0) for key in
                     ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        self.peak = max(self.peak, prompt)

    def rate_limit(self, info: dict[str, Any]) -> None:
        windows = info.get("unifiedWindows")
        if not isinstance(windows, dict) or not windows:  # an older Claude Code: the one window it names
            kind = info.get("rateLimitType")
            windows = {kind: {k: info.get(k) for k in ("utilization", "resetsAt", "status")}} if kind else {}
        state = {"windows": {name: w for name, w in windows.items() if isinstance(w, dict)},
                 "overage": {"status": info.get("overageStatus"), "using": bool(info.get("isUsingOverage")),
                             **({"disabled": info["overageDisabledReason"]} if info.get("overageDisabledReason")
                                else {})}}
        if self.first is None:
            self.first = state
        self.last = state

    def result(self, event: dict[str, Any]) -> None:
        per_model = event.get("model_usage_since_last_result")
        if not isinstance(per_model, dict):
            per_model = event.get("modelUsage") if isinstance(event.get("modelUsage"), dict) else {}
        for model, used in per_model.items():
            if not isinstance(used, dict):
                continue
            entry = self.models.setdefault(model, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
                                                   "web_searches": 0, "cost_usd": 0.0})
            for key, theirs in (("input", "inputTokens"), ("output", "outputTokens"),
                                ("cache_read", "cacheReadInputTokens"), ("cache_write", "cacheCreationInputTokens"),
                                ("web_searches", "webSearchRequests")):
                entry[key] += int(used.get(theirs) or 0)
            entry["cost_usd"] = round(entry["cost_usd"] + float(used.get("costUSD") or 0), 6)
            if used.get("contextWindow"):
                entry["context_window"] = used["contextWindow"]
            if used.get("costBasis"):
                entry["cost_basis"] = used["costBasis"]
        self.terminal_reason = event.get("terminal_reason") or self.terminal_reason
        if event.get("api_error_status"):
            self.api_errors += 1
        stats = event.get("subagent_stats")
        if isinstance(stats, dict):
            self.subagents += int(stats.get("spawned") or 0)

    def plan(self) -> dict[str, Any] | None:
        """Each window's utilization at the burst's start and end (as percentages), its reset time and status;
        and overage. None when Claude Code said nothing of a plan (an API key)."""
        if self.last is None:
            return None
        first = (self.first or {}).get("windows", {})
        windows = {}
        for name, now in self.last["windows"].items():
            then = first.get(name) or {}
            windows[name] = {"start": _percent(then.get("utilization")), "end": _percent(now.get("utilization")),
                             "status": now.get("status"), "resets": _when(now.get("resetsAt"))}
        return {**windows, "overage": self.last["overage"]}

    def numbers(self) -> dict[str, Any]:
        """What goes into the lead's metrics beside its totals."""
        windows = [used.get("context_window") for used in self.models.values() if used.get("context_window")]
        return {"peak_prompt_tokens": self.peak or None, "context_window": max(windows) if windows else None,
                "model_usage": self.models or None, "plan": self.plan(), "terminal_reason": self.terminal_reason,
                "api_errors": self.api_errors, "subagents": self.subagents}


def _percent(value: Any) -> float | None:
    return round(float(value) * 100, 1) if isinstance(value, int | float) else None


def _when(value: Any) -> str | None:
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds") if isinstance(value, int | float) else None
