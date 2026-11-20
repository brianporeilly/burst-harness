"""The setup round: a foundation document or a prompt turned into a project's layout, ending in a PR
(docs/project-layout.md, "Setting up a project").

`harness run --setup` (or `start --setup`; the orchestrator's "Set up this repo as a project"). It is never inferred,
so a one-off task in a repo with a stray brief isn't made a project. The round writes PLAN.md (the template in
plan_template.md), PROGRESS.md, DECISIONS.md, BACKLOG.md, the repo's AGENTS.md and .harness.toml, and puts a banner
on the foundation document; it builds nothing else. Finishing runs the plan check, and is refused until it passes.
The PR is the plan for review: merging it approves the plan, and the next round starts M0.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import git, project
from .context import HarnessContext

TEMPLATE = (Path(__file__).parent / "plan_template.md").read_text()
FOUNDATION = re.compile(r"(^|[/_.-])(foundation|prd|brief|vision|spec|concept|design-doc|proposal|idea)s?([_.-]|$)",
                        re.I)
BANNER = "> Superseded by PLAN.md on {date}; kept as the original brief. Sessions don't read it."

SETUP_SYSTEM_PROMPT = """You are a coding agent setting up one git repository as a project that agents will build over many sessions, each starting with nothing but the repo.

Working in: {repo}
Branch: {branch} — already created from {remote}/{base} by the harness. Do not create or switch branches.

This round writes the project's plan and the files that carry it, and nothing else: no product code. The next rounds build it, a milestone at a time, each ending in a PR a person reviews.

How to work:
- Read the foundation (the documents named in your first prompt, or the prompt itself) closely. Look at what the repo already has.
- Write the files as your first prompt describes. Run check_plan until it reports nothing wrong.
- Use handoff_to_user only for a question the foundation can't answer and the plan can't proceed without; otherwise choose, and record the choice in DECISIONS.md.
- The shell stops a command after 120 seconds unless you pass a longer `timeout` (in seconds).

End in exactly one terminal state:
- finish_with_pr — the plan is written and check_plan passes. The PR is the plan, for a person to review and approve by merging.
- pause_for_review — not done, or blocked on a decision. It commits everything so nothing is stranded.
- abandon — the repo should not be set up this way.

Never leave uncommitted changes behind."""

SETUP_PROMPT = """Set this repo up as a project: turn its foundation into the plan and the files that carry it between sessions.

The foundation: {foundation}
{task}
Write these, at the repo root:

1. PLAN.md, in this layout (the headings `## Roadmap`, `### M<n> — Title` and the labels Build, Accept and Checkpoint are fixed; the harness reads them). Take the substance from the foundation; don't invent scope it doesn't have, and where it is silent, choose conservatively and record the choice.
<template>
{template}
</template>
   - M0 is the bootstrap: the workspace, CI running the check command on every PR, the release pipeline, and the smallest thing that runs.
   - Each milestone is one reviewable PR. Each Accept item is checkable (a test, a command and its output, a number within a budget), written as an outcome, numbered M<n>.<k>. What only a person can judge goes in Checkpoint.
2. PROGRESS.md:
   # Progress

   Current: M0 — <M0's title>

   ## Milestones

   ## Blockers

   ## Measured

   ## Log
3. DECISIONS.md: `# Decisions`, then the foundation's decisions and yours, each `## D-001 — <title> (M0, {date})` with `- **What:**`, `- **Why:**`, `- **Alternatives:**`.
4. BACKLOG.md: `# Backlog`, then `## Ideas` listing what the foundation mentions but leaves out of scope, one `- ` line each.
5. AGENTS.md (add a section if it exists): how to build, test and run the project, the check command, the code style, and that PLAN.md is the contract, PROGRESS.md where the work stands, DECISIONS.md why. Short: what an agent new to the repo needs.
6. .harness.toml: `check_command = "..."` (the one command CI runs) and `setup_command = "..."` (what a fresh checkout needs), once M0 will make them work. Leave a setting out if the project can't have it yet.
{banner}
Then run check_plan, fix what it reports, and finish_with_pr. The PR's body: the milestones in a line each, and the decisions a person should look at before approving."""


def foundations(ctx: HarnessContext) -> list[str]:
    """The repo's documents that look like a foundation: a PRD, a brief, a vision, at the root or under docs/."""
    files = git.run(ctx.target, "ls-files", check=False).splitlines()
    files += git.run(ctx.target, "ls-files", "--others", "--exclude-standard", check=False).splitlines()
    record = {project.PLAN, project.PROGRESS, project.DECISIONS, project.BACKLOG, project.FEEDBACK}
    return sorted({name for name in files if name.lower().endswith((".md", ".txt", ".rst")) and name not in record
                   and name.count("/") <= (2 if name.startswith("docs/") else 0) and FOUNDATION.search(name)})


def ready(ctx: HarnessContext) -> list[str]:
    """What stops the repo counting as set up: the plan check's problems, or none."""
    plan = ctx.target.read_file(project.PLAN)
    if plan is None:
        return [f"There is no {project.PLAN}."]
    return project.check_plan(plan, ctx.target.read_file(project.PROGRESS) or "")


def prompt(ctx: HarnessContext, task: str, message: str | None, date: str) -> str:
    found = foundations(ctx)
    if found:
        foundation = ", ".join(f"`{name}`" for name in found) + ". Read it in full."
        banner = (f"7. At the top of {', '.join(f'`{n}`' for n in found)}, add this line, and change nothing else in it:\n"
                  f"   {BANNER.format(date=date)}\n")
    else:
        foundation = "the prompt below (no foundation document was found in the repo)."
        banner = ""
    existing = ctx.target.read_file(project.PLAN)
    if existing is not None:
        foundation += (" The repo already has a PLAN.md that doesn't pass the plan check; rework it into the layout, "
                       "keeping its substance and its milestones' order:\n" + "\n".join(f"- {p}" for p in ready(ctx)))
    asked = "\n".join(part for part in (f"The person's prompt: {task}" if task else "", message or "") if part)
    return SETUP_PROMPT.format(foundation=foundation, task=f"\n{asked}\n" if asked else "", template=TEMPLATE.strip(),
                               date=date, banner=banner)


def summary(ctx: HarnessContext) -> str:
    """The top of the setup round's PR: what the plan is, and how approving it works."""
    plan = project.Record(ctx.target).plan()
    items = sum(len([i for i in m.items if not i.struck]) for m in plan.milestones)
    lines = [f"## The plan: {len(plan.milestones)} milestones, {items} Accept items",
             "", *(f"- {m.id} — {m.title}" for m in plan.milestones), "",
             "This PR sets the repo up as a project (docs/project-layout.md in burst-harness). Merging it approves "
             f"the plan; the next round starts {plan.milestones[0].id if plan.milestones else 'M0'}. To change it "
             "first, comment and continue the task, or edit PLAN.md on this branch."]
    return "\n".join(lines)
