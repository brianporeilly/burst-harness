"""`harness ask SLUG --task QUESTION`: a task with no repo, which ends in an answer rather than a PR.

A question, a summary, some research: work that needs no checkout. It runs as
a one-off job (job.py) does: a fresh sandbox behind a gateway in report mode,
so the network only reads, the model routed afresh unless the run names one
(harness/agents.py), and the run ends with finish_report, whose report is the
answer. The answer is printed, and kept with the run, under the state
directory: asks/SLUG/runs/RUN-ID/ (run.json, with what that run asked, and
report.md).

`--message` follows up: the run is given the earlier questions and their
answers, oldest first, and the follow-up to answer now. A sandbox does not
outlive its run, so nothing but the conversation is carried over. Run again
with neither, after a run that gave no answer, and the same question is asked
again.

    harness --target podman ask brick-uses --task "Ten uses for a brick, ranked."
    harness --target podman ask brick-uses --message "Which of those is safest?"
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import job
from .config import HarnessConfig

ROOT = "asks"
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
# How much of the earlier conversation a follow-up is given: the newest turns that fit.
EARLIER_CHARS = 60_000

FOLLOW_UP = """This follows on from questions you answered earlier. They and your answers are below, oldest first.

<earlier>
{earlier}
</earlier>

Answer this now, in the light of what came before:

{message}"""


class AskError(ValueError):
    """Nothing to ask, or a run that cannot be read."""


def turns(cfg: HarnessConfig, slug: str) -> list[dict[str, Any]]:
    """The question's runs so far, oldest first: each with `asked`, and `answer` (None when it gave none)."""
    found = []
    records = job.list_runs(cfg, slug, limit=1000, root=ROOT)
    for record in sorted(records, key=lambda r: (int(r.get("turn") or 0), str(r.get("id")))):
        if record.get("state") == "running":
            continue
        report = job.runs_dir(cfg, slug, ROOT) / str(record.get("id")) / "report.md"
        answer = report.read_text() if record.get("state") == "reported" and report.is_file() else None
        found.append({"asked": str(record.get("asked") or ""), "answer": answer, "id": record.get("id")})
    return found


def prompt(cfg: HarnessConfig, slug: str, task: str | None, message: str | None,
           so_far: list[dict[str, Any]] | None = None) -> tuple[str, str]:
    """What this run asks, and the prompt that asks it (with the earlier conversation when there is one)."""
    so_far = turns(cfg, slug) if so_far is None else so_far
    answered = [turn for turn in so_far if turn["answer"] is not None]
    if message:
        asked = message
    elif so_far and so_far[-1]["answer"] is None:
        asked = so_far[-1]["asked"]  # the last run gave no answer: ask it again
    elif not so_far:
        if not task:
            raise AskError(f"Nothing has been asked as {slug} yet: give the question with --task.")
        asked = task
    else:
        raise AskError(f"{slug} is answered: follow up with --message.")
    if not answered:
        return asked, asked
    earlier: list[str] = []
    size = 0
    for turn in reversed(answered):
        text = f"<question>\n{turn['asked']}\n</question>\n<answer>\n{turn['answer']}\n</answer>"
        if earlier and size + len(text) > EARLIER_CHARS:
            break
        earlier.insert(0, text)
        size += len(text)
    return asked, FOLLOW_UP.format(earlier="\n".join(earlier), message=asked)


def spec(cfg: HarnessConfig, slug: str, task: str | None, message: str | None) -> job.JobSpec:
    so_far = turns(cfg, slug)
    asked, text = prompt(cfg, slug, task, message, so_far)
    return job.JobSpec(name=slug, path=cfg.resolve_state_dir(f"{ROOT}/{slug}"), prompt=text, settings={},
                       root=ROOT, asked=asked, turn=len(so_far) + 1)


def run(slug: str, task: str | None, message: str | None, overrides: dict[str, Any], **kwargs: Any) -> int:
    """One run of the question. 0 when it was answered. `kwargs` go to job.run."""
    if not SLUG.fullmatch(slug):
        raise SystemExit(f"{slug!r} is not a slug: lowercase letters, digits and dashes, up to 48.")
    try:
        # A question takes the settings a job does, with none of its own.
        cfg = job.config(job.JobSpec(name=slug, path=Path(), prompt="", settings={}, root=ROOT), overrides)
        return job.run(spec(cfg, slug, task, message), cfg, **kwargs)
    except (job.JobError, AskError) as err:
        raise SystemExit(str(err)) from err
