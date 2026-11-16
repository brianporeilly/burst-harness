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

A task started in the page or with the API, with "Tell me in Slack" (`notify`), is told to each listed user
in a direct message when a burst of it ends, unless it was cancelled: how it ended, as above. Its later bursts
are told in that message's thread, and a reply there continues it, as in a task started from Slack.

A job in act mode that posts to a channel (`slack:CHANNEL` among its outputs) has its plans posted there too,
with buttons to approve or reject each item, or all that are left (harness/plans.py). The listed users' clicks
decide; the message shows the decisions, and how applying them went, as they happen. Once every item is decided
with something approved, the apply run starts. The message's place is kept with the plan, so a restart loses
nothing. The Slack app needs Interactivity on (with Socket Mode, no request URL).

A job from a repo of jobs whose access changes, or that is new there, waits for Accept (sources.py). Each
listed user is told in a direct message: what changed, and an Accept button for that commit when the message
can show the whole change (otherwise the Jobs tab has it). The message is kept up to date: accepted (here or
in the page), changed again (a new message), or no longer waiting. One that waited while Slack was off is
told once it connects.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections import OrderedDict
from typing import Any

from harness import credentials

from .jobs import JobRunner, JobsError
from .sources import SourcesError
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
    "push_failed": ":x: The push to `{branch}` was refused; the commits are kept in its sandbox. Fix access and continue it to push them.",
    "paused": ":double_vertical_bar: Paused, with the work pushed to `{branch}`.",
    "waiting": ":question: It needs an answer.",
    "abandoned": ":x: It gave up.",
    "max_tokens": ":warning: Stopped: the model's replies kept hitting the output limit. `continue` carries on in the "
                  "same sandbox (a lower effort or another model may help).",
    "cancelled": ":stop_sign: Cancelled.",
    "interrupted": ":warning: Cut short by orchestrator restarts.",
}


REVIEW_VALUE = 300  # characters of a setting's value in a review's message
REVIEW_TEXT = 2800  # of its list of changes: a section's text is at most 3,000
REVIEW_STATES = {
    "accepted": ":white_check_mark: Accepted by {by}: it runs as it is at `{commit}` from now on.",
    "withdrawn": "No longer waits: the repo put its access back as accepted, or removed the job.",
    "superseded": "Changed again since: a newer message has it.",
}
PLAN_SHOWN = 22  # items with their own buttons: two blocks each, under Slack's 50 a message
DETAIL_CHARS = 300
PLAN_STATES = {
    "applying": ":hourglass_flowing_sand: Applying what was approved.",
    "applied": ":white_check_mark: Applied what was approved{run}.",
    "apply_failed": ":x: Applying it failed{reason}.",
    "rejected": "All rejected: nothing will be done.",
    "superseded": "Superseded by a newer plan: nothing more will be done with this one.",
    "expired": "Expired, undecided: nothing will be done.",
}


class SlackBot:
    def __init__(self, tasks: TaskRunner | None, users: tuple[str, ...], jobs: JobRunner | None = None) -> None:
        self.tasks, self.users, self.jobs = tasks, frozenset(users), jobs
        self.tokens: tuple[str, str] | None = None
        self.web: Any = None  # slack_sdk's WebClient, once there is a bot token
        self.socket: Any = None  # its SocketModeClient, once connected
        self.me = ""
        self.status = "off: needs SLACK_APP_TOKEN and SLACK_BOT_TOKEN (Settings)"
        self.outbox: queue.Queue[tuple[str, str, str]] = queue.Queue()
        self.notices: queue.Queue[tuple[str, str]] = queue.Queue()  # (task, what to say): a new direct message
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.lock = threading.Lock()
        self.plans: queue.Queue[tuple[str, str, dict[str, Any]]] = queue.Queue()
        self.reviews: queue.Queue[tuple[str, str, str, dict[str, Any]]] = queue.Queue()
        self.sources = jobs.sources if jobs is not None else None
        if tasks is not None:
            tasks.watch(self.burst)
        if jobs is not None:
            jobs.watch(self.plan_changed)
        if self.sources is not None:
            self.sources.watch(self.review_changed)

    # --- connecting ---

    def start(self) -> None:
        threading.Thread(target=self._supervise, daemon=True, name="slack").start()
        threading.Thread(target=self._post, daemon=True, name="slack-posts").start()
        threading.Thread(target=self._post_notices, daemon=True, name="slack-notices").start()
        threading.Thread(target=self._post_plans, daemon=True, name="slack-plans").start()
        threading.Thread(target=self._post_reviews, daemon=True, name="slack-reviews").start()

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
        self.untold()

    def untold(self) -> None:
        """Queue what waits for Accept and was never said (Slack was off, or no one was listed)."""
        for item in self.sources.waiting() if self.sources is not None else []:
            if not item.get("notices"):
                self.reviews.put(("waiting", str(item["source"]), str(item["job"]), item))

    def on_request(self, client: Any, request: Any) -> None:
        """A Socket Mode envelope: acknowledged at once (or Slack sends it again), then handled."""
        from slack_sdk.socket_mode.response import SocketModeResponse

        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        if request.type == "interactive" and request.payload.get("type") == "block_actions":
            if any(a.get("action_id") == "job-accept" for a in request.payload.get("actions") or []):
                self.accept_clicked(request.payload)
            else:
                self.clicked(request.payload)
            return
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
        if self.tasks is None:
            self.say(channel, answer_in, "Tasks are not on in this orchestrator: here it only posts jobs' plans.")
            return
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
        if isinstance(where, dict) and where.get("channel") and where.get("thread"):
            self.outbox.put((where["channel"], where["thread"], self._about(event, record)))
            return
        if not record.get("notify"):
            return
        if record.get("dm"):  # told before: in the threads it was told in
            for told in record["dm"]:
                self.outbox.put((told["channel"], told["thread"], self._about(event, record)))
        elif event == "ended" and record.get("state") != "cancelled":
            self.notices.put((str(record["slug"]), self._heading(record) + "\n" + self._about(event, record)))

    def _heading(self, record: dict[str, Any]) -> str:
        what = f"task `{record['slug']}` on {_repo(str(record['repo']))}" if record.get("repo") else \
            f"question `{record['slug']}`"
        by = str(record.get("created_by") or "")
        return f"*{what[0].upper() + what[1:]}*" + (f", started by {escape(by)}" if by else "") + ":"

    def _post_notices(self) -> None:
        while True:
            self.tell(*self.notices.get())

    def tell(self, slug: str, text: str) -> None:
        """A task's first word in Slack: a direct message to each listed user, whose threads it is told in from
        then on."""
        if self.web is None or not self.users or self.tasks is None:
            print(f"slack: not connected, or no one listed, so {slug} was not told: {text[:200]}")
            return
        threads = []
        for user in sorted(self.users):
            try:
                posted = self.web.chat_postMessage(channel=user, text=text, unfurl_links=False)
                threads.append({"channel": str(posted["channel"]), "thread": str(posted["ts"])})
            except Exception as err:  # noqa: BLE001 -- Slack's trouble is said, not raised into the task
                print(f"slack: telling {user} of {slug}: {err}")
        if threads:
            try:
                self.tasks.told_in(slug, threads)
            except TasksError as err:  # removed meanwhile
                print(f"slack: {slug}: {err}")

    def _about(self, event: str, record: dict[str, Any]) -> str:
        count = len(record.get("bursts") or [])
        if event == "started":
            if not record.get("repo"):
                return ":hourglass_flowing_sand: Working on " + ("your follow-up." if record.get("message") else "it.")
            if record.get("by_merge"):
                return (f":arrow_forward: The PR was merged, so round {len(record.get('rounds') or []) + 1} started "
                        f"by itself on `{record.get('branch')}`.")
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

    # --- jobs' plans ---

    def plan_changed(self, name: str, event: str, plan: dict[str, Any]) -> None:
        """JobRunner's watcher: queued, and posted (or the message updated) from the plans thread."""
        self.plans.put((name, event, plan))

    def _post_plans(self) -> None:
        while True:
            self.show_plan(*self.plans.get())

    def show_plan(self, name: str, event: str, plan: dict[str, Any]) -> None:
        """Post a proposed plan to the job's channel, or update the message it was posted as."""
        if self.jobs is None or self.web is None:
            return
        try:
            plan = self.jobs.plan(name, str(plan["run"]))  # as it is now, with where it was posted
            channel = next((o.split(":", 1)[1] for o in self.jobs.job(name).get("outputs") or []
                            if o.startswith("slack:")), "")
        except JobsError as err:
            print(f"slack: {name}'s plan {plan.get('run')}: {err}")
            return
        text, blocks = plan_message(name, plan)
        where = plan.get("slack") or {}
        try:
            if where.get("channel") and where.get("ts"):
                self.web.chat_update(channel=where["channel"], ts=where["ts"], text=text, blocks=blocks)
            elif channel and event == "proposed":
                posted = self.web.chat_postMessage(channel=channel, text=text, blocks=blocks, unfurl_links=False)
                self.jobs.note(name, str(plan["run"]), slack={"channel": posted["channel"], "ts": posted["ts"]})
        except Exception as err:  # noqa: BLE001 -- Slack's trouble is said, not raised into the jobs
            print(f"slack: posting {name}'s plan {plan['run']}: {err}")

    # --- jobs waiting for Accept ---

    def review_changed(self, event: str, source: str, job: str, item: dict[str, Any]) -> None:
        """JobSources' watcher: queued, and said from the reviews thread."""
        self.reviews.put((event, source, job, item))

    def _post_reviews(self) -> None:
        while True:
            self.show_review(*self.reviews.get())

    def show_review(self, event: str, source: str, job: str, item: dict[str, Any]) -> None:
        """Tell the listed users that a job waits for Accept, or bring the messages that said so up to date."""
        if self.web is None:
            return  # said once Slack connects (untold), if it still waits
        try:
            if event == "waiting":
                done = review_message(source, job, item, "superseded")
                for where in item.get("replaces") or []:
                    self.web.chat_update(channel=where["channel"], ts=where["ts"], text=done[0], blocks=done[1])
                if not self.users:
                    return
                text, blocks = review_message(source, job, item)
                notices = []
                for user in sorted(self.users):
                    posted = self.web.chat_postMessage(channel=user, text=text, blocks=blocks, unfurl_links=False)
                    notices.append({"channel": str(posted["channel"]), "ts": str(posted["ts"])})
                if self.sources is not None:
                    self.sources.note(source, job, str(item["commit"]), notices)
            else:
                text, blocks = review_message(source, job, item, None if event == "moved" else event)
                for where in item.get("notices") or []:
                    self.web.chat_update(channel=where["channel"], ts=where["ts"], text=text, blocks=blocks)
        except Exception as err:  # noqa: BLE001 -- Slack's trouble is said, not raised into the review
            print(f"slack: telling of {source}/{job} waiting for Accept: {err}")

    def accept_clicked(self, payload: dict[str, Any]) -> None:
        """A review message's Accept: the listed users' only, for the commit the message showed."""
        user = str((payload.get("user") or {}).get("id") or "")
        channel = str((payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get("channel_id") or "")
        action = next(a for a in payload.get("actions") or [] if a.get("action_id") == "job-accept")
        if user not in self.users:
            self.whisper(channel, user, f"Only the people this orchestrator lists can accept. Your Slack ID is "
                                        f"`{user}`: add it to {USERS_ENV} to be one.")
            return
        try:
            which = json.loads(action.get("value") or "{}")
            if self.sources is None:
                raise SourcesError("There are no repos of jobs in this orchestrator.")
            self.sources.accept(str(which["source"]), str(which["job"]), str(which["commit"]), user=f"slack:{user}")
        except (SourcesError, ValueError, KeyError) as err:
            self.whisper(channel, user, f":warning: {escape(str(err))}")

    def clicked(self, payload: dict[str, Any]) -> None:
        """A button of a plan's message: the listed users' decide."""
        user = str((payload.get("user") or {}).get("id") or "")
        channel = str((payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get("channel_id") or "")
        for action in payload.get("actions") or []:
            if action.get("action_id") not in ("plan-approve", "plan-reject"):
                continue
            if user not in self.users:
                self.whisper(channel, user, f"Only the people this orchestrator lists can decide. Your Slack ID is "
                                            f"`{user}`: add it to {USERS_ENV} to be one.")
                return
            try:
                which = json.loads(action.get("value") or "{}")
                decision = "approve" if action["action_id"] == "plan-approve" else "reject"
                if self.jobs is None:
                    raise JobsError("Jobs are not on in this orchestrator.")
                done = self.jobs.decide(str(which["job"]), str(which["run"]), {str(which["item"]): decision},
                                        by=f"slack:{user}")
            except (JobsError, ValueError, KeyError) as err:
                self.whisper(channel, user, f":warning: {escape(str(err))}")
                return
            if done.get("apply_error"):
                self.whisper(channel, user, f"Decided, but not applied yet: {escape(done['apply_error'])} "
                                            "It can be applied from the Jobs tab once that is over.")

    def whisper(self, channel: str, user: str, text: str) -> None:
        if self.web is None or not channel:
            return
        try:
            self.web.chat_postEphemeral(channel=channel, user=user, text=text)
        except Exception as err:  # noqa: BLE001
            print(f"slack: telling {user} in {channel}: {err}")

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


def plan_message(name: str, plan: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """A plan as a message: its text (for notifications), and blocks with a pair of buttons per item while it is
    open."""
    items, state, run = plan["items"], plan["state"], plan["run"]
    many = f"{len(items)} action{'s' if len(items) != 1 else ''}"
    text = f"{name} proposes {many}"
    head = f"*{escape(name)}* proposes {many} (run `{run}`)."
    if state == "open":
        head += f" Open until {plan.get('expires', '?')}."
    else:
        head += " " + PLAN_STATES.get(state, state).format(
            run=f" (run `{plan['apply_run']}`)" if plan.get("apply_run") else "",
            reason=f": {escape(str(plan['reason']))}" if plan.get("reason") else "")
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": head}}]

    def button(label: str, action: str, item: str, style: str) -> dict[str, Any]:
        return {"type": "button", "text": {"type": "plain_text", "text": label}, "style": style,
                "action_id": f"plan-{action}", "value": json.dumps({"job": name, "run": run, "item": item})}

    for item in items[:PLAN_SHOWN]:
        detail = escape(item.get("detail") or "")
        detail = detail[:DETAIL_CHARS] + ("…" if len(detail) > DETAIL_CHARS else "")
        mark = {"approve": ":white_check_mark:", "reject": ":heavy_multiplication_x:"}.get(item["decision"], ":black_small_square:")
        who = ""
        if item["decision"]:
            by = str(item.get("by") or "")
            who = f"\n_{'approved' if item['decision'] == 'approve' else 'rejected'} by " \
                  f"{f'<@{by[6:]}>' if by.startswith('slack:') else escape(by)}_"
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                   "text": f"{mark} *{escape(item['title'])}*" + (f"\n{detail}" if detail else "") + who}})
        if state == "open" and not item["decision"]:
            blocks.append({"type": "actions", "elements": [button("Approve", "approve", item["id"], "primary"),
                                                           button("Reject", "reject", item["id"], "danger")]})
    if len(items) > PLAN_SHOWN:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                       f"…and {len(items) - PLAN_SHOWN} more, in the orchestrator's Jobs tab."}]})
    left = sum(1 for item in items if not item["decision"])
    if state == "open" and left:
        everything = button(f"Approve the {left} left", "approve", "*", "primary")
        everything["confirm"] = {"title": {"type": "plain_text", "text": "Approve all that are left?"},
                                 "text": {"type": "mrkdwn", "text": f"{left} item{'s' if left != 1 else ''}, as listed. "
                                          "Once everything is decided, what was approved is done."},
                                 "confirm": {"type": "plain_text", "text": "Approve"},
                                 "deny": {"type": "plain_text", "text": "Not yet"}}
        blocks.append({"type": "actions", "elements": [everything, button(f"Reject the {left} left", "reject", "*", "danger")]})
    return text, blocks


def review_message(source: str, job: str, item: dict[str, Any], state: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    """A job waiting for Accept as a message: its text, and blocks with what changed, and while it waits
    (state None) an Accept button if the whole change is shown."""
    commit = str(item.get("commit") or "")[:12]
    what = "is new to the repo" if item.get("new") else "changes its access"
    text = f"{job} {what}, and waits for your Accept"
    head = f":lock: *{escape(job)}* (repo of jobs *{escape(source)}*) {what} at `{commit}`."
    lines, whole = [], True
    for change in item.get("changes") or []:
        values = []
        for value in (change.get("before"), change.get("after")):
            shown = "not set" if value is None else json.dumps(value, sort_keys=True)
            if len(shown) > REVIEW_VALUE:
                shown, whole = shown[:REVIEW_VALUE] + "…", False
            values.append(f"`{escape(shown)}`" if value is not None else "_not set_")
        lines.append(f"• *{escape(str(change.get('setting')))}*: {values[0]} → {values[1]}")
    body = "\n".join(lines) or "_no settings_"
    if len(body) > REVIEW_TEXT:
        body, whole = body[:REVIEW_TEXT].rsplit("\n", 1)[0] + "\n…", False
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": head}},
                                    {"type": "section", "text": {"type": "mrkdwn", "text": body}}]
    if state is not None:
        by = str(item.get("by") or "")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": REVIEW_STATES[state].format(
            commit=commit, by=f"<@{by[6:]}>" if by.startswith("slack:") else escape(by or "the page"))}})
        return text, blocks
    note = ("Until it is accepted it runs as it was last accepted" if not item.get("new") else
            "It does not run until it is accepted")
    if not whole:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
                       f"{note}. This is not the whole change: look at it, and accept it, in the Jobs tab."}]})
        return text, blocks
    button = {"type": "button", "text": {"type": "plain_text", "text": "Accept"}, "style": "primary",
              "action_id": "job-accept",
              "value": json.dumps({"source": source, "job": job, "commit": item.get("commit")}),
              "confirm": {"title": {"type": "plain_text", "text": f"Accept {job[:40]}?"},
                          "text": {"type": "mrkdwn", "text": f"It runs with the access above, from `{commit}`."},
                          "confirm": {"type": "plain_text", "text": "Accept"},
                          "deny": {"type": "plain_text", "text": "Not yet"}}}
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"{note}: here, or in the Jobs tab."}]})
    blocks.append({"type": "actions", "elements": [button]})
    return text, blocks


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
