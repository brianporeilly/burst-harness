"""Claude Code's usage beyond its totals: per model, the plan's utilization, the largest prompt. The events are
shaped as Claude Code 2.1.323 sends them in stream-json."""

import json

from harness import metrics
from harness.claude_code import ClaudeCodeEngine
from harness.config import HarnessConfig
from harness.context import HarnessContext
from harness.exec_target import LocalTarget


def _limits(five_hour, seven_day):
    return {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "resetsAt": 1795039800, "rateLimitType": "five_hour", "overageStatus": "rejected",
        "overageDisabledReason": "org_level_disabled", "isUsingOverage": False,
        "unifiedWindows": {"five_hour": {"utilization": five_hour, "resetsAt": 1795039800, "status": "allowed"},
                           "seven_day": {"utilization": seven_day, "resetsAt": 1795154400, "status": "allowed"}}}}


def _message(inp, cache_read, cache_write):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}], "usage": {
        "input_tokens": inp, "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write,
        "output_tokens": 3}}}


def _result(cost, spawned=0, **more):
    used = {"inputTokens": 2, "outputTokens": 4, "cacheReadInputTokens": 3474, "cacheCreationInputTokens": 4242,
            "webSearchRequests": 1, "costUSD": cost, "contextWindow": 1000000, "maxOutputTokens": 128000,
            "costBasis": "list", "provider": "firstParty"}
    return {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "duration_api_ms": 900,
            "total_cost_usd": cost, "terminal_reason": "completed", "api_error_status": None,
            "usage": {"input_tokens": 2, "output_tokens": 4, "cache_read_input_tokens": 3474,
                      "cache_creation_input_tokens": 4242},
            "modelUsage": {"claude-haiku-5-5": used}, "model_usage_since_last_result": {"claude-haiku-5-5": used},
            "subagent_stats": {"spawned": spawned}, **more}


def test_a_bursts_plan_models_and_largest_prompt(tmp_path):
    ctx = HarnessContext(target=LocalTarget(repo=str(tmp_path)), cfg=HarnessConfig(), slug="t", branch="b",
                         state_key="k")
    engine = ClaudeCodeEngine(ctx)
    for event in (_limits(0.51, 0.73), _message(2, 3474, 4242), _result(0.0009, spawned=1),
                  _message(5, 7716, 100), _limits(0.53, 0.74), _result(0.0011, api_error_status=529)):
        engine._on_line(json.dumps(event))  # a run, then a fix-up's --continue
    engine.report()
    numbers = metrics.burst(ctx.lead_metrics, {}, seconds=10, fixups=1, handoffs=0)

    assert numbers["plan"]["five_hour"] == {"start": 51.0, "end": 53.0, "status": "allowed",
                                            "resets": "2026-11-18T22:10:00+00:00"}
    assert (numbers["plan"]["seven_day"]["start"], numbers["plan"]["seven_day"]["end"]) == (73.0, 74.0)
    assert numbers["plan"]["overage"] == {"status": "rejected", "using": False, "disabled": "org_level_disabled"}
    model = numbers["model_usage"]["claude-haiku-5-5"]
    assert (model["input"], model["cache_read"], model["web_searches"], model["cost_usd"]) == (4, 6948, 2, 0.002)
    assert model["cost_basis"] == "list" and numbers["context_window"] == 1000000
    assert numbers["peak_prompt_tokens"] == 5 + 7716 + 100
    assert (numbers["terminal_reason"], numbers["api_errors"], numbers["subagents"]) == ("completed", 1, 1)
    assert numbers["cost_usd"] == 0.002 and numbers["model_calls"] == 2  # the totals as before


def test_on_an_api_key_there_is_no_plan(tmp_path):
    ctx = HarnessContext(target=LocalTarget(repo=str(tmp_path)), cfg=HarnessConfig(), slug="t", branch="b",
                         state_key="k")
    engine = ClaudeCodeEngine(ctx)
    engine._on_line(json.dumps(_result(0.01)))
    engine.report()
    assert ctx.lead_metrics["plan"] is None and ctx.lead_metrics["model_usage"]["claude-haiku-5-5"]["output"] == 4


def test_claude_codes_compactions_are_counted_and_its_window_is_set(tmp_path, capsys):
    ctx = HarnessContext(target=LocalTarget(repo=str(tmp_path)), cfg=HarnessConfig(), slug="t", branch="b",
                         state_key="k")
    engine = ClaudeCodeEngine(ctx)
    assert "export CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000 && " in engine.command("-p", "go")
    for event in (_message(2, 3474, 4242),
                  {"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": "auto",
                                                                                         "pre_tokens": 201234}},
                  _result(0.001)):
        engine._on_line(json.dumps(event))
    assert "(Claude Code compacted its conversation at 201,234 tokens)" in capsys.readouterr().out
    engine.report()
    assert metrics.burst(ctx.lead_metrics, {}, seconds=1, fixups=0, handoffs=0)["window_cuts"] == 1

    from dataclasses import replace

    ctx.cfg = replace(ctx.cfg, claude_compact_window=0)  # Claude Code's own
    assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in ClaudeCodeEngine(ctx).command("-p", "go")
