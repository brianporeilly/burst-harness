"""A project's milestones (docs/project-layout.md, step 2): Accept items ticked with evidence, the plan amended
through a tool and every change recorded, finishing checked against the milestone, the PR body written from the
record, and the next milestone taken up once the last one's PR is merged."""

import json

import pytest

from harness import cli, project, terminal
from harness.config import HarnessConfig
from harness.context import HarnessContext
from harness.exec_target import LocalTarget

from .conftest import git
from .test_agents import llama  # noqa: F401 -- the fixture
from .test_project import PLAN, PROGRESS, _project_origin


@pytest.fixture
def record(tmp_path):
    (tmp_path / "PLAN.md").write_text(PLAN)
    (tmp_path / "PROGRESS.md").write_text(PROGRESS)
    return project.Record(LocalTarget(repo=str(tmp_path)))


def test_an_item_is_ticked_with_its_evidence(record):
    assert record.criteria() == {"ticked": 1, "total": 2}
    with pytest.raises(project.ProjectError, match=r"M0.1 is not an Accept item of the current milestone, M1 \(M1.1, M1.3\)"):
        record.tick("M0.1", "CI")
    with pytest.raises(project.ProjectError, match="M1.2 is not"):
        record.tick("M1.2", "struck")
    with pytest.raises(project.ProjectError, match="Give the evidence"):
        record.tick("M1.3", "  ")
    record.tick("M1.3", "`just test-wasm`:\n hashes match")
    assert record.unticked() == [] and record.criteria() == {"ticked": 2, "total": 2}
    progress = record.read("PROGRESS.md")
    assert "  - [x] M1.3 Determinism across native and WASM. — `just test-wasm`: hashes match (" in progress
    assert "  - [x] M1.1 Energy drift under 0.1% over 200 orbits. — `cargo test energy` (2026-11-16)" in progress
    assert "- [ ] M1 — Simulation core" in progress  # the milestone itself waits for its PR to be merged


def test_the_next_milestone_is_taken_up_once_the_last_ones_pr_is_merged(record):
    assert record.advance(None) is None and record.advance(record.read("PROGRESS.md")) is None  # M1.3 not ticked
    record.tick("M1.3", "`just test-wasm`")
    merged = record.read("PROGRESS.md")
    plan = PLAN + "\n### M2 — Flight toy\nAccept:\n- M2.1 A burn changes the orbit.\n"
    record.target.write_file("PLAN.md", plan)
    assert record.advance(merged) == "M2"
    progress = record.read("PROGRESS.md")
    assert "Current: M2 — Flight toy" in progress and "- [x] M1 — Simulation core" in progress
    assert "  - [ ] M2.1 A burn changes the orbit." in progress and "M1.3" not in progress
    record.tick("M2.1", "a test")
    assert record.advance(record.read("PROGRESS.md")) is None  # the last one: nothing after it
    assert record.view().startswith("Every milestone in PLAN.md is done; M2 — Flight toy was the last.")


def test_the_plan_is_amended_and_every_change_recorded(record):
    m1 = PLAN.split("### M1 — Simulation core\n", 1)[1]
    reworded = "### M1 — Simulation core\n" + m1.replace("- M1.3 Determinism across native and WASM.",
                                                         "- M1.3 Determinism across native and WASM.\n- M1.4 A replay test.")
    ident, kind = record.amend("M1", reworded, "A replay test", "Determinism needs one to show it.")
    assert (ident, kind) == ("D-001", "item")
    assert "- M1.4 A replay test." in record.read("PLAN.md") and "  - [ ] M1.4 A replay test." in record.read("PROGRESS.md")
    decision = record.read("DECISIONS.md")
    assert "## D-001 — Plan change: A replay test (M1, " in decision and "It said:" in decision

    with pytest.raises(project.ProjectError, match=r"M1.4 would be gone: strike an item through"):
        record.amend("M1", reworded.replace("- M1.4 A replay test.\n", ""), "t", "w")
    with pytest.raises(project.ProjectError, match="would not be laid out"):
        record.amend("M1", "### M1 — Simulation core\nBuild: things.\n", "t", "w")
    with pytest.raises(project.ProjectError, match="There is no M9"):
        record.amend("M9", "", "t", "w")
    with pytest.raises(project.ProjectError, match="a milestone at a time"):
        record.amend("Roadmap", "", "t", "w")

    split = ("### M1a — Integrator\nAccept:\n- M1.1 Energy drift under 0.1% over 200 orbits.\n\n"
             "### M1b — Determinism\nAccept:\n- ~~M1.2 A replay format.~~\n- M1.3 Determinism across native and "
             "WASM.\n- M1.4 A replay test.\n")
    assert record.amend("M1", split, "Split M1", "Too big for one PR.") == ("D-002", "roadmap")
    assert record.current().id == "M1a"  # the current milestone was split: its first part
    assert record.amend("after M1b", "### M1c — Replays\nAccept:\n- M1.5 Replays load.\n", "Replays", "Asked for.")[1] \
        == "roadmap"
    assert record.amend("Testing", "Every rule and every replay has a test.", "Testing", "Replays.")[1] == "roadmap"
    assert [m.id for m in record.plan().milestones] == ["M0", "M1a", "M1b", "M1c"]
    assert project.check_plan(record.read("PLAN.md"), record.read("PROGRESS.md")) == []


def test_how_the_plan_changed_is_seen_from_git_too():
    assert project.plan_change(PLAN, PLAN + "\n") == ""
    assert project.plan_change(PLAN, PLAN.replace("Kepler rails", "Kepler rails, exactly")) == "item"
    assert project.plan_change(PLAN, PLAN.replace("Every rule has a unit test.", "Most rules.")) == "roadmap"
    assert project.plan_change(PLAN, PLAN.replace("### M1 — Simulation core", "### M2 — Simulation core")
                               .replace("M1.", "M2.")) == "roadmap"


def _finish_ctx(work):
    ctx = HarnessContext(target=LocalTarget(repo=str(work)), cfg=HarnessConfig(), slug="orbit-2",
                         branch="harness/orbit-2", state_key="k", project=True)
    git(work, "checkout", "-q", "-b", ctx.branch)
    return ctx


def test_finishing_is_checked_against_the_milestone_and_the_pr_written_from_the_record(tmp_path, origin, monkeypatch):
    _project_origin(tmp_path, origin)
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    ctx = _finish_ctx(work)
    record = project.Record(ctx.target)
    record.decide("Node for WASM", "Run it under Node.", "No wasmtime.", milestone="M1")
    record.amend("M1", "### M1 — Simulation core\nBuild: Kepler rails.\nAccept:\n- M1.1 Energy drift under 0.1% "
                 "over 200 orbits.\n- ~~M1.2 A replay format.~~\n- M1.3 Determinism across native and WASM.\n"
                 "Checkpoint: Does a circular orbit look circular?\n", "Shorter build", "It said too much.")
    (work / "docs").mkdir()
    (work / "docs" / "physics.md").write_text("# How the integrator works\n")
    opened = []
    monkeypatch.setattr(terminal, "_wrap_up", lambda ctx, title: ["Pushed."])
    monkeypatch.setattr(terminal.git, "open_pr", lambda **kw: opened.append(kw) or "https://example.com/pr/2")
    ctx.repo_slug = "github.com/me/orbit"

    with pytest.raises(ValueError, match=r"(?s)aren't all ticked:\n- M1.3 Determinism.*say why in the body"):
        terminal.finish(ctx, title="M1", body="Most of M1.")
    assert terminal.finish(ctx, title="M1", body="M1.3 needs wasmtime, which the sandbox lacks.").endswith(
        "Opened PR: https://example.com/pr/2")
    body = opened[0]["body"]
    assert body.startswith("## M1 — Simulation core (1 of 2 Accept items met)")
    assert "### Plan changes: review these\n- D-002 — Plan change: Shorter build (M1, " in body
    assert "### Met\n- [x] M1.1 Energy drift under 0.1% over 200 orbits. — `cargo test energy` (2026-11-16)" in body
    assert "### Not met (the reasons are below)\n- [ ] M1.3 Determinism across native and WASM." in body
    assert "### Decisions\n- D-001 — Node for WASM (M1, " in body
    assert "### For you to try\nDoes a circular orbit look circular?" in body
    assert "Docs changed: `docs/physics.md`" in body and body.endswith("M1.3 needs wasmtime, which the sandbox lacks.")
    assert ctx.plan_change == "item"


def test_a_burst_ticks_its_milestone_and_says_so(tmp_path, origin, llama, capsys):  # noqa: F811
    _project_origin(tmp_path, origin)
    finish = {"tool": "finish_with_pr", "args": {"title": "M1", "body": "Done."}}
    url, requests = llama([
        finish,  # refused: M1.3 isn't ticked
        {"tool": "tick_criterion", "args": {"item": "M1.3", "evidence": "`just test-wasm` passes"}},
        {"tool": "amend_plan", "args": {"target": "after M1", "text": "### M2 — Flight toy\nAccept:\n- M2.1 A burn.",
                                        "title": "Add M2", "why": "The plan stopped at M1."}},
        finish,
        {"text": "Done."},
    ])
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "start", "orbit-2", "--task", "M1."])
    result = json.loads(capsys.readouterr().out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])
    refused = json.dumps(requests[1]["messages"][-1])
    assert "aren't all ticked" in refused and "M1.3 Determinism" in refused
    assert "Every Accept item of this milestone is met" in json.dumps(requests[2]["messages"][-1])
    assert result["state"] == "pushed_no_pr"
    assert result["project"] == {"milestone": "M1", "complete": True, "plan_change": "roadmap", "setup": False}
    assert result["metrics"]["criteria"] == {"ticked": 2, "total": 2}
    assert result["metrics"]["records"] == {"progress": 0, "decisions": 0, "plan_changes": 1, "backlog": 0, "ticks": 1}

    # Merged: the next round takes up M2.
    git(origin, "update-ref", "refs/heads/main", "refs/heads/harness/orbit-2")
    url, requests = llama([{"tool": "pause_for_review", "args": {"summary": "s", "next_step": "n"}}, {"text": "."}])
    work = tmp_path / "round-2"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "run", "orbit-2",
              "--branch", "harness/orbit-2-2", "--task", "Carry on."])
    out = capsys.readouterr().out
    assert "The last milestone was merged: on to M2." in out
    assert "Current milestone: M2 — Flight toy" in json.dumps(requests[0]["messages"], ensure_ascii=False)


def test_another_milestones_open_items_are_kept_and_its_ticked_ones_folded(record):
    progress = record.read("PROGRESS.md").replace(
        "- [x] M0 — Bootstrap (merged #1)",
        "- [x] M0 — Bootstrap (merged #1)\n  - [ ] M0.1 `just check` passes in CI. — Actions has never run\n"
        "  - [x] M0.2 The title screen renders in a headless test. — `just check` (2026-11-14)")
    assert "  - [ ] M0.1" in progress  # the fixture's M0 line is replaced, not missing
    record.target.write_file("PROGRESS.md", progress)
    record.tick("M1.3", "`just test-wasm`")  # any write of PROGRESS.md
    written = record.read("PROGRESS.md")
    assert "- [x] M0 — Bootstrap (merged #1)\n  - [ ] M0.1 `just check` passes in CI. — Actions has never run\n" \
           "- [ ] M1 — Simulation core" in written  # still owed, so kept; M0.2 is done, folded into M0's line
    assert "M0.2" not in written
