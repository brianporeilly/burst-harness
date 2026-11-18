"""A burst's numbers in HARNESS_RESULT (and HARNESS_JOB): what launched it reads them rather than scraping the log."""

import json

from strands import Agent
from strands.agent.conversation_manager import NullConversationManager
from strands.types.exceptions import MaxTokensReachedException

from harness import agent as harness_agent
from harness import cli, metrics
from harness.agent import ChunkedWindow
from harness.claude_code import ClaudeCodeEngine
from harness.config import HarnessConfig

from .test_agent_loop import Looping, step
from .test_agents import llama  # noqa: F401 -- the fixture


def _result(out: str) -> dict:
    return json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])


def test_a_burst_says_its_numbers_and_the_variant_it_ran_as(tmp_path, origin, llama, capsys, monkeypatch):  # noqa: F811
    from .conftest import git

    url, _ = llama([{"tool": "pause_for_review", "args": {"summary": "Looked.", "next_step": "Carry on."}},
                    {"text": "Paused."}])
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    layer = tmp_path / "strict.toml"
    layer.write_text("max_fixup_attempts = 1\n")
    args = ["--config", str(layer), "--provider", "llamacpp", "--base-url", url, "--repo", str(work)]
    assert cli.main([*args, "start", "t1", "--task", "Look."]) == 0
    result = _result(capsys.readouterr().out)
    numbers = result["metrics"]
    assert numbers["schema"] == metrics.METRICS_SCHEMA and numbers["seconds"] >= 0
    assert numbers["model_calls"] == 1 and numbers["tool_calls"] == 1 and numbers["tool_errors"] == 0
    assert numbers["tokens"] == {"input": 10, "output": 5, "cache_read": 0, "cache_write": 0}  # the fake's usage
    assert numbers["peak_prompt_tokens"] == 10 and numbers["model_seconds"] >= 0 and numbers["tool_seconds"] >= 0
    assert numbers["cost_usd"] == 0  # a local model
    assert (numbers["fixups"], numbers["handoffs"], numbers["cut_off"], numbers["model_retries"]) == (0, 0, 0, 0)
    assert result["config"]["layers"] == ["strict"] and len(result["config"]["hash"]) == 16

    # The same layer's name with a different setting in it: the hash says they differ.
    layer.write_text("max_fixup_attempts = 2\n")
    monkeypatch.delenv("HARNESS_CONFIG")  # --config adds to it: a second run in one process would have it twice
    url, _ = llama([{"text": "Nothing to do."}])
    assert cli.main([*args[:-4], "--base-url", url, "--repo", str(work), "start", "t2", "--task", "Look."]) != 0
    again = _result(capsys.readouterr().out)
    assert again["config"]["layers"] == ["strict"] and again["config"]["hash"] != result["config"]["hash"]


def test_cut_off_replies_and_window_drops_are_counted():
    class CutOnce(Looping):
        async def stream(self, *args, **kwargs):
            if not getattr(self, "cut", False):
                self.cut = True
                raise MaxTokensReachedException("cut off")
            async for event in super().stream(*args, **kwargs):
                yield event

    agent = Agent(model=CutOnce(3), tools=[step], callback_handler=None,
                  conversation_manager=NullConversationManager())
    harness_agent.call(agent, "go")
    numbers = metrics.of_agent(agent, "a-model", "openai")
    assert numbers["cut_off"] == 1 and numbers["tool_calls"] == 3 and numbers["model_calls"] >= 4
    assert numbers["cost_usd"] is None  # no price known

    class Messages:
        def __init__(self, count):
            self.messages = [{"role": ("user", "assistant")[i % 2], "content": [{"text": "m"}]} for i in range(count)]

    held = Messages(22)
    ChunkedWindow(window_size=20, per_turn=True).apply_management(held)
    assert metrics.of_agent(agent, "a-model", "openai")["window_drops"] == 0
    assert metrics._counted[held]["window_drops"] == 12


def test_cost_and_sums():
    usage = {"inputTokens": 1_000_000, "outputTokens": 100_000, "cacheReadInputTokens": 0}
    assert metrics.cost(usage, "claude-sonnet-5", "anthropic") == 2.0 + 1.0
    assert metrics.cost(usage, "qwen", "llamacpp") == 0 and metrics.cost(usage, "unknown", "openai") is None

    one = {"model_calls": 2, "model_seconds": 1.5, "tokens": {"input": 10, "output": 1}, "peak_prompt_tokens": 9,
           "cost_usd": 0.5}
    two = {"model_calls": 3, "model_seconds": 1.0, "tokens": {"input": 5, "output": 2}, "peak_prompt_tokens": 12,
           "cost_usd": None}
    both = metrics.add(metrics.add(None, one), two)
    assert both["model_calls"] == 5 and both["model_seconds"] == 2.5 and both["tokens"] == {"input": 15, "output": 3}
    assert both["peak_prompt_tokens"] == 12 and both["cost_usd"] is None

    lead = {"cost_usd": 0.25, "model_calls": 4}
    total = metrics.burst(lead, {"scout": {"metrics": {"cost_usd": 0.5}}}, seconds=3.21, fixups=1, handoffs=2)
    assert (total["cost_usd"], total["seconds"], total["fixups"], total["handoffs"]) == (0.75, 3.2, 1, 2)
    assert metrics.burst(lead, {"scout": {"metrics": {"cost_usd": None}}}, seconds=0, fixups=0,
                         handoffs=0)["cost_usd"] is None
    nothing = metrics.burst(None, {}, seconds=1, fixups=0, handoffs=0)  # an engine that recorded nothing
    assert nothing["model_calls"] is None and nothing["tokens"] is None and nothing["cost_usd"] is None


def test_claude_codes_numbers_are_what_it_reported(tmp_path):
    from harness.context import HarnessContext
    from harness.exec_target import LocalTarget

    ctx = HarnessContext(target=LocalTarget(repo=str(tmp_path)), cfg=HarnessConfig(), slug="t", branch="b",
                         state_key="k")
    engine = ClaudeCodeEngine(ctx)
    for event in (
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}},
                                                      {"type": "tool_use", "name": "Read", "input": {}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True}, {"type": "tool_result"}]}},
        {"type": "result", "num_turns": 3, "total_cost_usd": 0.12, "duration_api_ms": 4500,
         "usage": {"input_tokens": 7, "output_tokens": 8, "cache_read_input_tokens": 100,
                   "cache_creation_input_tokens": 20}},
    ):
        engine._on_line(json.dumps(event))
    engine.report()
    numbers = ctx.lead_metrics
    assert (numbers["model_calls"], numbers["tool_calls"], numbers["tool_errors"]) == (3, 2, 1)
    assert numbers["tokens"] == {"input": 7, "output": 8, "cache_read": 100, "cache_write": 20}
    assert numbers["model_seconds"] == 4.5 and numbers["cost_usd"] == 0.12 and numbers["tool_seconds"] is None
