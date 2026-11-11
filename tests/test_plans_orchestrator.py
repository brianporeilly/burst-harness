"""Plans in the orchestrator: decided from the page, the API or Slack's buttons, and applied once everything is
decided (orchestrator/jobs.py, slack.py). Runs are faked as processes; nothing calls a model or Slack."""

import json
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from harness import job, plans
from orchestrator import launch as launch_module
from orchestrator.app import create_app
from orchestrator.jobs import JobRunner, JobsError
from orchestrator.launch import LaunchError
from orchestrator.servers import ModelServers
from orchestrator.slack import PLAN_SHOWN, SlackBot, plan_message

from .test_orchestrator import CHANGE, FakeBackend

PLAN_RUN = "20261110T080000Z-aaaa"
ITEMS = [{"id": "a", "title": "Archive 4 from news@example.com", "detail": "Digests"},
         {"id": "b", "title": "Archive 1 from boss@example.com"}]
ALLOWED = "U0ALLOWED"


def _mail(tmp_path, outputs='["slack:C123"]'):
    root = tmp_path / "jobs"
    (root / "mail").mkdir(parents=True)
    (root / "mail" / "prompt.md").write_text("sort it")
    # A model server of ours: applying calls no model, so it is not taken.
    (root / "mail" / "job.toml").write_text(
        'provider = "llamacpp"\nbase_url = "http://127.0.0.1:9"\n'
        f'[job]\nmode = "act"\napply = "true"\noutput = {outputs}\n')
    runner = JobRunner([root], servers=ModelServers())
    cfg = job.config(job.load(root / "mail"))
    plans.store(cfg, "mail", PLAN_RUN, plans.parse(json.dumps({"items": ITEMS})))
    return runner, cfg


class Applies:
    """Stands in for `harness job apply` processes: each, when the test lets it end, leaves the record the harness
    would, failed unless told otherwise."""

    def __init__(self, cfg):
        self.cfg, self.started, self.end = cfg, [], threading.Event()
        self.ended = threading.Event()

    def __call__(self, argv, **kwargs):
        self.started.append((argv, kwargs["env"]))
        applies = self

        class Process:
            pid = 4242

            def wait(self):
                applies.end.wait(10)
                run_id = kwargs["env"][job.RUN_ID_ENV]
                job._write_run(job.runs_dir(applies.cfg, "mail") / run_id,
                               {"id": run_id, "job": "mail", "applies": argv[-1], "state": "failed",
                                "reason": "quota exceeded"})
                return 1

            def poll(self):
                return 1 if applies.end.is_set() else None

        return Process()


def _settle(runner):
    for _ in range(250):
        if not runner.running:
            return
        threading.Event().wait(0.02)
    raise AssertionError("still running")


def test_deciding_everything_starts_the_apply_run_once_and_watchers_hear_how_it_goes(tmp_path, monkeypatch):
    runner, cfg = _mail(tmp_path)
    applies = Applies(cfg)
    monkeypatch.setattr(launch_module.subprocess, "Popen", applies)
    heard = []
    runner.watch(lambda name, event, plan: heard.append((event, plan["state"])))

    half = runner.decide("mail", PLAN_RUN, {"a": "approve"}, by="page")
    assert half["state"] == "open" and "apply_run" not in half and applies.started == []
    done = runner.decide("mail", PLAN_RUN, {"*": "reject"}, by="slack:U1")
    assert done["state"] == "applying"
    (argv, env), = applies.started
    assert argv[-4:] == ["apply", str(runner.discover()["mail"]), "--plan", PLAN_RUN]
    assert env[job.TRIGGER_ENV] == "approval" and env[job.RUN_ID_ENV] == done["apply_run"]
    assert runner.servers.status() == {}  # no model server taken to apply
    kept = plans.load(cfg, "mail", PLAN_RUN)
    assert kept["state"] == "applying" and kept["apply_run"] == done["apply_run"]  # marked before it started
    with pytest.raises(JobsError, match="is applying"):
        runner.decide("mail", PLAN_RUN, {"b": "approve"}, by="page")
    assert runner.job("mail")["plan"] == {"run": PLAN_RUN, "state": "applying", "items": 2, "undecided": 0,
                                          "expires": kept["expires"]}

    applies.end.set()  # the run ends without the harness closing the plan: it is apply_failed, with why
    _settle(runner)
    for _ in range(250):
        if heard and heard[-1][0] == "apply_failed":
            break
        threading.Event().wait(0.02)
    assert [event for event, _ in heard] == ["decided", "decided", "applying", "apply_failed"]
    failed = plans.load(cfg, "mail", PLAN_RUN)
    assert failed["state"] == "apply_failed" and failed["reason"] == "quota exceeded"
    assert runner.job("mail")["plan"] is None and runner.job("mail")["plans"][0]["state"] == "apply_failed"


def test_an_apply_that_cannot_start_leaves_the_plan_decided_to_apply_later(tmp_path, monkeypatch):
    runner, cfg = _mail(tmp_path)

    def broken(*args, **kwargs):
        raise LaunchError("no kubectl")

    monkeypatch.setattr(runner.launcher, "start", broken)
    done = runner.decide("mail", PLAN_RUN, {"*": "approve"}, by="page")
    assert done["state"] == "open" and "no kubectl" in done["apply_error"]
    assert plans.load(cfg, "mail", PLAN_RUN)["state"] == "open"
    with pytest.raises(JobsError, match="no kubectl"):
        runner.apply("mail", PLAN_RUN)


def test_plans_over_the_api(tmp_path, monkeypatch):
    runner, cfg = _mail(tmp_path)
    monkeypatch.setattr(launch_module.subprocess, "Popen", Applies(cfg))
    client = TestClient(create_app(FakeBackend(), runner), base_url="http://127.0.0.1:8700")
    listed = client.get("/api/jobs").json()["jobs"][0]
    assert listed["mode"] == "act" and listed["plan"]["undecided"] == 2
    assert client.get("/api/jobs/mail").json()["plans"][0]["run"] == PLAN_RUN
    assert client.get(f"/api/jobs/mail/plans/{PLAN_RUN}").json()["items"][0]["title"].startswith("Archive 4")
    path = f"/api/jobs/mail/plans/{PLAN_RUN}/decide"
    assert client.post(path, json={"decisions": {"a": "approve"}}).status_code == 403  # no X-Orchestrator header
    assert client.post(path, json={"decisions": {"zz": "approve"}}, headers=CHANGE).status_code == 400
    assert client.post(path, json={}, headers=CHANGE).status_code == 400
    decided = client.post(path, json={"decisions": {"a": "approve", "b": "reject"}}, headers=CHANGE).json()
    assert decided["state"] == "applying" and decided["items"][0]["by"] == "page"
    assert client.post(f"/api/jobs/mail/plans/{PLAN_RUN}/apply", headers=CHANGE).status_code == 400  # running


class FakeWeb:
    def __init__(self):
        self.calls = []

    def chat_postMessage(self, **message):
        self.calls.append(("post", message))
        return {"channel": "C123", "ts": "111.1"}

    def chat_update(self, **message):
        self.calls.append(("update", message))

    def chat_postEphemeral(self, **message):
        self.calls.append(("whisper", message))


def _click(user, item, action="plan-approve", run=PLAN_RUN):
    return SimpleNamespace(type="interactive", envelope_id="e1", payload={
        "type": "block_actions", "user": {"id": user}, "channel": {"id": "C123"},
        "actions": [{"action_id": action, "value": json.dumps({"job": "mail", "run": run, "item": item})}]})


def test_a_plan_is_posted_with_buttons_decided_by_the_listed_users_and_the_message_kept_up_to_date(tmp_path, monkeypatch):
    runner, cfg = _mail(tmp_path)
    monkeypatch.setattr(launch_module.subprocess, "Popen", Applies(cfg))
    bot = SlackBot(None, (ALLOWED,), jobs=runner)
    bot.web, bot.me = FakeWeb(), "UBOT"

    def flush():
        while not bot.plans.empty():
            bot.show_plan(*bot.plans.get())

    runner._tell("mail", "proposed", plans.load(cfg, "mail", PLAN_RUN))
    flush()
    (kind, posted), = bot.web.calls
    assert kind == "post" and posted["channel"] == "C123" and posted["text"] == "mail proposes 2 actions"
    assert plans.load(cfg, "mail", PLAN_RUN)["slack"] == {"channel": "C123", "ts": "111.1"}

    acks = []
    client = SimpleNamespace(send_socket_mode_response=acks.append)
    bot.on_request(client, _click("U0STRANGER", "a"))
    assert bot.web.calls[-1][0] == "whisper" and "U0STRANGER" in bot.web.calls[-1][1]["text"]
    assert plans.load(cfg, "mail", PLAN_RUN)["items"][0]["decision"] is None

    bot.on_request(client, _click(ALLOWED, "a"))
    flush()
    kind, updated = bot.web.calls[-1]
    assert kind == "update" and updated["ts"] == "111.1"
    text = json.dumps(updated["blocks"])
    assert f"approved by <@{ALLOWED}>" in text and text.count('"plan-approve"') == 2  # b's, and "the 1 left"
    bot.on_request(client, _click(ALLOWED, "*", action="plan-reject"))
    flush()
    assert plans.load(cfg, "mail", PLAN_RUN)["state"] == "applying"
    assert "Applying what was approved" in json.dumps(bot.web.calls[-1][1]["blocks"])
    assert "plan-approve" not in json.dumps(bot.web.calls[-1][1]["blocks"])  # nothing left to click
    bot.on_request(client, _click(ALLOWED, "b"))  # too late
    assert bot.web.calls[-1][0] == "whisper" and "is applying" in bot.web.calls[-1][1]["text"]
    assert len(acks) == 4


def test_a_job_without_a_slack_channel_has_its_plans_in_the_page_only(tmp_path):
    runner, cfg = _mail(tmp_path, outputs='["stdout"]')
    bot = SlackBot(None, (ALLOWED,), jobs=runner)
    bot.web = FakeWeb()
    bot.show_plan("mail", "proposed", plans.load(cfg, "mail", PLAN_RUN))
    assert bot.web.calls == []


def test_a_long_plan_shows_its_first_items_with_buttons_and_approving_the_rest_asks_first():
    items = [{"id": str(n), "title": f"Item {n}", "detail": "x" * 400, "decision": None} for n in range(30)]
    plan = {"run": PLAN_RUN, "state": "open", "expires": "2026-11-11T08:00:00Z", "items": items}
    text, blocks = plan_message("mail", plan)
    assert len(blocks) <= 50 and text == "mail proposes 30 actions"
    assert sum(1 for b in blocks if b["type"] == "actions") == PLAN_SHOWN + 1
    assert "and 8 more" in json.dumps(blocks) and "x" * 301 not in json.dumps(blocks)
    everything = blocks[-1]["elements"][0]
    assert everything["text"]["text"] == "Approve the 30 left" and everything["confirm"]
    _, done = plan_message("mail", {**plan, "state": "apply_failed", "reason": "quota <exceeded>",
                                    "items": [{**i, "decision": "approve", "by": "page"} for i in items]})
    assert "Applying it failed: quota &lt;exceeded&gt;" in done[0]["text"]["text"]
    assert not any(b["type"] == "actions" for b in done)
