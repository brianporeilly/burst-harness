"""The setup round (setup_round.py): a foundation document or a prompt turned into a project's layout, refused at
finish until the plan check passes, and ending in a PR whose merge approves the plan."""

import json
from pathlib import Path

import pytest

from harness import cli, project, setup_round
from harness.config import HarnessConfig
from harness.context import HarnessContext
from harness.exec_target import LocalTarget

from . import fake_llama
from .conftest import git
from .test_agents import llama  # noqa: F401 -- the fixture
from .test_project import PLAN
from .test_tasks import _runner, harness  # noqa: F401 -- the fixture

FOUNDATION = "# Orbit\n\nA one-thumb orbital toy for phones. Gravity is real. No accounts, no network.\n"
PROGRESS = "# Progress\n\nCurrent: M0 — Bootstrap\n\n## Milestones\n\n## Blockers\n\n## Measured\n\n## Log\n"


def _write(name, text):
    return {"tool": "shell", "args": {"command": f"mkdir -p \"$(dirname {name})\" && cat > {name} <<'EOF'\n{text}EOF"}}


def test_the_template_is_a_plan_that_passes_and_the_doc_carries_it():
    assert project.check_plan(setup_round.TEMPLATE) == []
    assert [m.id for m in project.parse_plan(setup_round.TEMPLATE).milestones] == ["M0", "M1"]
    doc = (Path(__file__).parent.parent / "docs" / "plan-template.md").read_text()
    assert setup_round.TEMPLATE.strip() in doc  # what you paste into a chat is what the setup round is given


def test_foundation_documents_are_found_by_their_names(tmp_path):
    for name in ("docs/000-foundation.md", "PRD.md", "docs/notes/brief.md", "docs/a/b/vision.md", "src/spec.md",
                 "README.md", "docs/guide.md", "docs/spec.rs"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("x\n")
    git(tmp_path, "init", "-q")
    ctx = HarnessContext(target=LocalTarget(repo=str(tmp_path)), cfg=HarnessConfig(), slug="t", branch="b", state_key="k")
    assert setup_round.foundations(ctx) == ["PRD.md", "docs/000-foundation.md", "docs/notes/brief.md"]


def _origin_with(tmp_path, origin, files):
    seed = tmp_path / "seed-setup"
    git(tmp_path, "clone", "-q", str(origin), str(seed))
    for name, text in files.items():
        (seed / name).parent.mkdir(parents=True, exist_ok=True)
        (seed / name).write_text(text)
    git(seed, "add", "-A")
    git(seed, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "seed")
    git(seed, "push", "-q", "origin", "HEAD:main")


def test_a_setup_round_writes_the_plan_and_is_held_to_the_check(tmp_path, origin, llama, capsys):  # noqa: F811
    _origin_with(tmp_path, origin, {"docs/000-foundation.md": FOUNDATION})
    banner = setup_round.BANNER.format(date=project.today())
    url, requests = llama([
        _write("PLAN.md", "# Orbit — Plan\n\n## Roadmap\n\n### M0 — Bootstrap\nBuild: CI.\n"),  # no Accept items
        {"tool": "finish_with_pr", "args": {"title": "Plan", "body": "The plan."}},
        _write("PLAN.md", PLAN),
        _write("PROGRESS.md", PROGRESS),
        _write("docs/000-foundation.md", f"{banner}\n\n{FOUNDATION}"),
        {"tool": "check_plan", "args": {}},
        {"tool": "finish_with_pr", "args": {"title": "Plan", "body": "The plan."}},
        {"text": "Done."},
    ])
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "run", "orbit", "--setup",
              "--task", "Make it fun."])
    out = capsys.readouterr().out
    result = json.loads(out.split("HARNESS_RESULT ", 1)[1].splitlines()[0])

    system, prompt = (fake_llama._text(m["content"]) for m in requests[0]["messages"][:2])
    assert "setting up one git repository as a project" in system and "No product code" not in prompt
    assert "The foundation: `docs/000-foundation.md`. Read it in full." in prompt
    assert "The person's prompt: Make it fun." in prompt and "<template>\n# <Project> — Plan" in prompt
    assert banner in prompt
    tools = {t["function"]["name"] for t in requests[0]["tools"]}
    assert "check_plan" in tools and not {"read_task_doc", "record_progress", "tick_criterion"} & tools

    refused = json.dumps(requests[2]["messages"][-1])
    assert "No PR yet: the plan doesn't pass the check" in refused and "M0 has no Accept items" in refused
    assert "PLAN.md passes: 2 milestones, 4 Accept items." in json.dumps(requests[6]["messages"][-1])
    assert result["state"] == "pushed_no_pr"
    assert result["project"] == {"milestone": "M0", "complete": False, "plan_change": None, "setup": True}
    files = git(origin, "ls-tree", "-r", "--name-only", "harness/orbit")
    assert "PLAN.md" in files and "PROGRESS.md" in files and "docs/tasks" not in files
    assert git(origin, "show", "harness/orbit:docs/000-foundation.md").startswith("> Superseded by PLAN.md on ")


def test_a_plan_that_doesnt_pass_is_reworked_and_one_that_does_is_left_alone(tmp_path, origin, llama, capsys):  # noqa: F811
    prose = PLAN.replace("- M0.1 `just check` passes in CI.\n- M0.2 The title screen renders in a headless test.\n",
                         "CI passes and the title renders.\n")
    _origin_with(tmp_path, origin, {"PLAN.md": prose})
    url, requests = llama([{"tool": "pause_for_review", "args": {"summary": "s", "next_step": "n"}}, {"text": "."}])
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "start", "orbit", "--setup",
              "--task", "Set it up."])
    prompt = fake_llama._text(requests[0]["messages"][1]["content"])
    assert "already has a PLAN.md that doesn't pass" in prompt and "- M0 has no Accept items" in prompt
    assert "no foundation document was found" in prompt
    capsys.readouterr()

    good = tmp_path / "good"
    good.mkdir()
    _origin_with(good, origin, {"PLAN.md": PLAN, "PROGRESS.md": PROGRESS.replace("M0 — Bootstrap", "M1 — Simulation core")})
    work = good / "work"
    git(good, "clone", "-q", str(origin), str(work))
    with pytest.raises(SystemExit, match="already passes the plan check"):
        cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "start", "orbit-2", "--setup",
                  "--task", "Again."])


def test_the_setup_prs_summary_names_the_milestone_progress_is_at(tmp_path):
    (tmp_path / "PLAN.md").write_text(PLAN)
    ctx = HarnessContext(target=LocalTarget(repo=str(tmp_path)), cfg=HarnessConfig(), slug="t", branch="b", state_key="k")
    assert "the next round starts M0." in setup_round.summary(ctx)  # a new project: no PROGRESS.md yet
    (tmp_path / "PROGRESS.md").write_text(PROGRESS.replace("M0 — Bootstrap", "M1 — Simulation core"))
    assert "the next round starts M1." in setup_round.summary(ctx)  # a project carried over, part built


def test_after_the_setup_round_a_round_needs_no_prompt_of_its_own(tmp_path, origin, llama, capsys):  # noqa: F811
    _origin_with(tmp_path, origin, {"PLAN.md": PLAN, "PROGRESS.md": PROGRESS})
    url, requests = llama([{"tool": "pause_for_review", "args": {"summary": "s", "next_step": "n"}}, {"text": "."}])
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(work), "run", "orbit"])
    prompt = fake_llama._text(requests[0]["messages"][1]["content"])
    assert prompt.endswith("Carry on with the current milestone.") and "M0 — Bootstrap" in prompt
    capsys.readouterr()
    plain = tmp_path / "plain"
    git(tmp_path, "clone", "-q", str(origin), str(plain))
    (plain / "PLAN.md").unlink()
    git(plain, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "no plan")
    with pytest.raises(SystemExit, match="--task is needed"):  # not a project: nothing says what to do
        cli.main(["--provider", "llamacpp", "--base-url", url, "--repo", str(plain), "run", "other"])


def test_claude_code_is_told_its_setup_round(tmp_path):
    from harness.claude_code import ClaudeCodeEngine

    ctx = HarnessContext(target=LocalTarget(repo=str(tmp_path)), cfg=HarnessConfig(), slug="t", branch="b",
                         state_key="k", setup_round=True)
    rules = ClaudeCodeEngine(ctx).rules()
    assert "This round sets the repo up as a project" in rules and "docs/tasks" not in rules


def test_the_orchestrator_asks_for_the_setup_round_until_its_pr_is_merged(tmp_path, harness):  # noqa: F811
    runner = _runner(tmp_path)
    runner.create({"repo": "me/proj", "prompt": "a roguelike", "slug": "p", "setup": True})
    assert runner.task("p")["setup"] is True
    assert "--setup" in runner._argv(runner._read("p"))
    record = runner._read("p")
    record["rounds"] = [{"branch": "harness/p", "pr": "https://github.com/me/proj/pull/1", "pr_state": "merged"}]
    argv = runner._argv(record)
    assert "--setup" not in argv  # the plan is approved: later rounds build it
    assert "--task" not in argv and "a roguelike" not in argv  # the setup prompt is done with: the plan is the task
    runner.create({"prompt": "what is new?", "slug": "q", "setup": True})
    assert runner.task("q")["setup"] is False  # a question has no repo to set up
