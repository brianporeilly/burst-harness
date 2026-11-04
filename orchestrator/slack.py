"""Slack, for the orchestrator: start a task from a message, and hear how it goes in that message's thread.

It uses Socket Mode: the orchestrator connects out to Slack with SLACK_APP_TOKEN
(xapp-..., the app-level token) and posts with SLACK_BOT_TOKEN (xoxb-...), so
nothing has to reach it from outside. Both come from the Settings tab or the
environment, like any credential. It connects once both are there, and again
when either changes, checked every POLL seconds, so setting them in the page
is enough.

Only the Slack users in ORCHESTRATOR_SLACK_USERS (their IDs, such as U012ABC)
can use it. Anyone else is told their own ID and nothing more, which is also
how you find yours.

Mention the bot in a channel it is in, or message it directly:

    owner/repo what to do    a task on that repo; the bot answers in its thread
    anything else            a question (a task with no repo): answered in its thread
    status                   the latest tasks
    help

In a task's thread (mentioning the bot in a channel; any message in a direct
conversation):

    status                   where it is
    cancel                   stop it
    continue                 another burst, from where it stopped
    anything else            the reply to continue it with (its questions, a
                             pause's next step), or a question's follow-up

The task's bursts are reported in the thread: each start, then how it ended (a
PR, a pause and its questions, a question, a failure and the end of its
output), or a question's answer. The task is the same as one made in the Tasks tab or with the API,
created by slack:<user ID>.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from collections import OrderedDict
from typing import Any

from harness import credentials

from .tasks import SHORT_REPO, URL_REPO, TaskRunner, TasksError, repo_url, slug_for

POLL = 30
USERS_ENV = "ORCHESTRATOR_SLACK_USERS"
TAIL_LINES = 15
ANSWER_CHARS = 39_000  # chat.postMessage truncates text at 40,000 characters
USAGE = """Give me a repo and what to do: `owner/repo fix the flaky test in test_api.py` (or the repo's https URL).
I work on it as a task until there is a PR, and say how it goes in this thread. In the thread: `status`, \
`cancel`, `continue`, or anything else as the reply to continue it with.
Anything without a repo first is a question: I answer it in the thread, and a reply there follows up.
`status` on its own lists the latest tasks."""
ENDED = {
    "pr": ":white_check_mark: Opened a PR: {pr}",
    "pushed_no_pr": ":warning: Pushed `{branch}`, but the PR could not be opened.",
    "paused": ":double_vertical_bar: Paused, with the work pushed to `{branch}`.",
    "waiting": ":question: It needs an answer.",
    "abandoned": ":x: It gave up.",
    "max_tokens": ":warning: Stopped: the model's replies kept hitting the output limit. `continue` carries on in the "
                  "same sandbox (a lower effort or another model may help).",
    "cancelled": ":stop_sign: Cancelled.",
    "interrupted": ":warning: Cut short by orchestrator restarts.",
}


class SlackBot:
    def __init__(self, tasks: TaskRunner, users: tuple[str, ...]) -> None:
        self.tasks, self.users = tasks, frozenset(users)
        self.tokens: tuple[str, str] | None = None
        self.web: Any = None  # slack_sdk's WebClient, once there is a bot token
        self.socket: Any = None  # its SocketModeClient, once connected
        self.me = ""
        self.status = "off: needs SLACK_APP_TOKEN and SLACK_BOT_TOKEN (Settings)"
        self.outbox: queue.Queue[tuple[str, str, str]] = queue.Queue()
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.lock = threading.Lock()
        tasks.watch(self.burst)

    # --- connecting ---

    def start(self) -> None:
        threading.Thread(target=self._supervise, daemon=True, name="slack").start()
        threading.Thread(target=self._post, daemon=True, name="slack-posts").start()

    def _supervise(self) -> None:
        while True:
            self.check()
            time.sleep(POLL)

    def check(self) -> None:
        """Connect, or connect again, if the tokens are not the ones connected with."""
        tokens = (credentials.get("SLACK_APP_TOKEN"), credentials.get("SLACK_BOT_TOKEN"))
        if tokens == self.tokens:
            return
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        self.tokens, self.web = tokens, None
        if not all(tokens):
            self.status = "off: needs SLACK_APP_TOKEN and SLACK_BOT_TOKEN (Settings)"
            return
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient

        try:
            web = WebClient(token=tokens[1])
            me = web.auth_test()
            socket = SocketModeClient(app_token=tokens[0], web_client=web)
            socket.socket_mode_request_listeners.append(self.on_request)
            socket.connect()
        except Exception as err:  # a bad token, Slack unreachable: said in the page, tried again next time
            response = getattr(err, "response", None)  # SlackApiError: Slack's own code, such as invalid_auth
            reason = (response.get("error") if response is not None else None) or str(err).splitlines()[0]
            self.tokens, self.status = None, f"not connected: {reason}"
            print(f"slack: {self.status}")
            return
        self.web, self.socket, self.me = web, socket, str(me["user_id"])
        self.status = f"connected as @{me['user']} in {me['team']}" + ("" if self.users else f", but no one may use it yet ({USERS_ENV})")
        print(f"slack: {self.status}")

    def on_request(self, client: Any, request: Any) -> None:
        """A Socket Mode envelope: acknowledged at once (or Slack sends it again), then handled."""
        from slack_sdk.socket_mode.response import SocketModeResponse

        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        if request.type != "events_api":
            return
        with self.lock:
            ident = str(request.payload.get("event_id") or "")
            if ident in self.seen:
                return
            self.seen[ident] = None
            while len(self.seen) > 500:
                self.seen.popitem(last=False)
        self.handle(request.payload.get("event") or {})

    # --- messages ---

    def handle(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        direct = kind == "message" and event.get("channel_type") == "im" and not event.get("subtype")
        if not (kind == "app_mention" or direct) or event.get("bot_id") or event.get("user") in ("", None, self.me):
            return
        user, channel = str(event["user"]), str(event["channel"])
        thread = str(event.get("thread_ts") or "")
        answer_in = thread or str(event["ts"])
        if user not in self.users:
            self.say(channel, answer_in, f"Only the people this orchestrator lists can use it. Your Slack ID is "
                                         f"`{user}`: add it to {USERS_ENV} to be one.")
            return
        text = plain(str(event.get("text") or ""), self.me)
        try:
            if thread and (slug := self.tasks.in_thread(channel, thread)):
                self.in_task(slug, text, channel, thread)
            else:
                self.command(text, user, channel, answer_in)
        except TasksError as err:
            self.say(channel, answer_in, f":warning: {escape(str(err))}")

    def command(self, text: str, user: str, channel: str, thread: str) -> None:
        first, rest = (re.split(r"\s+", text.strip(), maxsplit=1) + [""])[:2]
        if first.lower() in ("", "help", "?"):
            self.say(channel, thread, USAGE)
        elif first.lower() in ("status", "tasks", "list") and not rest.strip():
            found = self.tasks.tasks()[:5]
            self.say(channel, thread, "\n".join(_line(t) for t in found) or "No tasks yet.")
        elif not (SHORT_REPO.fullmatch(first.removesuffix("/")) or URL_REPO.fullmatch(first.removesuffix("/"))):
            # No repo: a question, for whichever model the router picks.
            slug = slug_for(text)
            self.say(channel, thread, f"On it: question `{slug}`. I'll answer here.")
            self.tasks.create({"prompt": text.strip(), "slug": slug}, user=f"slack:{user}",
                              slack={"channel": channel, "thread": thread})
        else:
            url, prompt = repo_url(first), rest.strip()
            if not prompt:
                raise TasksError(f"Say what to do on {first}, after it.")
            slug = slug_for(prompt)
            # Said first: the task's first burst may start (and say so) before create returns.
            self.say(channel, thread, f"On it: task `{slug}` on {_repo(url)}. I'll say here how it goes.")
            self.tasks.create({"repo": url, "prompt": prompt, "slug": slug}, user=f"slack:{user}",
                              slack={"channel": channel, "thread": thread})

    def in_task(self, slug: str, text: str, channel: str, thread: str) -> None:
        word = text.strip().lower().rstrip(".!")
        if word == "status":
            self.say(channel, thread, _line(self.tasks.task(slug)))
        elif word in ("cancel", "stop"):
            state = self.tasks.cancel(slug)["state"]
            self.say(channel, thread, ":stop_sign: Cancelled." if state == "cancelled" else "Stopping it.")
        elif word in ("continue", "go on", "retry"):
            self.tasks.resume(slug)
            self.say(channel, thread, "Continuing from where it stopped.")
        else:
            self.tasks.resume(slug, text)
            self.say(channel, thread, "Continuing, with your reply.")

    # --- what the task's bursts do ---

    def burst(self, event: str, record: dict[str, Any]) -> None:
        """TaskRunner's watcher: called with its lock held, so this only queues the message."""
        where = record.get("slack")
        if not isinstance(where, dict) or not where.get("channel") or not where.get("thread"):
            return
        self.outbox.put((where["channel"], where["thread"], self._about(event, record)))

    def _about(self, event: str, record: dict[str, Any]) -> str:
        count = len(record.get("bursts") or [])
        if event == "started":
            if not record.get("repo"):
                return ":hourglass_flowing_sand: Working on " + ("your follow-up." if record.get("message") else "it.")
            return f":arrow_forward: Burst {count} started" + (" with your reply." if record.get("message") else ".")
        state = str(record.get("state") or "")
        if not record.get("repo"):
            return self._answer(state, record)
        said = record.get("said") or {}
        text = ENDED.get(state, ":x: It failed (exit {exit}).").format(
            pr=record.get("pr") or "", branch=record.get("branch") or "", exit=record.get("exit"))
        parts = [text]
        for key, label in (("summary", "Done"), ("next_step", "Next step"), ("questions", "Questions"), ("reason", "Why")):
            if said.get(key):
                parts.append(f"*{label}:* {escape(str(said[key]))}")
        parts += [f"> {escape(str(ask))}" for ask in said.get("asks") or []]
        if state not in ENDED:  # a failure: the end of what it printed
            parts.append("```" + escape(self._tail(str(record["slug"]))) + "```")
        if state in ("paused", "waiting"):
            parts.append("Reply here to continue it.")
        elif state not in ("pr", "cancelled"):
            parts.append("`continue` (or a reply) here tries again.")
        return "\n".join(parts)

    def _answer(self, state: str, record: dict[str, Any]) -> str:
        """How a question's burst ended: its answer, or why there is none."""
        burst = (record.get("bursts") or [{}])[-1]
        if state == "answered":
            answer = str(burst.get("answer") or "")
            if len(answer) > ANSWER_CHARS:
                answer = answer[:ANSWER_CHARS] + "\n_(cut short: the whole answer is in the Tasks tab)_"
            return escape(answer) + "\n\n_Reply here to follow up._"
        if state == "cancelled":
            return ENDED["cancelled"]
        parts = [":x: No answer" + (f": {escape(str(burst['reason']))}" if burst.get("reason") else ".")]
        if not burst.get("reason"):
            parts.append("```" + escape(self._tail(str(record["slug"]))) + "```")
        parts.append("`continue` here asks it again.")
        return "\n".join(parts)

    def _tail(self, slug: str) -> str:
        try:
            output = self.tasks.task(slug).get("output") or ""
        except TasksError:
            return "(no output)"
        return "\n".join(output.rstrip().splitlines()[-TAIL_LINES:])[-2500:] or "(no output)"

    def _post(self) -> None:
        while True:
            self.say(*self.outbox.get())

    def say(self, channel: str, thread: str, text: str) -> None:
        if self.web is None:
            print(f"slack: not connected, so not said in {channel}: {text[:200]}")
            return
        try:
            self.web.chat_postMessage(channel=channel, thread_ts=thread, text=text, unfurl_links=False)
        except Exception as err:
            print(f"slack: posting to {channel}: {err}")


def users_from(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(re.findall(r"[A-Z0-9]+", value.upper())))


def plain(text: str, me: str) -> str:
    """A message's text as typed: without the mention of the bot, links as their URLs, &, < and > as they are."""
    text = text.replace(f"<@{me}>", " ") if me else text
    text = re.sub(r"<((?:https?|mailto):[^|>]+)(?:\|[^>]*)?>", r"\1", text)
    return re.sub(r"[ \t]+", " ", text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")).strip()


def escape(text: str) -> str:
    """Text Slack shows as written: &, < and > are its markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _repo(url: str) -> str:
    return url.removeprefix("https://github.com/").removesuffix(".git")


def _line(task: dict[str, Any]) -> str:
    pr = f" {task['pr']}" if task.get("pr") else ""
    return f"`{task['slug']}` {task['state']}: {_repo(str(task['repo'])) if task.get('repo') else 'a question'}{pr}"
