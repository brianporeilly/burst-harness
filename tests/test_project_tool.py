"""The project tool (project_tool.py): a project's record kept from a shell, for Claude Code, in the same formats as
the Strands tools. It runs alone, beside a copy of project.py, as the engine puts it in a sandbox."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from harness import claude_code, project, project_tool
from harness.config import HarnessConfig
from harness.context import HarnessContext
from harness.exec_target import LocalTarget

from .conftest import git
from .test_project import PLAN, PROGRESS


def _setup(tmp_path, settings=None):
    """The tool as the engine leaves it (a directory of its own), and a project repo to run it in."""
    tool = tmp_path / "tool"
    tool.mkdir()
    for module in (project, project_tool):
        shutil.copy(module.__file__, tool / Path(module.__file__).name)
    if settings is not None:
        (tool / "project_tool.json").write_text(json.dumps(settings))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "PLAN.md").write_text(PLAN)
    (repo / "PROGRESS.md").write_text(PROGRESS)
    git(repo, "init", "-q", "-b", "harness/orbit-2")
    return tool, repo


def _run(tool, repo, *args, stdin=None):
    # -E -s: no PYTHONPATH or user site, so `import project` finds the copy beside the script, as in a sandbox.
    return subprocess.run([sys.executable, "-E", "-s", str(tool / "project_tool.py"), *args], cwd=repo / "src" if (
        repo / "src").is_dir() else repo, input=stdin, capture_output=True, text=True, check=False)


def test_the_tool_runs_alone_and_writes_the_records_formats(tmp_path):
    tool, repo = _setup(tmp_path, {"heading": "2026-11-21 06:00Z — M1 · harness/orbit-2"})
    (repo / "src").mkdir()  # from anywhere in the repo
    assert "PLAN.md passes: 2 milestones, 4 Accept items." in _run(tool, repo, "check").stdout
    assert "M1 — Simulation core" in _run(tool, repo, "status").stdout
    assert _run(tool, repo, "plan", "M0").stdout.startswith("### M0 — Bootstrap")

    said = _run(tool, repo, "tick", "M1.3", "`just test-wasm`: hashes match")
    assert said.returncode == 0 and "Ticked M1.3. Every Accept item of this milestone is met" in said.stdout
    refused = _run(tool, repo, "tick", "M0.1", "CI")
    assert refused.returncode == 1 and "Rejected: M0.1 is not an Accept item of the current milestone" in refused.stderr

    assert _run(tool, repo, "log", "--done", "-", "--next", "the PR", stdin="the WASM test\nthe docs\n").returncode == 0
    _run(tool, repo, "blocker", "No phone to test on.")
    _run(tool, repo, "measure", "WASM size", "15.3 MiB", "--target", "15 MB")
    _run(tool, repo, "resolve", "phone")
    decided = _run(tool, repo, "decide", "Keep rails", "--what", "Rails for planets.", "--why", "Cheaper.")
    assert decided.stdout.strip() == "Recorded D-001 in DECISIONS.md."
    _run(tool, repo, "backlog", "A moon editor.")

    progress = (repo / "PROGRESS.md").read_text()
    assert "  - [x] M1.3 Determinism across native and WASM. — `just test-wasm`: hashes match (" in progress
    assert ("### 2026-11-21 06:00Z — M1 · harness/orbit-2\n- the WASM test\n- the docs\nNext: the PR\nResolved: No "
            "phone to test on. (since ") in progress
    assert "| WASM size | 15.3 MiB | 15 MB |" in progress and "No phone" not in progress.split("## Blockers")[1].split("##")[0]
    assert "## D-001 — Keep rails (M1, " in (repo / "DECISIONS.md").read_text()
    assert "- A moon editor." in (repo / "BACKLOG.md").read_text()
    kinds = [json.loads(line)["kind"] for line in (tool / "records.jsonl").read_text().splitlines()]
    assert kinds == ["ticks", "progress", "progress", "progress", "progress", "decisions", "backlog"]


def test_a_plan_change_and_a_failed_check_and_a_heading_of_its_own(tmp_path):
    tool, repo = _setup(tmp_path, {"started": 1_795_000_000, "branch": "harness/orbit-2"})
    new = "### M2 — Flight toy\nBuild: a burn.\nAccept:\n- M2.1 A burn changes the orbit.\n"
    changed = _run(tool, repo, "amend", "after M1", "--text", "-", "--title", "Flight", "--why", "Next.", stdin=new)
    assert changed.returncode == 0 and "It changes the roadmap" in changed.stdout
    assert json.loads((tool / "records.jsonl").read_text()) == {"kind": "plan_changes", "plan_change": "roadmap"}
    _run(tool, repo, "log", "--done", "planned")
    assert "### 2026-11-18 11:06Z — M1 · harness/orbit-2\n- planned" in (repo / "PROGRESS.md").read_text()

    (repo / "PLAN.md").write_text("# Plan\n\n## Roadmap\n\n### M0 — Bootstrap\nBuild: CI.\n")
    failed = _run(tool, repo, "check")
    assert failed.returncode == 1 and "Not yet:" in failed.stderr and "M0 has no Accept items" in failed.stderr


def test_claude_code_is_given_the_tool_and_its_records_are_counted(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "PLAN.md").write_text(PLAN)
    (repo / "PROGRESS.md").write_text(PROGRESS)
    git(repo, "init", "-q")
    ctx = HarnessContext(target=LocalTarget(repo=str(repo)), cfg=HarnessConfig(state_dir=str(tmp_path / "state")),
                         slug="t", branch="harness/t", state_key="k")
    ctx.project = True
    engine = claude_code.ClaudeCodeEngine(ctx)
    engine._install_project_tool()
    tool = Path(engine.project_tool)
    assert tool.is_file() and (tool.parent / "project.py").is_file()
    assert json.loads((tool.parent / "project_tool.json").read_text())["heading"].endswith("— M1 · harness/t")
    rules = engine.rules()
    assert f"`python3 {tool} <command>`" in rules and "tick M3.1" in rules and "## D-NNN" not in rules

    ran = subprocess.run([sys.executable, "-E", "-s", str(tool), "tick", "M1.3", "a test"], cwd=repo, capture_output=True,
                         text=True, check=False)
    assert ran.returncode == 0, ran.stderr
    (tool.parent / "records.jsonl").write_text((tool.parent / "records.jsonl").read_text()
                                               + json.dumps({"kind": "plan_changes", "plan_change": "item"}) + "\n")
    engine.cleanup()
    assert ctx.records == {"ticks": 1, "plan_changes": 1} and ctx.plan_change == "item"
    assert not tool.parent.exists()  # gone with the burst

    ctx.project, ctx.setup_round = False, True
    assert f"`python3 {claude_code.ClaudeCodeEngine(ctx).project_tool} check`" in claude_code.ClaudeCodeEngine(ctx).rules()
