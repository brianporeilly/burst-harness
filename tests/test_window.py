"""The conversation window: cut in chunks, so the prompt's prefix (and its cache) holds between cuts."""

from types import SimpleNamespace

from strands.types.exceptions import ContextWindowOverflowException

from harness.agent import ChunkedWindow, conversation_manager
from harness.config import HarnessConfig


def _agent(count: int) -> SimpleNamespace:
    roles = ("user", "assistant")
    return SimpleNamespace(messages=[{"role": roles[i % 2], "content": [{"text": f"m{i}"}]} for i in range(count)])


def _say(agent: SimpleNamespace, count: int) -> None:
    for _ in range(count):
        role = "assistant" if agent.messages[-1]["role"] == "user" else "user"
        agent.messages.append({"role": role, "content": [{"text": "more"}]})


def test_past_the_window_it_cuts_back_to_half_and_then_holds():
    window = ChunkedWindow(window_size=20, per_turn=True)
    agent = _agent(22)
    window.apply_management(agent)
    assert len(agent.messages) == 10 and agent.messages[0]["role"] == "user"
    first = agent.messages[0]
    for _ in range(10):  # up to the window again: nothing cut, so the prefix is the same every call
        _say(agent, 1)
        window.apply_management(agent)
        assert agent.messages[0] is first
    assert len(agent.messages) == 20
    _say(agent, 2)
    window.apply_management(agent)
    assert len(agent.messages) == 10 and agent.messages[0] is not first


def test_compression_before_the_window_is_full_halves_the_conversation():
    window = ChunkedWindow(window_size=200, per_turn=True)
    agent = _agent(60)
    window.reduce_context(agent)
    assert len(agent.messages) == 30
    assert window.window_size == 200


def test_pinned_messages_survive_the_cut():
    window = ChunkedWindow(window_size=20, per_turn=True, pin_first=2)
    agent = _agent(21)
    head = agent.messages[:2]
    window.apply_management(agent)
    assert agent.messages[:2] == head and len(agent.messages) <= 12


def test_an_overflow_is_left_to_upstream():
    window = ChunkedWindow(window_size=200, per_turn=True)
    agent = _agent(60)
    window.reduce_context(agent, ContextWindowOverflowException("too long"))
    assert len(agent.messages) == 58  # upstream's smallest valid cut, not half


def test_tasks_and_jobs_get_it_from_their_config():
    window = conversation_manager(HarnessConfig(window_size=50, pin_first=3))
    assert isinstance(window, ChunkedWindow)
    assert (window.window_size, window.pin_first, window.per_turn) == (50, 3, True)
    assert HarnessConfig().window_size == 200
