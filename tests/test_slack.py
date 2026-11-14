"""Slack: tasks started from a message, for the listed users only, and their bursts told in its thread."""

import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from harness import credentials
from orchestrator.slack import SlackBot, plain, users_from

from .test_tasks import _answer, _runner, _settled, harness  # noqa: F401 (harness is a fixture)

ME, ALLOWED = "UBOT", "U0ALLOWED"


class FakeWeb:
    def __init__(self):
        self.posts, self.posted = [], threading.Event()

    def chat_postMessage(self, **message):
        self.posts.append(message)
        self.posted.set()

    def texts(self):
        return [post["text"] for post in self.posts]


@pytest.fixture
def bot(tmp_path, harness):  # noqa: F811
    bot = SlackBot(_runner(tmp_path), (ALLOWED,))
    bot.web, bot.me = FakeWeb(), ME
    return bot


def _mention(text, *, user=ALLOWED, ts="100.1", thread=None, channel="C1"):
    event = {"type": "app_mention", "user": user, "channel": channel, "ts": ts, "text": f"<@{ME}> {text}"}
    return event | ({"thread_ts": thread} if thread else {})


def _flush(bot, until):
    """Post what the bot has queued until `until(texts)` holds. A burst's end is told after its state is
    written, from the burst's thread, so a test that saw the state may still be ahead of the post."""
    for _ in range(250):
        while not bot.outbox.empty():
            bot.say(*bot.outbox.get())
        if until(bot.web.texts()):
            return
        threading.Event().wait(0.02)
    raise AssertionError(f"not posted: {bot.web.texts()}")


def test_a_mention_starts_a_task_and_its_bursts_are_told_in_the_thread(bot, harness):  # noqa: F811
    bot.handle(_mention("me/proj  fix the\nflaky test"))
    task = bot.tasks.tasks()[0]
    assert (task["repo"], task["prompt"], task["created_by"]) == ("https://github.com/me/proj.git",
                                                                   "fix the\nflaky test", f"slack:{ALLOWED}")
    assert bot.tasks.in_thread("C1", "100.1") == task["slug"]
    assert bot.web.texts()[0].startswith(f"On it: task `{task['slug']}` on me/proj")
    assert all(post["thread_ts"] == "100.1" for post in bot.web.posts)

    harness.started[0][1].finish(3, state="waiting", said={"asks": ["Which <API> version?"]})
    _settled(bot.tasks, task["slug"], "waiting")
    _flush(bot, lambda texts: len(texts) >= 3)
    assert bot.web.texts()[1] == ":arrow_forward: Burst 1 started."
    assert "> Which &lt;API&gt; version?" in bot.web.texts()[2] and "Reply here" in bot.web.texts()[2]

    # A reply in the thread continues it, with the reply.
    bot.handle(_mention("Use v2.", ts="100.2", thread="100.1"))
    assert harness.argv()[-2:] == ["--message", "Use v2."]
    harness.started[1][1].finish(0, state="pr", pr="https://github.com/me/proj/pull/8")
    _settled(bot.tasks, task["slug"], "pr")
    _flush(bot, lambda texts: "Opened a PR" in texts[-1])
    assert bot.web.texts()[-1] == ":white_check_mark: Opened a PR: https://github.com/me/proj/pull/8"

    # With auto_continue, the merge starts the next round, and the thread is told why.
    bot.tasks.auto_continue(task["slug"], True)
    harness.prs["https://github.com/me/proj/pull/8"] = "merged"
    bot.tasks.check_pr(task["slug"], 0)
    _flush(bot, lambda texts: "round 2" in texts[-1])
    assert bot.web.texts()[-1] == (":arrow_forward: The PR was merged, so round 2 started by itself on "
                                   f"`harness/{task['slug']}-2`.")


def test_a_failure_says_how_it_ended_and_a_pause_its_questions(bot, harness):  # noqa: F811
    bot.handle({"type": "message", "channel_type": "im", "user": ALLOWED, "channel": "D1", "ts": "5.0",
                "text": "me/proj tidy up"})  # a direct message: no mention needed
    slug = bot.tasks.tasks()[0]["slug"]
    harness.started[0][1].finish(1)
    _settled(bot.tasks, slug, "failed")
    _flush(bot, lambda texts: "It failed" in texts[-1])
    assert "It failed (exit 1)" in bot.web.texts()[-1] and "working..." in bot.web.texts()[-1]

    bot.handle({"type": "message", "channel_type": "im", "user": ALLOWED, "channel": "D1", "ts": "5.1",
                "thread_ts": "5.0", "text": "continue"})
    assert "--message" not in harness.argv()
    harness.started[1][1].finish(0, state="paused", said={"next_step": "Pick a library", "questions": "A or B?"})
    _settled(bot.tasks, slug, "paused")
    _flush(bot, lambda texts: "*Next step:*" in texts[-1])
    assert "*Next step:* Pick a library" in bot.web.texts()[-1] and "*Questions:* A or B?" in bot.web.texts()[-1]

    bot.handle({"type": "message", "channel_type": "im", "user": ALLOWED, "channel": "D1", "ts": "5.2",
                "thread_ts": "5.0", "text": "status"})
    assert bot.web.texts()[-1] == f"`{slug}` paused: me/proj"


def test_only_listed_users_and_never_the_bot_itself(bot):
    bot.handle(_mention("me/proj do it", user="U0STRANGER"))
    assert bot.tasks.tasks() == [] and "Your Slack ID is `U0STRANGER`" in bot.web.texts()[0]
    for event in ({"type": "message", "channel_type": "im", "user": ME, "channel": "D1", "ts": "1", "text": "x"},
                  {"type": "message", "channel_type": "im", "bot_id": "B1", "user": "U9", "channel": "D1", "ts": "1"},
                  {"type": "message", "channel_type": "im", "subtype": "message_changed", "channel": "D1", "ts": "1"},
                  {"type": "message", "channel_type": "channel", "user": ALLOWED, "channel": "C1", "ts": "1"}):
        bot.handle(event)
    assert len(bot.web.posts) == 1


def test_mistakes_are_answered_not_raised(bot):
    bot.handle(_mention("https://x/../y do it"))
    bot.handle(_mention("me/proj", ts="2"))
    bot.handle(_mention("", ts="3"))
    texts = bot.web.texts()
    assert "is not a repo" in texts[0] and "Say what to do on me/proj" in texts[1] and "Give me a repo" in texts[2]
    assert bot.tasks.tasks() == []


def test_a_message_with_no_repo_is_a_question_answered_in_the_thread(bot, harness):  # noqa: F811
    bot.handle(_mention("What are <good> uses for a brick?"))
    task = bot.tasks.tasks()[0]
    assert (task["repo"], task["prompt"]) == (None, "What are <good> uses for a brick?")
    assert bot.web.texts()[0] == f"On it: question `{task['slug']}`. I'll answer here."
    assert harness.argv()[-4:] == ["ask", task["slug"], "--task", "What are <good> uses for a brick?"]
    _answer(bot.tasks, harness.started[0][1], "A <doorstop>.")
    _settled(bot.tasks, task["slug"], "answered")
    _flush(bot, lambda texts: len(texts) >= 3)
    assert bot.web.texts()[1] == ":hourglass_flowing_sand: Working on it."
    assert bot.web.texts()[2] == "A &lt;doorstop&gt;.\n\n_Reply here to follow up._"

    bot.handle(_mention("Which is safest?", ts="100.2", thread="100.1"))
    assert harness.argv()[-2:] == ["--message", "Which is safest?"]
    _answer(bot.tasks, harness.started[1][1], None, reason="the model ended without calling finish_report")
    _settled(bot.tasks, task["slug"], "failed")
    _flush(bot, lambda texts: texts[-1].startswith(":x: No answer"))
    assert bot.web.texts()[-2] == ":hourglass_flowing_sand: Working on your follow-up."
    assert bot.web.texts()[-1] == (":x: No answer: the model ended without calling finish_report\n"
                                   "`continue` here asks it again.")
    bot.handle(_mention("status", ts="100.3", thread="100.1"))
    assert bot.web.texts()[-1] == f"`{task['slug']}` failed: a question"


def test_an_event_slack_sends_twice_is_handled_once(bot):
    acks = []
    client = SimpleNamespace(send_socket_mode_response=acks.append)
    request = SimpleNamespace(type="events_api", envelope_id="e1",
                              payload={"event_id": "Ev1", "event": _mention("me/proj do it")})
    bot.on_request(client, request)
    bot.on_request(client, SimpleNamespace(**{**vars(request), "envelope_id": "e2"}))
    assert len(acks) == 2 and len(bot.tasks.tasks()) == 1


def test_it_stays_off_without_both_tokens(tmp_path, harness, monkeypatch):  # noqa: F811
    monkeypatch.setattr(credentials, "_stored", {})
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-1")
    bot = SlackBot(_runner(tmp_path), ())
    bot.check()
    assert bot.socket is None and bot.status.startswith("off: needs SLACK_APP_TOKEN")


def test_text_as_typed_and_users_as_listed():
    assert plain(f"<@{ME}>  <https://git.lan/me/proj.git|git.lan/me/proj>  a &amp; b &lt;x&gt;", ME) == \
        "https://git.lan/me/proj.git a & b <x>"
    assert users_from("U012ABC, u034def U012ABC") == ("U012ABC", "U034DEF")
