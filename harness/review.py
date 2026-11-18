"""A review before the PR: review_with names a profile ([agents.NAME]) that reads the change before a PR opens.

When the lead finishes (terminal.finish), after check_command passes, the profile runs read-only on the
task document and the branch's diff, as a delegate does (agent.delegate_function: the burst's budget, loop
watch and usage apply). Its findings go back to the lead once, as finish_with_pr's refusal, for it to address
or answer; finishing again opens the PR with the review in its body. A review that finds nothing worth
changing (it says NO FINDINGS) lets the first finish through. Either engine may be the lead: the reviewer is
a Strands agent in the same sandbox.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from . import budget, git, state

# How much of the diff goes into the reviewer's brief; it can run git diff itself for the rest.
DIFF_CHARS = 30000
NOTHING = "NO FINDINGS"

BRIEF = """Review this change before it becomes a pull request. You are read-only: change nothing. You may read \
the repo and run its tests.

Look for bugs, requirements in the task document the change misses, claims the work doesn't back up, and \
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

GIVEN = """No PR yet: {name} reviewed the change first. Nothing was pushed. Address the findings you agree with; \
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
    doc = ctx.target.read_file(state.doc_relpath(cfg, ctx.slug)) or ""
    return BRIEF.format(nothing=NOTHING, doc=doc.strip() or "(none)",
                        status=git.run(target, "status", "--short", check=False) or "(none)",
                        base=base, diff=diff or "(empty)")


def before_pr(ctx: Any, body: str) -> str:
    """The PR's body, with the review in it. Raises ReviewGiven the first time the review finds something."""
    name = ctx.cfg.review_with
    if not name:
        return body
    if ctx.review is None:
        findings = run(ctx, name)
        # A reviewer that failed or was stopped (its model unreachable, the budget) holds nothing up: the PR says so.
        broke = findings.startswith((f"{name} failed:", f"{name} stopped:")) or "[harness] " + name + " was stopped" \
            in findings
        clean = findings.strip().upper().startswith(NOTHING)
        ctx.review = {"by": name, "findings": findings, "outcome": "failed" if broke else
                      "no findings" if clean else "findings"}
        if ctx.review["outcome"] == "findings":
            raise ReviewGiven(GIVEN.format(name=name, findings=findings))
    findings = "No findings." if ctx.review["outcome"] == "no findings" else ctx.review["findings"]
    return body + IN_BODY.format(name=name, findings=findings)


def run(ctx: Any, name: str) -> str:
    """The review: profile `name`, read-only whatever it says, as a delegate of the burst."""
    from .agent import delegate_function

    cfg = replace(ctx.cfg, delegate_to=(name,),
                  agents={**ctx.cfg.agents, name: {**ctx.cfg.agents[name], "read_only": True}})
    state_dir = cfg.resolve_state_dir(ctx.state_key)
    state_dir.mkdir(parents=True, exist_ok=True)
    review = delegate_function(ctx.target, cfg, ctx.delegated, str(state_dir), budget.of(ctx))
    print(f"\nReviewing the change with {name} before the PR.", flush=True)
    return review(name, _brief(ctx))
