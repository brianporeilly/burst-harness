"""harness.agent.call: a run of many tool calls, on a model that only asks for them."""

from strands import Agent, tool
from strands.agent.conversation_manager import NullConversationManager
from strands.models.model import Model
from strands.types.tools import ToolContext

from harness import agent as harness_agent


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
            tool_context.invocation_state.setdefault("request_state", {})["stop_event_loop"] = True
        stopped.append(1)
        return "ok"

    model = Looping(500)
    result = harness_agent.call(Agent(model=model, tools=[step], callback_handler=None), "go")
    assert model.made == harness_agent.RESTART_EVERY and result.stop_reason == "tool_use"
