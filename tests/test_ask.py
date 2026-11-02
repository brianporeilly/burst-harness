"""harness ask: a task with no repo, answered as a one-off job is, with follow-ups that see the earlier answers."""

import json

import pytest

from harness import ask, cli
from harness.config import HarnessConfig

from . import fake_llama
from .test_job import _local


def _answers(*reports):
    return [{"tool": "finish_report", "args": {"report": report}} for report in reports]


def _overrides(url):
    return {"provider": "llamacpp", "base_url": url, "model_id": "m", "model_retries": 0}


def _outcome(out):
    return json.loads(out.split("HARNESS_JOB ", 1)[1].splitlines()[0])


def test_a_question_is_answered_and_a_follow_up_sees_the_answer(tmp_path, capsys):
    server, requests = fake_llama.serve(_answers("Ten uses, ranked."))
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        assert ask.run("bricks", "Ten uses for a brick?", None, _overrides(url), acquire=_local(tmp_path)) == 0
        out = capsys.readouterr().out
        assert "----- bricks answer -----\nTen uses, ranked." in out and _outcome(out)["state"] == "reported"
        system = fake_llama._text(requests[0]["messages"][0]["content"])
        assert "answering a question" in system and "call finish_report with it" in system
        assert fake_llama._text(requests[0]["messages"][-1]["content"]) == "Ten uses for a brick?"

        with pytest.raises(SystemExit, match="is answered: follow up with --message"):
            ask.run("bricks", None, None, _overrides(url), acquire=_local(tmp_path))
        requests.clear()
        assert ask.run("bricks", None, "Which is safest?", _overrides(url), acquire=_local(tmp_path)) == 0
        asked = fake_llama._text(requests[0]["messages"][-1]["content"])
        assert "<question>\nTen uses for a brick?\n</question>\n<answer>\nTen uses, ranked.\n</answer>" in asked
        assert asked.endswith("Which is safest?")
    finally:
        server.shutdown()
    cfg = HarnessConfig.load(None)
    assert [turn["asked"] for turn in ask.turns(cfg, "bricks")] == ["Ten uses for a brick?", "Which is safest?"]


def test_a_question_left_unanswered_is_asked_again(tmp_path, capsys):
    dead = {"provider": "no_such_module:build"}  # fails at once, with no backoff
    assert ask.run("q", "What is up?", None, dead, acquire=_local(tmp_path)) == 1
    assert _outcome(capsys.readouterr().out)["state"] == "failed"
    cfg = HarnessConfig.load(None)
    assert ask.prompt(cfg, "q", None, None) == ("What is up?", "What is up?")
    with pytest.raises(ask.AskError, match="give the question with --task"):
        ask.prompt(cfg, "never-asked", None, None)


def test_a_long_conversation_keeps_its_newest_turns(monkeypatch):
    turns = [{"asked": f"q{i}", "answer": "x" * 25_000, "id": str(i)} for i in range(5)]
    monkeypatch.setattr(ask, "turns", lambda cfg, slug: turns)
    _, text = ask.prompt(HarnessConfig(), "s", None, "next")
    assert "q4" in text and "q3" in text and "q2" not in text


def test_ask_is_a_command(monkeypatch):
    with pytest.raises(SystemExit, match="not a slug"):
        ask.run("Bad Slug", "x", None, {})
    seen = {}
    monkeypatch.setattr(ask, "run", lambda slug, task, message, overrides: seen.update(
        slug=slug, task=task, message=message, provider=overrides["provider"]) or 0)
    assert cli.main(["--provider", "anthropic", "ask", "s", "--task", "Why?"]) == 0
    assert seen == {"slug": "s", "task": "Why?", "message": None, "provider": "anthropic"}
