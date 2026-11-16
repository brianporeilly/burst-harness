"""Slack: the listed users are told when a job from a repo of jobs waits for Accept, and can accept it there."""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from orchestrator.jobs import JobRunner
from orchestrator.slack import SlackBot, review_message
from orchestrator.sources import Source

from .test_sources import _add, _commit, _repo, _sources, _write

ALLOWED, OTHER = "U0ALLOWED", "U0OTHER"
SECRET = '[job.secrets.mail]\nenv = "MAIL_TOKEN"\nhost = "mail.example.com"\n'


class FakeWeb:
    def __init__(self):
        self.calls, self.posted = [], 0

    def chat_postMessage(self, **message):
        self.calls.append(("post", message))
        self.posted += 1
        return {"channel": "D" + message["channel"], "ts": f"{self.posted}.1"}

    def chat_update(self, **message):
        self.calls.append(("update", message))

    def chat_postEphemeral(self, **message):
        self.calls.append(("whisper", message))

    def take(self):
        calls, self.calls = self.calls, []
        return calls


def _flush(bot):
    while not bot.reviews.empty():
        bot.show_review(*bot.reviews.get())


def _click(user, value):
    return SimpleNamespace(type="interactive", envelope_id="e1", payload={
        "type": "block_actions", "user": {"id": user}, "channel": {"id": "D" + user},
        "actions": [{"action_id": "job-accept", "value": json.dumps(value)}]})


def _accept_value(call):
    blocks = call[1]["blocks"]
    button = next((e for b in blocks if b["type"] == "actions" for e in b["elements"]), None)
    return json.loads(button["value"]) if button else None


def test_a_job_waiting_for_accept_is_told_to_the_listed_users_and_kept_up_to_date(tmp_path):
    repo = _repo(tmp_path)
    store = _sources(tmp_path, Source("mine", str(repo), path="jobs"))
    first = _write(repo, "jobs/digest", '[job]\ntrigger = "manual"\n')
    store.sync()
    store.accept("mine", "digest", first)
    bot = SlackBot(None, (ALLOWED, OTHER), jobs=JobRunner([], sources=store))
    bot.web = FakeWeb()

    # Its access changes: each listed user gets a message with the change and an Accept for that commit.
    second = _write(repo, "jobs/digest", '[job]\ntrigger = "manual"\n' + SECRET)
    store.sync()
    _flush(bot)
    posts = bot.web.take()
    assert [(kind, call["channel"]) for kind, call in posts] == [("post", ALLOWED), ("post", OTHER)]
    assert posts[0][1]["text"] == "digest changes its access, and waits for your Accept"
    assert "MAIL_TOKEN" in json.dumps(posts[0][1]["blocks"])
    assert _accept_value(posts[0]) == {"source": "mine", "job": "digest", "commit": second}
    notices = store.review("mine")["pending"]["digest"]["notices"]
    assert notices == [{"channel": "D" + ALLOWED, "ts": "1.1"}, {"channel": "D" + OTHER, "ts": "2.1"}]

    # A newer commit with the same access: the same messages, their Accept for the newer commit.
    third = _write(repo, "jobs/digest", '[job]\ntrigger = "manual"\n' + SECRET, "a new prompt")
    store.sync()
    _flush(bot)
    updates = bot.web.take()
    assert [(kind, call["ts"]) for kind, call in updates] == [("update", "1.1"), ("update", "2.1")]
    assert _accept_value(updates[0])["commit"] == third

    # Its access changes again: the old messages say so, and new ones have it.
    fourth = _write(repo, "jobs/digest", 'egress_allow = ["api.example.com"]\n[job]\ntrigger = "manual"\n' + SECRET)
    store.sync()
    _flush(bot)
    calls = bot.web.take()
    assert [kind for kind, _ in calls] == ["update", "update", "post", "post"]
    assert "Changed again since" in json.dumps(calls[0][1]["blocks"]) and _accept_value(calls[0]) is None
    assert _accept_value(calls[2])["commit"] == fourth

    # Only a listed user accepts, and only what is waiting now.
    acks = []
    client = SimpleNamespace(send_socket_mode_response=acks.append)
    bot.on_request(client, _click("U0STRANGER", {"source": "mine", "job": "digest", "commit": fourth}))
    (kind, whispered), = bot.web.take()
    assert kind == "whisper" and "U0STRANGER" in whispered["text"]
    bot.on_request(client, _click(ALLOWED, {"source": "mine", "job": "digest", "commit": third}))
    (kind, whispered), = bot.web.take()
    assert kind == "whisper" and "changed again" in whispered["text"]
    assert store.waiting()

    bot.on_request(client, _click(ALLOWED, {"source": "mine", "job": "digest", "commit": fourth}))
    _flush(bot)
    assert store.waiting() == [] and store.review("mine")["accepted"]["digest"]["by"] == f"slack:{ALLOWED}"
    done = bot.web.take()
    assert [(kind, call["ts"]) for kind, call in done] == [("update", "3.1"), ("update", "4.1")]
    assert f"Accepted by <@{ALLOWED}>" in json.dumps(done[0][1]["blocks"]) and _accept_value(done[0]) is None
    assert len(acks) == 3


def test_one_that_stops_waiting_says_so_and_one_waiting_while_slack_was_off_is_told_on_connecting(tmp_path):
    repo = _repo(tmp_path)
    store = _sources(tmp_path, Source("mine", str(repo), path="jobs"))
    first = _write(repo, "jobs/digest", "[job]\n")
    store.sync()
    store.accept("mine", "digest", first)
    bot = SlackBot(None, (ALLOWED,), jobs=JobRunner([], sources=store))

    _write(repo, "jobs/digest", "[job]\n" + SECRET)
    _add(repo, "jobs/watcher")
    _commit(repo)
    store.sync()
    _flush(bot)  # not connected: nothing said, nothing kept
    assert all(not item["notices"] for item in store.waiting())

    bot.web = FakeWeb()
    bot.untold()
    _flush(bot)
    posts = bot.web.take()
    assert sorted(call["text"] for _, call in posts) == ["digest changes its access, and waits for your Accept",
                                                          "watcher is new to the repo, and waits for your Accept"]
    assert all(item["notices"] for item in store.waiting())
    bot.untold()
    assert bot.reviews.empty()  # told once

    _write(repo, "jobs/digest", "[job]\n")  # back to what was accepted
    store.sync()
    _flush(bot)
    updates = {call["text"].split()[0]: json.dumps(call["blocks"]) for kind, call in bot.web.take() if kind == "update"}
    assert "No longer waits" in updates["digest"]
    assert "No longer waits" not in updates["watcher"] and "job-accept" in updates["watcher"]  # at the newer commit


def test_a_change_too_long_to_show_is_accepted_in_the_page_only():
    item = {"commit": "a" * 40, "new": True,
            "changes": [{"setting": "file claude.json", "before": None, "after": "x" * 1000}]}
    _, blocks = review_message("mine", "digest", item)
    assert "not the whole change" in json.dumps(blocks) and not any(b["type"] == "actions" for b in blocks)
    item["changes"][0]["after"] = "short"
    _, blocks = review_message("mine", "digest", item)
    assert blocks[-1]["elements"][0]["action_id"] == "job-accept"
    assert "does not run until it is accepted" in json.dumps(blocks)
