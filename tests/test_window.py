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


def _notes(agent) -> list[tuple[int, str]]:
    return [(i, block["text"]) for i, message in enumerate(agent.messages) for block in message["content"]
            if block.get("text", "").startswith("[harness] The conversation was cut here")]


def test_a_cut_leaves_one_note_where_it_was_made():
    window = ChunkedWindow(window_size=20, per_turn=True, pin_first=2, on_cut=lambda: "The document, as it is now.")
    agent = _agent(22)
    window.apply_management(agent)
    [(at, note)] = _notes(agent)
    assert agent.messages[0]["content"][0]["text"] == "m0" and at == 2  # the first message after the pinned ones
    assert agent.messages[at]["role"] == "user" and agent.messages[at]["content"][0] == {"text": "m12"}
    assert "10 earlier messages were removed" in note and note.endswith("The document, as it is now.")
    _say(agent, 12)
    window.apply_management(agent)
    assert len(_notes(agent)) == 1  # the latest cut's alone


def test_a_note_that_cant_be_completed_still_says_the_cut():
    def broken():
        raise RuntimeError("no git")

    agent = _agent(22)
    ChunkedWindow(window_size=20, per_turn=True, on_cut=broken).apply_management(agent)
    [(_, note)] = _notes(agent)
    assert "earlier messages were removed" in note and "no git" not in note


def test_a_tasks_lead_is_told_the_document_and_git_at_a_cut(tmp_path, origin, monkeypatch, capsys):
    import json
    from pathlib import Path

    from harness import cli

    from . import fake_llama
    from .conftest import git

    # The fake picks its reply by counting the assistant's turns, which a cut takes back: it steps along for good,
    # and the budget ends the burst.
    server, requests = fake_llama.serve([{"tool": "shell", "args": {"command": "echo step"}}])
    try:
        work = tmp_path / "work"
        git(tmp_path, "clone", "-q", str(origin), str(work))
        monkeypatch.setenv("HARNESS_WINDOW_SIZE", "10")
        monkeypatch.setenv("HARNESS_MAX_MODEL_CALLS", "14")
        cli.main(["--provider", "llamacpp", "--base-url", f"http://127.0.0.1:{server.server_port}",
                  "--repo", str(work), "start", "t1", "--task", "Step along."])
        result = json.loads(capsys.readouterr().out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])
    finally:
        server.shutdown()
    cut = next(request for request in requests if "conversation was cut here" in json.dumps(request["messages"]))
    sent = json.dumps(cut["messages"])
    assert "may be out of date" in sent and "<task-document" in sent and "Step along." in sent
    assert result["metrics"]["window_cuts"] >= 1 and result["metrics"]["window_drops"] >= 1
    cuts = json.loads(Path(result["transcript"]).read_text())["cuts"]
    assert len(cuts) == result["metrics"]["window_cuts"] and cuts[0]["agent"] == "lead" and cuts[0]["dropped"] > 0
