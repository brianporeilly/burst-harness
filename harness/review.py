"""A review before the PR: review_with names a profile ([agents.NAME]) that reads the change before a PR opens.

When the lead finishes (terminal.finish), after check_command passes, the profile runs read-only on the
task document and the branch's diff, as a delegate does (agent.delegate_function: the loop watch and usage
apply). It has limits of its own, review_max_minutes and review_max_calls (and the sandbox's deadline): it is
told when it is nearly at one, to report what it has, and stopped at it. A review cut off with findings still
goes back to the lead; one that gives none holds nothing up. Its findings go back to the lead once, as finish_with_pr's refusal, for it to address
or answer; finishing again opens the PR with the review in its body. A review that finds nothing worth
changing (it says NO FINDINGS) lets the first finish through. Either engine may be the lead: the reviewer is
a Strands agent in the same sandbox.
"""

from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from . import budget, git, metrics, project, state, transcript

# How much of the diff goes into the reviewer's brief; it can run git diff itself for the rest.
DIFF_CHARS = 30000
NOTHING = "NO FINDINGS"

BRIEF = """Review this change before it becomes a pull request. You are read-only: change nothing.

{checked}Don't build the project, run long commands or write scratch programs: read the diff and the code \
around it, and look things up in the sources. You have about {minutes} minutes and {calls} model calls; you will \
be told when you are nearly out, and should then report what you have.

Look for bugs, requirements in the task's record (below) the change misses, Accept items ticked as met that \
the change doesn't back up, behaviour it adds or changes that no documentation describes, claims the work doesn't back up, and \
risky or unrelated changes. Report the findings that matter, most serious first, each with where (file:line) \
and why; at most ten. Skip style nits. If there is nothing worth changing, reply with exactly {nothing}.

<task-document>
{doc}
</task-document>

Files changed (git status --short):
{status}

<diff against {base}>
{diff}
</diff>"""

GIVEN = """No PR yet: {name} reviewed the change first. Your work so far is committed to the branch. Address the findings you agree with; \
for any you don't, say why in the PR body. Then finish again: the PR opens then, with this review in its body.

{findings}"""

IN_BODY = """

---
<details><summary>Reviewed by {name} before this PR</summary>

{findings}
</details>"""


class ReviewGiven(ValueError):
    """The review's findings, refusing the first finish for the lead to address."""


def _brief(ctx: Any) -> str:
    cfg, target = ctx.cfg, ctx.target
    base = f"{cfg.remote}/{cfg.base_branch}"
    since = git.run(target, "merge-base", base, "HEAD", check=False) or base
    diff = git.run(target, "diff", since, check=False)  # what is committed and what isn't, tracked files
    for path in git.run(target, "ls-files", "--others", "--exclude-standard", check=False).splitlines():
        if len(diff) > DIFF_CHARS:
            break
        diff += "\n" + git.run(target, "diff", "--no-index", "--", "/dev/null", path, check=False)  # a new file
    diff = diff.strip()
    if len(diff) > DIFF_CHARS:
        diff = diff[:DIFF_CHARS] + f"\n[harness] The diff was cut here: run `git diff {since}` for the rest."
    if getattr(ctx, "project", False):
        doc = project.Record(ctx.target).view(log_entries=0)  # the milestone, its criteria and their ticks
    else:
        doc = ctx.target.read_file(state.doc_relpath(cfg, ctx.slug)) or ""
    checked = (f"The repo's check (`{cfg.check_command.strip()}`) has already passed on this change: don't run "
               "it, or the tests, again. " if cfg.check_command.strip() else "")
    return BRIEF.format(nothing=NOTHING, checked=checked, minutes=cfg.review_max_minutes, calls=cfg.review_max_calls,
                        doc=doc.strip() or "(none)",
                        status=git.run(target, "status", "--short", check=False) or "(none)",
                        base=base, diff=diff or "(empty)")


def before_pr(ctx: Any, body: str) -> str:
    """The PR's body, with the review in it. Raises ReviewGiven the first time the review finds something."""
    name = ctx.cfg.review_with
    if not name:
        return body
    if ctx.review is None:
        findings = run(ctx, name)
        # A reviewer that failed (its model unreachable), or was stopped with nothing to say, holds nothing up: the
        # PR says so. One stopped at its limit with findings to give gives them.
        report, _, why = findings.partition(f"\n[harness] {name} was stopped")
        report = report.strip()
        broke = findings.startswith((f"{name} failed:", f"{name} stopped:")) or bool(why) and (
            not report or report == "(it gave no report)")
        if why and not broke:
            findings = f"(The review was cut off at its limit; this is what it had found.)\n\n{report}"
        clean = findings.strip().upper().startswith(NOTHING)
        ctx.review = {"by": name, "findings": findings, "outcome": "failed" if broke else
                      "no findings" if clean else "findings"}
        if ctx.review["outcome"] == "findings":
            raise ReviewGiven(GIVEN.format(name=name, findings=findings))
    findings = "No findings." if ctx.review["outcome"] == "no findings" else ctx.review["findings"]
    return body + IN_BODY.format(name=name, findings=findings)


def run(ctx: Any, name: str, *, brief: str = "") -> str:
    """The review: profile `name`, read-only whatever it says, as a delegate of the burst. `brief`: _brief's,
    unless given (a review of an open PR's)."""
    from .agent import delegate_function

    cfg = replace(ctx.cfg, delegate_to=(name,),
                  agents={**ctx.cfg.agents, name: {**ctx.cfg.agents[name], "read_only": True}})
    state_dir = cfg.resolve_state_dir(ctx.state_key)
    state_dir.mkdir(parents=True, exist_ok=True)
    # Its own limits, as a budget of its own (budget.py): its minutes and model calls, and the sandbox's deadline.
    caps = budget.Budget(SimpleNamespace(
        cfg=replace(cfg, max_model_calls=cfg.review_max_calls, max_burst_minutes=cfg.review_max_minutes,
                    max_cost_usd=0.0),
        started=time.time(), delegated={}, target=ctx.target, budget=None))
    review = delegate_function(ctx.target, cfg, ctx.delegated, str(state_dir), caps, transcript.of(ctx),
                               watch=lambda helper: _nearly_out(helper, caps))
    print(f"\nReviewing the change with {name}" + ("" if brief else " before the PR")
          + f" (at most {cfg.review_max_minutes} minutes, {cfg.review_max_calls} model calls).", flush=True)
    return review(name, brief or _brief(ctx))


# A review of an open PR, asked for from the orchestrator (`harness review`): a reviewer of your choosing, with a fresh
# context, read-only on the PR's branch, its findings a comment on the PR. A comment from the token's user, so the
# task's next burst reads it as review (cli._review_feedback), to address or answer.
PR_HEAD = ("Review pull request {pr}: the change on this branch, against {base}. You are read-only: change "
           "nothing, commit nothing.")
PR_SYSTEM = ("You are reviewing a pull request another agent wrote, with a fresh view of it. You are read-only: "
             "don't edit files, commit or push. Your last reply is posted on the PR as the review, so make it the "
             "findings alone, in Markdown.")
PR_COMMENT = """**Review by {by}**, asked for from the orchestrator: a fresh context, read-only on this PR's branch.

{findings}"""


def pr_brief(ctx: Any, pr: str) -> str:
    """The brief for a review of an open PR: _brief's, about the PR rather than a change before one."""
    first, _, rest = _brief(ctx).partition("\n")
    return PR_HEAD.format(pr=pr, base=f"{ctx.cfg.remote}/{ctx.cfg.base_branch}") + "\n" + rest


def of_pr(ctx: Any, pr: str, by: str = "") -> tuple[str, str]:
    """Review an open PR: profile `by` (read-only, as a delegate), or with none, the run's Claude Code. Its
    findings, and who gave them."""
    brief = pr_brief(ctx, pr)
    if by:
        return run(ctx, by, brief=brief), by
    from .claude_code import ClaudeCodeEngine

    if ctx.cfg.engine != "claude-code":
        raise ValueError("A review needs a profile (--by), or engine=claude-code.")
    engine = ClaudeCodeEngine(ctx)
    name = f"Claude Code ({ctx.cfg.model_id})" if ctx.cfg.model_id else "Claude Code"
    print(f"\nReviewing {pr} with {name} (at most {ctx.cfg.max_burst_minutes} minutes, "
          f"{ctx.cfg.max_model_calls} model calls).", flush=True)
    engine.prepare()
    try:
        findings = engine.ask(brief, PR_SYSTEM)
        engine.report()
    finally:
        engine.cleanup()
    return findings, name


def pr_comment(by: str, findings: str) -> str:
    """The comment a review leaves on its PR."""
    text = "No findings: nothing worth changing." if findings.strip().upper().startswith(NOTHING) else findings
    return PR_COMMENT.format(by=by, findings=text.strip() or "(It gave no report.)")


# Told this close to a limit (model calls, or minutes) to stop and report.
NEARLY_CALLS, NEARLY_SECONDS = 3, 180
NEARLY = ("\n[harness] The review is nearly out of time: stop investigating and write your findings now, in the "
          "form asked for (or reply with exactly " + NOTHING + ").")


def _nearly_out(helper: Any, caps: budget.Budget) -> None:
    """Tell the reviewer, once, in a tool result, that it is near its limits."""
    from strands.hooks import AfterToolCallEvent

    told = [False]

    def after_call(event: AfterToolCallEvent) -> None:
        cfg = caps.cfg
        calls = metrics.of_agent(event.agent, "", "")["model_calls"] or 0
        stop = caps.stop_at()
        near = (cfg.max_model_calls and calls >= cfg.max_model_calls - NEARLY_CALLS) or (
            stop and time.time() >= stop - NEARLY_SECONDS)
        if near and not told[0] and isinstance(event.result, dict):
            told[0] = True
            event.result = {**event.result, "content": [*(event.result.get("content") or []), {"text": NEARLY}]}

    helper.hooks.add_callback(AfterToolCallEvent, after_call)
