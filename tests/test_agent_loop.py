"""harness.agent.call: a run of many tool calls, on a model that only asks for them; and a tool that stops it."""

import json

from strands import Agent, tool
from strands.agent.conversation_manager import NullConversationManager
from strands.models.model import Model
from strands.types.tools import ToolContext

from harness import agent as harness_agent
from harness.tools import stop_loop


class Looping(Model):
    """Asks for the `step` tool `calls` times, then says it is done."""

    def __init__(self, calls):
        self.calls, self.made = calls, 0

    def update_config(self, **kwargs):
        pass

    def get_config(self):
        return {}

    async def structured_output(self, *args, **kwargs):
        raise NotImplementedError

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        yield {"messageStart": {"role": "assistant"}}
        if self.made < self.calls:
            self.made += 1
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"t{self.made}", "name": "step"}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockDelta": {"delta": {"text": "done"}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}


@tool
def step() -> str:
    """One step."""
    return "ok"


def test_a_run_goes_on_past_strands_recursion_limit():
    # Strands recurses once per tool call: without the restarts, this ended in a RecursionError at ~319.
    model = Looping(450)
    agent = Agent(model=model, tools=[step], callback_handler=None, conversation_manager=NullConversationManager())
    result = harness_agent.call(agent, "go")
    assert model.made == 450 and result.stop_reason == "end_turn" and str(result).strip() == "done"
    assert len(agent.messages) == 1 + 450 * 2 + 1  # the prompt, each call and its result, the answer
    assert not [m for m in agent.messages if m["role"] == "user" and "text" in m["content"][0]][1:]  # nothing added


def test_a_tool_that_stops_the_loop_on_a_restart_still_stops_it():
    stopped = []

    @tool(context="tool_context")
    def step(tool_context: ToolContext) -> str:
        """One step."""
        if len(stopped) + 1 == harness_agent.RESTART_EVERY:
            stop_loop(tool_context)
        stopped.append(1)
        return "ok"

    model = Looping(500)
    result = harness_agent.call(Agent(model=model, tools=[step], callback_handler=None), "go")
    assert model.made == harness_agent.RESTART_EVERY and result.stop_reason == "cancelled"


def test_a_prompt_after_a_tool_stopped_the_loop_joins_its_results():
    """A burst resuming after a terminal state: the history ends with that tool's results, a user turn."""
    @tool(context="tool_context")
    def step(tool_context: ToolContext) -> str:
        """One step."""
        stop_loop(tool_context)
        return "ok"

    agent = Agent(model=Looping(2), tools=[step], callback_handler=None)
    harness_agent.call(agent, "go")
    assert [m["role"] for m in agent.messages] == ["user", "assistant", "user"]
    harness_agent.call(agent, "carry on")
    assert [m["role"] for m in agent.messages] == ["user", "assistant", "user", "assistant", "user"]
    assert agent.messages[2]["content"][-1] == {"text": "carry on"} and "toolResult" in agent.messages[2]["content"][0]


def test_a_session_ends_when_the_model_finishes_even_if_it_would_finish_again(tmp_path, origin, capsys):
    """A model that called finish_with_pr over and over, the PR step failing each time, ran until it was stopped."""
    from harness import cli

    from . import fake_llama
    from .conftest import git

    finish = {"tool": "finish_with_pr", "args": {"title": "Fix it", "body": "Fixed."}}
    server, requests = fake_llama.serve([finish] * 5 + [{"text": "Done."}])
    try:
        work = tmp_path / "work"
        git(tmp_path, "clone", "-q", str(origin), str(work))
        args = ["--provider", "llamacpp", "--base-url", f"http://127.0.0.1:{server.server_port}", "--repo", str(work)]
        assert cli.main([*args, "start", "t1", "--task", "Fix it."]) == 0
        out = capsys.readouterr().out
        # Pushed, no PR (the origin is a path, no host/owner/repo): an end, after one call and one model request.
        assert out.count("Tool #") == 1 and len(requests) == 1
        assert json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])["state"] == "pushed_no_pr"
    finally:
        server.shutdown()


def test_a_sigint_cancels_the_run_at_once_rather_than_when_strands_returns():
    """Ctrl-C, or the orchestrator's Stop: the run used to go on to its next restart, RESTART_EVERY calls later."""
    import asyncio
    import os
    import signal
    import threading
    import time

    import pytest

    class Slow(Looping):
        async def stream(self, *args, **kwargs):
            await asyncio.sleep(0.02)
            async for event in super().stream(*args, **kwargs):
                yield event

    model = Slow(500)
    agent = Agent(model=model, tools=[step], callback_handler=None, conversation_manager=NullConversationManager())
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)  # a test runner may have its own
    try:
        threading.Timer(0.5, os.kill, args=(os.getpid(), signal.SIGINT)).start()
        started = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            harness_agent.call(agent, "go")
        assert time.monotonic() - started < 2 and model.made < harness_agent.RESTART_EVERY // 2
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler  # put back
        assert agent not in harness_agent._running
    finally:
        signal.signal(signal.SIGINT, previous)
