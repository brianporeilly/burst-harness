"""A project's record (project.py, docs/project-layout.md): PLAN.md's milestones read, PROGRESS.md, DECISIONS.md and
BACKLOG.md written through the tools, and a burst in a project starting from its current milestone."""

import pytest

from harness import cli, project
from harness.exec_target import LocalTarget

from . import fake_llama
from .conftest import git
from .test_agents import llama  # noqa: F401 -- the fixture

PLAN = """# Orbit — Plan

A one-thumb orbital toy.

## 8. Testing

Every rule has a unit test.

## 9. Roadmap

### M0 — Bootstrap
**Build:** workspace, CI, a title screen.
**Accept:**
- M0.1 `just check` passes in CI.
- M0.2 The title screen renders in a headless test.

### M1 — Simulation core
Build: Kepler rails and the integrator.
Accept:
- M1.1 Energy drift under 0.1% over 200 orbits.
- ~~M1.2 A replay format.~~
- M1.3 Determinism across native and WASM.
Checkpoint: Does a circular orbit look circular?
"""

PROGRESS = """# Progress

Current: M1 — Simulation core

## Milestones

- [x] M0 — Bootstrap (merged #1)
- [ ] M1 — Simulation core
  - [x] M1.1 Energy drift under 0.1% over 200 orbits. — `cargo test energy` (2026-11-16)

## Blockers

- CI has never run (since 2026-11-14)

## Session log

### 2026-11-16 — M1 · harness/orbit
- the integrator
Next: determinism
"""


def test_the_plan_is_read_as_milestones_with_their_criteria():
    plan = project.parse_plan(PLAN)
    assert [(m.id, m.title) for m in plan.milestones] == [("M0", "Bootstrap"), ("M1", "Simulation core")]
    m0, m1 = plan.milestones
    assert m0.build == "workspace, CI, a title screen." and [i.id for i in m0.items] == ["M0.1", "M0.2"]
    assert [(i.id, i.struck) for i in m1.items] == [("M1.1", False), ("M1.2", True), ("M1.3", False)]
    assert m1.checkpoint == "Does a circular orbit look circular?"
    assert project.check_plan(PLAN, PROGRESS) == []


def test_check_says_what_is_wrong():
    prose = PLAN.replace("**Accept:**\n- M0.1 `just check` passes in CI.\n- M0.2 The title screen renders in a "
                         "headless test.", "**Accept:** CI passes and the title renders.")
    assert project.check_plan(prose) == ["M0 has no Accept items (`- M0.1 ...` under `Accept:`)."]
    assert "M1.3 is in the plan twice." in project.check_plan(PLAN.replace("M0.2 The", "M1.3 The"))
    assert project.check_plan("# Plan\n") == ["PLAN.md has no `## Roadmap` with milestones (`### M0 — Title`)."]
    assert project.check_plan(PLAN, "# Progress\n") == ["PROGRESS.md has no `Current: M<n> — Title` line."]
    assert project.check_plan(PLAN, "Current: M7 — Gone\n") == ["PROGRESS.md's current milestone M7 is not in the plan."]


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "PLAN.md").write_text(PLAN)
    (tmp_path / "PROGRESS.md").write_text(PROGRESS)
    (tmp_path / "DECISIONS.md").write_text("# Decisions\n\n## D-007 — Pinned versions (M0, 2026-11-14)\n\nRust 1.99.\n")
    return tmp_path


def test_the_record_is_written_in_its_own_format(repo):
    record = project.Record(LocalTarget(repo=str(repo)))
    record.log("2026-11-21 10:00Z — M1 · harness/orbit-2", done="the replay golden test\n- the WASM runner")
    record.log("2026-11-21 10:00Z — M1 · harness/orbit-2", done="determinism hash", next_step="M1.3 in CI")
    record.log("2026-11-21 10:00Z — M1 · harness/orbit-2", next_step="M1.3 on WASM")
    record.add_blocker("No wasmtime in the sandbox")
    record.measure("WASM raw", "15.2 MiB", "≤ 15 MB")
    with pytest.raises(project.ProjectError, match="No blocker matches 'nothing'"):
        record.resolve_blocker("nothing")
    assert record.resolve_blocker("CI has never") == "CI has never run (since 2026-11-14)"
    assert record.decide("Node for the WASM test", "Run it under Node.", "No wasmtime.", "wasmtime") == "D-008"
    assert record.decide("Split M1", "M1a, M1b.", "Too big.", plan_change=True) == "D-009"
    record.add_backlog("A ghost of the last run")
    record.add_backlog("Haptics on burns")

    progress = (repo / "PROGRESS.md").read_text()
    assert progress.startswith("# Progress\n\nCurrent: M1 — Simulation core\n\n## Milestones\n")
    # Ticks and evidence kept; the current milestone's items listed from the plan, the struck one left out.
    assert "  - [x] M1.1 Energy drift under 0.1% over 200 orbits. — `cargo test energy` (2026-11-16)" in progress
    assert "  - [ ] M1.3 Determinism across native and WASM." in progress and "M1.2" not in progress
    assert "- [x] M0 — Bootstrap (merged #1)" in progress and "M0.1" not in progress
    assert "## Log\n" in progress and "## Session log" not in progress  # the name it is read as
    entry = progress.split("### 2026-11-21 10:00Z — M1 · harness/orbit-2\n", 1)[1].split("\n\n", 1)[0]
    assert entry == "- the replay golden test\n- the WASM runner\n- determinism hash\nNext: M1.3 on WASM"
    assert "### 2026-11-16 — M1 · harness/orbit\n- the integrator\nNext: determinism" in progress
    blockers = progress.split("## Blockers\n\n", 1)[1].split("\n\n", 1)[0]
    assert blockers.startswith("- No wasmtime in the sandbox (since ") and "CI has never" not in blockers
    assert "| Date | What | Value | Target |\n|---|---|---|---|\n| " in progress and "| WASM raw | 15.2 MiB | ≤ 15 MB |" in progress

    decisions = (repo / "DECISIONS.md").read_text()
    assert "## D-008 — Node for the WASM test (" in decisions and "- **Alternatives:** wasmtime" in decisions
    assert "## D-009 — Plan change: Split M1 (" in decisions
    backlog = (repo / "BACKLOG.md").read_text()
    assert backlog.index("## Ideas") < backlog.index("- A ghost of the last run") < backlog.index("- Haptics on burns")


def test_the_view_is_what_a_session_needs(repo):
    (repo / "FEEDBACK.md").write_text("Fuel feels too tight.\n")
    view = project.Record(LocalTarget(repo=str(repo))).view()
    assert view.startswith("Current milestone: M1 — Simulation core (1 of 2 milestones done)")
    assert "- [x] M1.1 Energy drift under 0.1% over 200 orbits. — `cargo test energy` (2026-11-16)" in view
    assert "- [ ] M1.3 Determinism across native and WASM." in view and "M1.2" not in view
    assert "Checkpoint (for the reviewer, never blocking): Does a circular orbit look circular?" in view
    assert "CI has never run" in view and "Fuel feels too tight." in view
    assert "Last log entry:\n### 2026-11-16 — M1 · harness/orbit" in view
    assert "Every rule has a unit test" not in view  # not the whole plan


def test_plan_check_on_the_command_line(repo, capsys):
    assert cli.main(["--repo", str(repo), "plan", "check"]) == 0
    assert "2 milestones, 4 Accept items. Fine." in capsys.readouterr().out
    (repo / "PROGRESS.md").write_text("# Progress\n")
    assert cli.main(["--repo", str(repo), "plan", "check"]) == 1
    assert "no `Current:" in capsys.readouterr().out


def _project_origin(tmp_path, origin):
    seed = tmp_path / "seed-project"
    git(tmp_path, "clone", "-q", str(origin), str(seed))
    (seed / "PLAN.md").write_text(PLAN)
    (seed / "PROGRESS.md").write_text(PROGRESS)
    git(seed, "add", "-A")
    git(seed, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "plan")
    git(seed, "push", "-q", "origin", "HEAD:main")


def test_a_burst_in_a_project_works_from_its_milestone_and_records_through_the_tools(tmp_path, origin, llama,  # noqa: F811
                                                                                     capsys):
    _project_origin(tmp_path, origin)
    url, requests = llama([
        {"tool": "record_progress", "args": {"done": "determinism hash on native", "blocker": "No wasmtime here",
                                             "measured_what": "Hash time", "measured_value": "3 ms"}},
        {"tool": "record_decision", "args": {"title": "Node for WASM", "what": "Run under Node.", "why": "No wasmtime."}},
        {"tool": "add_backlog", "args": {"idea": "Replays shared by link"}},
        {"tool": "pause_for_review", "args": {"summary": "M1.3 on native", "next_step": "M1.3 on WASM"}},
        {"text": "Paused."},
    ])
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    assert cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "start", "orbit-2",
                     "--task", "Carry on please."]) == 0
    out = capsys.readouterr().out
    assert "PLAN.md has a roadmap: a project" in out

    first = requests[0]
    system, prompt = (fake_llama._text(m["content"]) for m in first["messages"][:2])
    assert "This repo is a project with a plan" in system and "read_task_doc" not in system
    tools = {t["function"]["name"] for t in first["tools"]}
    assert {"current_milestone", "read_plan", "record_progress", "record_decision", "add_backlog"} <= tools
    assert not {"read_task_doc", "update_task_doc"} & tools
    assert "<project files=" in prompt and "Current milestone: M1 — Simulation core" in prompt
    assert "The person's message: Carry on please." in prompt

    branch = "harness/orbit-2"
    progress = git(origin, "show", f"{branch}:PROGRESS.md")
    entry = progress.split(f"· {branch}\n", 1)[1]
    assert entry.startswith("- determinism hash on native\n- M1.3 on native\nNext: M1.3 on WASM")
    assert "- No wasmtime here (since" in progress and "| Hash time | 3 ms |" in progress
    assert "## D-001 — Node for WASM (M1, " in git(origin, "show", f"{branch}:DECISIONS.md")
    assert "- Replays shared by link" in git(origin, "show", f"{branch}:BACKLOG.md")
    files = git(origin, "ls-tree", "-r", "--name-only", branch)
    assert "docs/tasks" not in files  # the task document is retired in a project


def test_without_a_plan_a_pause_adds_to_done_rather_than_replacing_it(tmp_path, origin, llama, capsys):  # noqa: F811
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    url, _ = llama([{"tool": "record_progress", "args": {"done": "parser written"}},
                    {"tool": "pause_for_review", "args": {"summary": "first half", "next_step": "second half"}},
                    {"text": "Paused."}])
    cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "start", "t1", "--task", "Do it."])
    url, _ = llama([{"tool": "pause_for_review", "args": {"summary": "second half", "next_step": "tests"}},
                    {"text": "Paused."}])
    cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "resume", "t1"])
    capsys.readouterr()
    doc = (work / "docs" / "tasks" / "t1.md").read_text()
    done = doc.split("## Done\n\n", 1)[1].split("\n\n##", 1)[0]
    assert done == "- parser written\n- first half\n- second half"
    assert "## Next step\n\ntests" in doc


def test_claude_codes_rules_in_a_project_name_the_files_not_a_task_document(tmp_path):
    from harness.claude_code import ClaudeCodeEngine
    from harness.config import HarnessConfig
    from harness.context import HarnessContext

    ctx = HarnessContext(target=LocalTarget(repo=str(tmp_path)), cfg=HarnessConfig(), slug="t", branch="harness/t",
                         state_key="k", project=True)
    rules = ClaudeCodeEngine(ctx).rules()
    assert "Task document: none. This repo is a project" in rules and "docs/tasks" not in rules
    assert "project_tool.py <command>" in rules and "`log --done" in rules  # the tool writes the log's heading
    ctx.project = False
    assert "Task document: docs/tasks/t.md" in ClaudeCodeEngine(ctx).rules()


def test_the_project_layout_doc_matches_the_parser():
    """docs/project-layout.md's examples are what the parser reads."""
    from pathlib import Path

    doc = (Path(__file__).parent.parent / "docs" / "project-layout.md").read_text()
    example = doc.split("### PLAN.md\n\n```markdown\n", 1)[1].split("```", 1)[0]
    plan = project.parse_plan(example)
    assert [m.id for m in plan.milestones] == ["M0", "M1"] and [i.id for i in plan.milestones[0].items] == ["M0.1", "M0.2"]
    assert project.check_plan(example) == ["M1 has no Accept items (`- M1.1 ...` under `Accept:`)."]  # its "..."
