"""Act mode: a run proposes a plan, a person approves items of it, and the job's apply command does only what
was approved (harness/plans.py, job.apply). No model: the fake `claude` of test_job writes the plan."""

import json
import time

import pytest

from harness import cli, job, plans

from .test_job import _local, _write_job, fake_claude  # noqa: F401 -- the fixture

ACT = """
engine = "claude-code"
[job]
mode = "act"
apply = "cp approved.json applied.json && echo Archived $(grep -c '\\"id\\"' approved.json) groups"
apply_writes = [{host = "gmail.googleapis.com", method = "POST", path = "/gmail/v1/users/me/messages/batchModify"}]
output = ["stdout", "file:REPORTS/brief.md"]
"""
PLAN = {"items": [
    {"id": "sender:news@example.com", "title": "Archive 40 messages from news@example.com",
     "detail": "Weekly digests", "data": {"ids": ["a1", "a2"]}},
    {"id": "sender:boss@example.com", "title": "Archive 2 messages from boss@example.com", "data": {"ids": ["b1"]}},
    {"id": "sender:shop@example.com", "title": "Archive 9 receipts from shop@example.com", "data": {"ids": ["c1"]}},
]}


def _act(tmp_path, monkeypatch, plan=PLAN, toml=ACT):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "report")
    monkeypatch.setenv("FAKE_CLAUDE_PLAN", json.dumps(plan) if isinstance(plan, dict) else plan)
    spec = job.load(_write_job(tmp_path, toml.replace("REPORTS", str(tmp_path / "reports")), name="mail"))
    return spec, job.config(spec)


def test_a_plan_is_proposed_decided_and_only_what_was_approved_is_done(tmp_path, monkeypatch, fake_claude, capsys):  # noqa: F811 -- the fixture
    spec, cfg = _act(tmp_path, monkeypatch)
    monkeypatch.setenv(job.RUN_ID_ENV, "20261110T080000Z-aaaa")
    assert job.run(spec, cfg, acquire=_local(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "The plan (3 items) waits for approval" in out
    rules = fake_claude()[0]["argv"][fake_claude()[0]["argv"].index("--append-system-prompt") + 1]
    assert "Acting: this job does nothing itself; it proposes" in rules and f"{tmp_path}/work/plan.json" in rules
    (record,) = job.list_runs(cfg, "mail")
    assert record["plan"]["items"] == 3 and record["model"]

    plan = plans.load(cfg, "mail")
    assert plan["state"] == "open" and [i["decision"] for i in plan["items"]] == [None] * 3
    with pytest.raises(job.JobError, match="not decided yet"):
        job.apply(spec, cfg, acquire=_local(tmp_path))
    assert plans.load(cfg, "mail")["state"] == "open"

    assert cli.main(["job", "approve", str(spec.path), "sender:news@example.com", "sender:shop@example.com"]) == 0
    assert cli.main(["job", "reject", str(spec.path)]) == 0  # all that is left
    assert "Every item is decided" in capsys.readouterr().out
    decided = plans.load(cfg, "mail")["items"]
    assert [i["decision"] for i in decided] == ["approve", "reject", "approve"]
    assert decided[0]["by"].startswith("cli:") and decided[0]["at"]

    code = job.apply(spec, cfg, acquire=_local(tmp_path), run_id="20261110T090000Z-bbbb")
    out = capsys.readouterr().out
    assert code == 0 and "----- mail applied -----\nArchived 2 groups" in out
    applied = json.loads((tmp_path / "work" / "applied.json").read_text())
    assert [i["id"] for i in applied] == ["sender:news@example.com", "sender:shop@example.com"]
    assert applied[0] == {"id": "sender:news@example.com", "title": "Archive 40 messages from news@example.com",
                          "data": {"ids": ["a1", "a2"]}}
    done = plans.load(cfg, "mail")
    assert done["state"] == "applied" and done["apply_run"] == "20261110T090000Z-bbbb"
    newest = job.list_runs(cfg, "mail")[0]
    assert newest["engine"] == "apply" and newest["applies"] == "20261110T080000Z-aaaa" and "model" not in newest
    assert (tmp_path / "reports" / "brief.md").read_text().startswith("# Digest")  # not overwritten by the apply
    assert len(fake_claude()) == 1  # no model was asked to apply anything
    with pytest.raises(job.JobError, match="is applied: there is nothing to apply"):
        job.apply(spec, cfg, acquire=_local(tmp_path))


def test_the_plan_run_is_read_only_and_the_apply_run_may_make_only_its_writes(tmp_path, monkeypatch):
    spec, cfg = _act(tmp_path, monkeypatch)
    planning = job.write_rules(spec, cfg)
    assert planning is not None and not any("gmail" in w["host"] for w in planning)
    applying = job.write_rules(job.replace(spec, applying="20261110T080000Z-aaaa"), cfg)
    assert applying == (*job.RUN_WRITES, {"host": "gmail.googleapis.com", "method": "POST",
                                          "path": "/gmail/v1/users/me/messages/batchModify"})
    assert job._slug(job.replace(spec, applying="x")) == "apply-mail"
    assert "apply    cp approved.json" in job.describe(spec, cfg) and "batchModify" in job.describe(spec, cfg)


def test_a_failed_apply_is_reported_and_the_plan_is_not_applied_again(tmp_path, monkeypatch, fake_claude, capsys):  # noqa: F811 -- the fixture
    spec, cfg = _act(tmp_path, monkeypatch, toml=ACT.replace("cp approved.json", "echo quota exceeded >&2; exit 4; cp x"))
    assert job.run(spec, cfg, acquire=_local(tmp_path)) == 0
    plans.decide(cfg, "mail", plans.load(cfg, "mail")["run"], {"*": "approve"}, by="test")
    assert job.apply(spec, cfg, acquire=_local(tmp_path)) == 1
    assert "! Applying mail's plan failed: " in capsys.readouterr().out
    plan = plans.load(cfg, "mail")
    assert plan["state"] == "apply_failed" and "exited with status 4: quota exceeded" in plan["reason"]


def test_an_apply_that_cannot_start_leaves_the_plan_to_apply_again(tmp_path, monkeypatch, fake_claude):  # noqa: F811 -- the fixture
    spec, cfg = _act(tmp_path, monkeypatch)
    assert job.run(spec, cfg, acquire=_local(tmp_path)) == 0
    plans.decide(cfg, "mail", plans.load(cfg, "mail")["run"], {"*": "approve"}, by="test")
    monkeypatch.setattr(job, "check", lambda spec: (_ for _ in ()).throw(job.JobError("needs GOOGLE_REFRESH_TOKEN")))
    with pytest.raises(job.JobError, match="GOOGLE_REFRESH_TOKEN"):
        job.apply(spec, cfg, acquire=_local(tmp_path))
    plan = plans.load(cfg, "mail")
    assert plan["state"] == "open" and "apply_run" not in plan


def test_a_new_plan_supersedes_an_open_one_and_plans_expire(tmp_path, monkeypatch, fake_claude):  # noqa: F811 -- the fixture
    spec, cfg = _act(tmp_path, monkeypatch)
    for n in range(2):
        monkeypatch.setenv(job.RUN_ID_ENV, f"20261110T08000{n}Z-aaaa")
        assert job.run(spec, cfg, acquire=_local(tmp_path)) == 0
    newer, older = plans.list_plans(cfg, "mail")
    assert (newer["state"], older["state"]) == ("open", "superseded")
    with pytest.raises(plans.PlanError, match="is superseded"):
        plans.decide(cfg, "mail", older["run"], {"*": "approve"}, by="test")
    monkeypatch.setattr(plans.time, "time", lambda: time.time_ns() / 1e9 + plans.DEFAULT_TTL + 60)
    assert plans.load(cfg, "mail")["state"] == "expired"
    with pytest.raises(plans.PlanError, match="is expired"):
        plans.decide(cfg, "mail", newer["run"], {"*": "approve"}, by="test")


def test_the_next_run_is_shown_what_was_decided_so_it_does_not_propose_a_rejection_again(tmp_path, monkeypatch, fake_claude):  # noqa: F811 -- the fixture
    spec, cfg = _act(tmp_path, monkeypatch)
    monkeypatch.setenv(job.RUN_ID_ENV, "20261110T080000Z-aaaa")
    assert job.run(spec, cfg, acquire=_local(tmp_path)) == 0
    assert json.loads((tmp_path / "work" / plans.DECISIONS_FILE).read_text()) == {"decided": []}  # the first run
    plans.decide(cfg, "mail", "20261110T080000Z-aaaa", {"sender:boss@example.com": "reject"}, by="test")
    time.sleep(1.1)  # decisions are to the second
    plans.decide(cfg, "mail", "20261110T080000Z-aaaa", {"sender:news@example.com": "approve"}, by="test")

    monkeypatch.setenv(job.RUN_ID_ENV, "20261111T080000Z-cccc")
    assert job.run(spec, cfg, acquire=_local(tmp_path)) == 0
    shown = json.loads((tmp_path / "work" / plans.DECISIONS_FILE).read_text())["decided"]
    assert [(d["id"], d["decision"]) for d in shown] == [("sender:news@example.com", "approve"),
                                                         ("sender:boss@example.com", "reject")]  # newest first
    assert shown[1]["title"] == "Archive 2 messages from boss@example.com" and shown[1]["plan_state"] == "open"  # as the run began
    rules = fake_claude()[-1]["argv"][fake_claude()[-1]["argv"].index("--append-system-prompt") + 1]
    assert f"{tmp_path}/work/{plans.DECISIONS_FILE} lists what a person decided" in rules

    # A later decision on the same item is the one shown; one older than DECIDED_DAYS is not.
    plans.decide(cfg, "mail", "20261111T080000Z-cccc", {"sender:boss@example.com": "approve"}, by="test")
    assert {d["id"]: d["decision"] for d in plans.decided(cfg, "mail")}["sender:boss@example.com"] == "approve"
    monkeypatch.setattr(plans.time, "time", lambda: time.time_ns() / 1e9 + (plans.DECIDED_DAYS + 1) * 86400)
    assert plans.decided(cfg, "mail") == []


def test_rejecting_everything_closes_the_plan(tmp_path, monkeypatch, fake_claude):  # noqa: F811 -- the fixture
    spec, cfg = _act(tmp_path, monkeypatch)
    assert job.run(spec, cfg, acquire=_local(tmp_path)) == 0
    run_id = plans.load(cfg, "mail")["run"]
    with pytest.raises(plans.PlanError, match="has no item nope"):
        plans.decide(cfg, "mail", run_id, {"nope": "approve"}, by="test")
    assert plans.decide(cfg, "mail", run_id, {"*": "reject"}, by="test")["state"] == "rejected"


@pytest.mark.parametrize("plan, kept", [
    ("not json", "not JSON"),
    ({"items": [{"id": "a b", "title": "x"}]}, "is not unique letters"),
    ({"items": [{"id": "a", "title": "x"}, {"id": "a", "title": "y"}]}, "is not unique"),
    ({"items": [{"id": str(n), "title": "x"} for n in range(plans.MAX_ITEMS + 1)]}, "group them"),
    ({"items": []}, None),
])
def test_a_plan_that_is_not_one_is_not_kept_and_the_report_says_so(tmp_path, monkeypatch, fake_claude, capsys,  # noqa: F811 -- the fixture
                                                                    plan, kept):
    spec, cfg = _act(tmp_path, monkeypatch, plan=plan)
    assert job.run(spec, cfg, acquire=_local(tmp_path)) == 0
    assert plans.list_plans(cfg, "mail") == []
    (record,) = job.list_runs(cfg, "mail")
    if kept:
        assert kept in record["plan"]["error"] and "The plan could not be kept" in capsys.readouterr().out
    else:
        assert record["plan"] == {"items": 0}


@pytest.mark.parametrize("table, error", [
    ('mode = "act"\n', "goes with apply"),
    ('apply = "x"\n', "goes with apply"),
    ('plan_ttl = "1d"\n', "are for mode"),
    ('mode = "act"\napply = "x"\nplan_ttl = "soon"\n', "plan_ttl is a duration"),
    ('mode = "act"\napply = "x"\napply_writes = [{host = "h"}]\n', "apply_writes entry"),
])
def test_act_settings_are_checked(tmp_path, table, error):
    with pytest.raises(job.JobError, match=error):
        job.load(_write_job(tmp_path, f"[job]\n{table}"))
