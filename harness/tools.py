"""The harness's own Strands tools: the record (a project's PLAN.md and PROGRESS.md, or the task document), and the
three terminal states."""

from __future__ import annotations

from collections.abc import Callable

from strands import tool
from strands.types.tools import ToolContext

import re

from . import project, state, terminal
from .context import HarnessContext

# In request_state: a tool stopped the loop (stop_loop). Strands keeps a deferred cancel to itself, so this is
# how the harness's own restarts (agent.py) know to leave it stopped.
STOPPED = "harness_stopped"


def stop_loop(tool_context: ToolContext) -> None:
    """End the run once this batch of tools has run: a terminal state, a job's report."""
    tool_context.invocation_state.setdefault("request_state", {})[STOPPED] = True
    tool_context.agent.cancel(after_current_tools=True)

__all__ = ["HarnessContext", "make_tools"]


def make_tools(ctx: HarnessContext) -> list:
    """Build the task tools bound to one run."""
    target, cfg, slug = ctx.target, ctx.cfg, ctx.slug

    def end(tool_context: ToolContext, step: Callable[[], str]) -> str:
        """A terminal state's step, which ends the run once the task has one.

        Stopping the loop here, rather than leaving it to the model to end its turn, is what ends it for a model
        that calls the tool again and again. A call after the task ended does nothing again.
        """
        if ctx.terminal:
            result = f"The task already ended ({ctx.terminal}); nothing was done again. Stop here."
        else:
            try:
                result = step()
            except ValueError as err:
                return f"Rejected: {err}"
        if ctx.terminal:
            stop_loop(tool_context)
        return result

    @tool
    def read_task_doc() -> str:
        """Read the task document: what this task is, what has been done, and what is open.

        Call this before anything else. It is the authoritative record and is
        cheaper and more reliable than re-deriving context from the codebase.
        """
        relative = state.doc_relpath(cfg, slug)
        text = target.read_file(relative)
        if text is None:
            return f"No task document at {relative} yet. Create one with update_task_doc."
        return text

    @tool
    def update_task_doc(
        goal: str | None = None,
        constraints: str | None = None,
        done: str | None = None,
        decisions: str | None = None,
        open_questions: str | None = None,
        next_step: str | None = None,
    ) -> str:
        """Replace one or more sections of the task document.

        Each argument replaces that whole section, so include everything that
        should remain. The document is size-capped; if a write is rejected,
        summarise rather than append.

        Args:
            goal: What this task is trying to achieve.
            constraints: Limits, requirements, and things to avoid.
            done: Outcomes achieved so far, as a terse list -- not a narrative.
            decisions: Choices made and, briefly, why.
            open_questions: What is still unresolved or needs a human decision.
            next_step: The single next actionable step.
        """
        try:
            path = terminal.update_doc(
                ctx,
                goal=goal,
                constraints=constraints,
                done=done,
                decisions=decisions,
                open_questions=open_questions,
                next_step=next_step,
            )
        except ValueError as err:
            return f"Rejected: {err}"
        return f"Updated {path}."

    @tool(context="tool_context")
    def finish_with_pr(title: str, body: str, tool_context: ToolContext, next_step: str | None = None) -> str:
        """Finish the task by committing, pushing, and opening a pull request.

        Use this only when the work is reviewable. If it is incomplete or you
        need a decision, use pause_for_review instead.

        Args:
            title: Pull request title.
            body: Pull request description: what changed and why.
            next_step: Optional follow-up to record in the task document.
        """
        return end(tool_context, lambda: terminal.finish(ctx, title=title, body=body, next_step=next_step))

    @tool(context="tool_context")
    def pause_for_review(summary: str, next_step: str, tool_context: ToolContext, questions: str | None = None) -> str:
        """Stop cleanly with the work committed but no pull request.

        Use this when the work is mid-flight, not yet usable, or blocked on a
        decision. It commits everything so nothing is left stranded, and records
        where things stand so the next session can pick up without re-reading
        the codebase.

        Args:
            summary: What was accomplished, for the Done section.
            next_step: The single next actionable step.
            questions: Anything you need the user to decide.
        """
        return end(tool_context, lambda: terminal.pause(ctx, summary=summary, next_step=next_step, questions=questions))

    @tool(context="tool_context")
    def abandon(reason: str, tool_context: ToolContext) -> str:
        """Stop and record that this task should not continue.

        Commits whatever exists so nothing is stranded, and writes the reason
        into the task document.

        Args:
            reason: Why the task is being abandoned.
        """
        return end(tool_context, lambda: terminal.abandon(ctx, reason=reason))

    terminals = [finish_with_pr, pause_for_review, abandon]
    if ctx.project:
        return [*project_tools(ctx), *terminals]

    @tool
    def record_progress(done: str | None = None, next_step: str | None = None) -> str:
        """Add to the task document as you go, without rewriting a section: a finished step to Done, or the next
        step. Cheaper than update_task_doc for one item; use it whenever something is done.

        Args:
            done: What was just achieved, as a short outcome (several on separate lines).
            next_step: The single next actionable step.
        """
        if not (done or next_step):
            return "Rejected: give done, next_step, or both."
        try:
            path = terminal.record(ctx, done=done or "", next_step=next_step or "")
        except ValueError as err:
            return f"Rejected: {err}"
        ctx.records["progress"] = ctx.records.get("progress", 0) + 1
        return f"Recorded in {path}."

    @tool
    def record_decision(title: str, what: str, why: str, alternatives: str = "") -> str:
        """Add a decision to the task document's Decisions: a choice made, and why.

        Args:
            title: A few words naming it.
            what: What was decided.
            why: Why.
            alternatives: What else was considered, briefly.
        """
        doc = state.read(target, cfg, slug)
        line = f"- {title.strip()}: {what.strip()} Why: {why.strip()}" + (
            f" Instead of: {alternatives.strip()}" if alternatives.strip() else "")
        try:
            path = terminal.update_doc(ctx, decisions=(doc.decisions.rstrip() + "\n" + line).strip())
        except ValueError as err:
            return f"Rejected: {err}"
        ctx.records["decisions"] = ctx.records.get("decisions", 0) + 1
        return f"Recorded in {path}."

    return [read_task_doc, update_task_doc, record_progress, record_decision, *terminals]


def project_tools(ctx: HarnessContext) -> list:
    """A project's: its plan read a part at a time, and its record added to through fields, never laid out by hand
    (project.py writes the files' shape)."""
    record = project.Record(ctx.target)

    def count(kind: str) -> None:
        ctx.records[kind] = ctx.records.get(kind, 0) + 1

    def attempt(step: Callable[[], str]) -> str:
        try:
            return step()
        except ValueError as err:  # ProjectError is one
            return f"Rejected: {err}"

    @tool
    def current_milestone() -> str:
        """The milestone being worked on: what to build, its Accept items (ticked or not), the reviewer's checkpoint,
        blockers, the person's FEEDBACK.md, and the last log entry. Where every session starts."""
        return record.view()

    @tool
    def read_plan(section: str) -> str:
        """Read one part of PLAN.md, for what an item needs: a `##` section by its heading (or a word of it), or a
        milestone by its id (M3).

        Args:
            section: The heading, a word of it, or a milestone id.
        """
        text = record.read(project.PLAN) or ""
        wanted = section.strip().lower()
        parts = re.split(r"(?=^#{2,3}\s)", text, flags=re.M)
        headings = [part.splitlines()[0] for part in parts if part.startswith("#")]
        level = "###" if re.fullmatch(r"m\d+[a-z]?", wanted) else "##"
        for at, part in enumerate(parts):
            heading = part.splitlines()[0] if part.startswith("#") else ""
            if not (heading.startswith(level + " ") and wanted in heading.lower()):
                continue
            if level == "###":
                return part.strip()
            rest = parts[at + 1:]  # a `##` section runs on through its `###` parts
            stop = next((i for i, later in enumerate(rest) if later.startswith("## ")), len(rest))
            return (part + "".join(rest[:stop])).strip()
        return "No such part of PLAN.md. Its headings:\n" + "\n".join(headings)

    @tool
    def record_progress(done: str | None = None, next_step: str | None = None, blocker: str | None = None,
                        resolved: str | None = None, measured_what: str | None = None,
                        measured_value: str | None = None, measured_target: str | None = None) -> str:
        """Add to PROGRESS.md as you go. Give one field or several; the harness writes them in the file's format.

        Args:
            done: What was just achieved, as a short outcome (several on separate lines). Goes in this session's
                log entry.
            next_step: The single next step, for this session's log entry.
            blocker: Something stopping the work that you can't fix (a missing tool, an unclear requirement).
            resolved: A few words of a blocker that no longer blocks, to take it off the list.
            measured_what: A measurement's name (a build size, a timing), with measured_value.
            measured_value: Its value, with units.
            measured_target: Its target, if the plan sets one.
        """
        if not any((done, next_step, blocker, resolved, measured_what)):
            return "Rejected: give at least one of done, next_step, blocker, resolved, measured_what."
        if measured_what and not measured_value:
            return "Rejected: measured_what needs measured_value."

        def write() -> str:
            said = []
            if blocker:
                record.add_blocker(blocker)
                said.append("blocker added")
            if resolved:
                gone = record.resolve_blocker(resolved)
                terminal.record(ctx, note=f"Resolved: {gone}")
                said.append("blocker resolved")
            if measured_what:
                record.measure(measured_what, measured_value or "", measured_target or "")
                said.append("measurement added")
            if done or next_step:
                terminal.record(ctx, done=done or "", next_step=next_step or "")
                said.append("log entry updated")
            count("progress")
            return f"Recorded in {project.PROGRESS}: {', '.join(said)}."

        return attempt(write)

    @tool
    def record_decision(title: str, what: str, why: str, alternatives: str = "") -> str:
        """Add a decision to DECISIONS.md, numbered for you (D-NNN): a choice made, a deviation from the plan, and why.

        Args:
            title: A few words naming it.
            what: What was decided.
            why: Why.
            alternatives: What else was considered, briefly.
        """
        current = record.current()

        def write() -> str:
            ident = record.decide(title, what, why, alternatives, milestone=current.id if current else "")
            count("decisions")
            return f"Recorded {ident} in {project.DECISIONS}."

        return attempt(write)

    @tool
    def add_backlog(idea: str) -> str:
        """Put an idea or a deferred bug in BACKLOG.md instead of building it: anything outside the plan.

        Args:
            idea: The idea, in a sentence.
        """
        return attempt(lambda: (record.add_backlog(idea), count("backlog"), f"Added to {project.BACKLOG}.")[2])

    @tool
    def tick_criterion(item: str, evidence: str) -> str:
        """Tick one of the current milestone's Accept items once it is met, with what shows it: the test's name or the
        command, and its result. Then commit. Ticks are what finishing checks, and what the PR lists as met.

        Args:
            item: Its id, such as M3.2.
            evidence: What shows it is met (a test that passes, a command and what it printed, a measurement).
        """
        def write() -> str:
            ticked = record.tick(item, evidence)
            count("ticks")
            left = record.unticked()
            return (f"Ticked {ticked.id}. " + (f"Still to meet: {', '.join(i.id for i in left)}." if left else
                                               "Every Accept item of this milestone is met: finish_with_pr."))

        return attempt(write)

    @tool
    def amend_plan(target: str, text: str, title: str, why: str, alternatives: str = "") -> str:
        """Change PLAN.md when the work shows it is wrong: an item added, reworded or struck; a milestone too big,
        split; one added, dropped or moved; the goal. Every change is recorded as a decision and put first in the PR
        for review; a change to the roadmap makes the next round wait for a person.

        Args:
            target: A milestone's id (M3) to replace its section with `text`, or drop it with empty text; `after M3`
                to add milestones after it; or a `##` section's heading (or a word of it) to replace its body.
            text: The new text, in the plan's layout: `### M3 — Title`, `Build: ...`, `Accept:` with `- M3.1 ...`
                items, `Checkpoint: ...`. Keep existing items' ids; strike an item through (`- ~~M3.2 ...~~`) rather
                than deleting it. New items take the next number; a split milestone's parts (M3a, M3b) keep its items.
            title: A few words naming the change.
            why: What was found that the plan didn't foresee.
            alternatives: What else was considered, briefly.
        """
        def write() -> str:
            ident, kind = record.amend(target, text, title, why, alternatives)
            count("plan_changes")
            if kind == "roadmap" or not ctx.plan_change:
                ctx.plan_change = kind
            current = record.current()
            return (f"Changed PLAN.md and recorded {ident}." + (f" The current milestone is {current.id}."
                                                                if current else "")
                    + (" It changes the roadmap: the PR puts it first, and the next round waits for a person."
                       if kind == "roadmap" else ""))

        return attempt(write)

    return [current_milestone, read_plan, tick_criterion, record_progress, record_decision, amend_plan, add_backlog]
