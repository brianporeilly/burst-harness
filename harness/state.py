"""The committed task document -- the thing a fresh session reads instead of the code.

One file per task, fixed sections, hard size cap. The cap is the point: an
uncapped handoff document grows into the token problem it was meant to solve,
so an oversized write is rejected rather than silently truncated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from .config import HarnessConfig
from .exec_target import ExecTarget

SECTIONS = ("Goal", "Constraints", "Done", "Decisions", "Open questions", "Next step")

_PLACEHOLDER = "_(none yet)_"


@dataclass(frozen=True)
class TaskDoc:
    slug: str
    goal: str = ""
    constraints: str = ""
    done: str = ""
    decisions: str = ""
    open_questions: str = ""
    next_step: str = ""

    _FIELD_BY_SECTION = {
        "Goal": "goal",
        "Constraints": "constraints",
        "Done": "done",
        "Decisions": "decisions",
        "Open questions": "open_questions",
        "Next step": "next_step",
    }

    def render(self) -> str:
        lines = [f"# Task: {self.slug}", ""]
        for section in SECTIONS:
            value = getattr(self, self._FIELD_BY_SECTION[section]).strip()
            lines += [f"## {section}", "", value or _PLACEHOLDER, ""]
        return "\n".join(lines).rstrip() + "\n"

    @classmethod
    def parse(cls, slug: str, text: str) -> "TaskDoc":
        found: dict[str, str] = {}
        # Split on level-2 headings, keeping the heading text as the key.
        parts = re.split(r"^##\s+(.+?)\s*$", text, flags=re.MULTILINE)
        for heading, body in zip(parts[1::2], parts[2::2]):
            field = cls._FIELD_BY_SECTION.get(heading.strip())
            if field:
                cleaned = body.strip()
                found[field] = "" if cleaned == _PLACEHOLDER else cleaned
        return cls(slug=slug, **found)


def doc_relpath(cfg: HarnessConfig, slug: str) -> str:
    """Repo-relative path of the task document."""
    return f"{cfg.docs_dir}/{slug}.md"


def read(target: ExecTarget, cfg: HarnessConfig, slug: str) -> TaskDoc:
    text = target.read_file(doc_relpath(cfg, slug))
    return TaskDoc(slug=slug) if text is None else TaskDoc.parse(slug, text)


def write(target: ExecTarget, cfg: HarnessConfig, doc: TaskDoc) -> str:
    """Write the doc, refusing to exceed the configured cap.

    Refusing rather than truncating is deliberate: a handoff document that grows
    without bound becomes the token problem it exists to solve.
    """
    rendered = doc.render()
    size = len(rendered.encode("utf-8"))
    if size > cfg.doc_max_bytes:
        raise ValueError(
            f"Task document would be {size} bytes, over the {cfg.doc_max_bytes}-byte cap. "
            "Summarise instead of appending: keep Done to outcomes rather than narrative, "
            "drop resolved questions, and keep Next step to a single actionable item."
        )
    relative = doc_relpath(cfg, doc.slug)
    target.write_file(relative, rendered)
    return relative


def merge(doc: TaskDoc, **updates: str | None) -> TaskDoc:
    """Replace only the sections given a non-None value."""
    return replace(doc, **{k: v for k, v in updates.items() if v is not None})
