"""A project's record, at the repo root: PLAN.md, PROGRESS.md, DECISIONS.md and BACKLOG.md (docs/project-layout.md).

A repo is a project when its PLAN.md has a `## Roadmap` of milestones. Then these files, not the harness's task
document, are what a burst starts from and what it keeps current, and the agent writes them only through the
harness's tools, which keep their shape:

    PLAN.md       ## Roadmap / ### M3 — Title / Build: ... / Accept: - M3.1 ... / Checkpoint: ...
    PROGRESS.md   Current: M3 — Title / ## Milestones (ticks, evidence) / ## Blockers / ## Measured / ## Log
    DECISIONS.md  ## D-008 — Title (M3, 2026-11-21) / What / Why / Alternatives
    BACKLOG.md    ## Ideas / - ...

The parser takes what a person may also have written by hand: bold labels (`**Accept:**`), `Session log` for
`Log`, items struck through with `~~`. It writes what it reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .exec_target import ExecTarget

PLAN, PROGRESS, DECISIONS, BACKLOG, FEEDBACK = "PLAN.md", "PROGRESS.md", "DECISIONS.md", "BACKLOG.md", "FEEDBACK.md"

MILESTONE = re.compile(r"^###\s+(M\d+[a-z]?)\b\s*[—:–-]*\s*(.*?)\s*$")
ITEM = re.compile(r"^\s*[-*]\s+(~~)?\s*(M\d+[a-z]?\.\d+)\b[.:]?\s*(.*?)\s*(~~)?\s*$")
LABEL = re.compile(r"^\s*(?:\*\*)?(Build|Accept|Checkpoint)(?:\*\*)?\s*:?(?:\*\*)?\s*:?\s*(.*)$", re.I)
TICK = re.compile(r"^(\s*)[-*]\s+\[([ xX])\]\s+(M\d+[a-z]?(?:\.\d+)?)\b\s*(.*)$")
CURRENT = re.compile(r"^Current:\s*(M\d+[a-z]?)\b.*$", re.M)
DECISION = re.compile(r"^#{2,3}\s+D-(\d+)\b", re.M)
LOG_HEADINGS = ("Log", "Session log")

PROGRESS_SECTIONS = ("Milestones", "Blockers", "Measured", "Log")
MEASURED_HEADER = "| Date | What | Value | Target |\n|---|---|---|---|"


class ProjectError(ValueError):
    """The record can't be read or written as asked: said to the agent, which can fix what it gave."""


@dataclass
class Item:
    id: str
    text: str
    struck: bool = False


@dataclass
class Milestone:
    id: str
    title: str
    build: str = ""
    items: list[Item] = field(default_factory=list)
    checkpoint: str = ""


@dataclass
class Plan:
    milestones: list[Milestone]

    def find(self, milestone_id: str) -> Milestone | None:
        return next((m for m in self.milestones if m.id == milestone_id), None)


def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


# --- PLAN.md -----------------------------------------------------------------


def _section(text: str, heading: str) -> str | None:
    """The body of `## heading` (any case, a section number before it allowed: `## 9. Roadmap`), up to the
    next `## `."""
    found = re.search(rf"^##\s+(?:[\d.]+\s+)?{re.escape(heading)}\s*$", text, re.M | re.I)
    if not found:
        return None
    rest = text[found.end():]
    end = re.search(r"^##\s", rest, re.M)
    return rest[:end.start()] if end else rest


def parse_plan(text: str) -> Plan:
    roadmap = _section(text, "Roadmap")
    if roadmap is None:
        return Plan([])
    milestones: list[Milestone] = []
    label = ""
    for line in roadmap.splitlines():
        if found := MILESTONE.match(line):
            milestones.append(Milestone(found.group(1), found.group(2)))
            label = ""
            continue
        if not milestones:
            continue
        current = milestones[-1]
        if found := LABEL.match(line):
            label = found.group(1).lower()
            line = found.group(2)
        if label == "accept" and (item := ITEM.match(line)):
            current.items.append(Item(item.group(2), item.group(3).strip(), struck=bool(item.group(1))))
        elif label in ("build", "checkpoint") and line.strip():
            setattr(current, label, (getattr(current, label) + "\n" + line.strip()).strip())
    return Plan(milestones)


def _number(ident: str) -> str:
    """M3, M3a, M3.2 -> "3": the milestone a split one, or an item, belongs to."""
    found = re.match(r"M(\d+)", ident)
    return found.group(1) if found else ident


def _milestone_spans(text: str) -> dict[str, tuple[int, int]]:
    """Where each `### M<n>` section of the roadmap starts and ends in PLAN.md's text."""
    roadmap = re.search(r"^##\s+(?:[\d.]+\s+)?Roadmap\s*$", text, re.M | re.I)
    if roadmap is None:
        return {}
    end = re.search(r"^##\s", text[roadmap.end():], re.M)
    stop = roadmap.end() + end.start() if end else len(text)
    heads = [(found.start() + roadmap.end(), found.group(1))
             for found in re.finditer(r"^###\s+(M\d+[a-z]?)\b.*$", text[roadmap.end():stop], re.M)]
    return {ident: (start, heads[i + 1][0] if i + 1 < len(heads) else stop) for i, (start, ident) in enumerate(heads)}


def check_plan(plan_text: str, progress_text: str | None = None) -> list[str]:
    """What is wrong with the plan's layout, as sentences: nothing when it is fine."""
    plan = parse_plan(plan_text)
    problems = []
    if not plan.milestones:
        problems.append("PLAN.md has no `## Roadmap` with milestones (`### M0 — Title`).")
    seen: set[str] = set()
    for milestone in plan.milestones:
        if milestone.id in seen:
            problems.append(f"{milestone.id} is in the roadmap twice.")
        seen.add(milestone.id)
        if not [item for item in milestone.items if not item.struck]:
            problems.append(f"{milestone.id} has no Accept items (`- {milestone.id}.1 ...` under `Accept:`).")
        for item in milestone.items:
            if _number(item.id) != _number(milestone.id):  # M3a and M3b, split from M3, keep M3's items
                problems.append(f"{item.id} is under {milestone.id}: its id should start {milestone.id}.")
            if item.id in seen:
                problems.append(f"{item.id} is in the plan twice.")
            seen.add(item.id)
    if progress_text is not None:
        current = current_id(progress_text)
        if current is None:
            problems.append("PROGRESS.md has no `Current: M<n> — Title` line.")
        elif plan.milestones and plan.find(current) is None:
            problems.append(f"PROGRESS.md's current milestone {current} is not in the plan.")
    return problems


# --- PROGRESS.md -------------------------------------------------------------


def current_id(progress_text: str) -> str | None:
    found = CURRENT.search(progress_text)
    return found.group(1) if found else None


def ticks(progress_text: str) -> dict[str, tuple[bool, str]]:
    """Each milestone's and criterion's tick in PROGRESS.md, with what follows its id (the evidence)."""
    found: dict[str, tuple[bool, str]] = {}
    for line in (_section(progress_text, "Milestones") or "").splitlines():
        if tick := TICK.match(line):
            found[tick.group(3)] = (tick.group(2) != " ", tick.group(4).strip())
    return found


@dataclass
class Progress:
    """PROGRESS.md as its parts: the lines before the first section, and each section's body by heading."""

    head: str
    sections: dict[str, str]

    @classmethod
    def parse(cls, text: str) -> "Progress":
        parts = re.split(r"^##\s+(.+?)\s*$", text, flags=re.M)
        sections: dict[str, str] = {}
        for heading, body in zip(parts[1::2], parts[2::2]):
            name = "Log" if heading in LOG_HEADINGS else heading
            sections[name] = body.strip("\n")
        return cls(parts[0].strip("\n"), sections)

    def render(self) -> str:
        out = [self.head.rstrip(), ""]
        for name, body in self.sections.items():
            out += [f"## {name}", "", body.strip("\n"), ""] if body.strip() else [f"## {name}", ""]
        return "\n".join(out).rstrip() + "\n"

    def section(self, name: str) -> str:
        return self.sections.setdefault(name, "")


def new_progress(plan: Plan, current: str | None = None) -> str:
    current = current or (plan.milestones[0].id if plan.milestones else "M0")
    milestone = plan.find(current)
    return (f"# Progress\n\nCurrent: {current} — {milestone.title if milestone else ''}\n\n"
            + "".join(f"## {name}\n\n" for name in PROGRESS_SECTIONS))


def sync_milestones(progress: Progress, plan: Plan) -> None:
    """The Milestones section from the plan, keeping each tick and its evidence: a line per milestone, and under
    the current one a line per Accept item (struck items left out)."""
    held = ticks("## Milestones\n" + progress.section("Milestones"))
    current = current_id(progress.head)
    lines = []
    for milestone in plan.milestones:
        done, rest = held.get(milestone.id, (False, ""))
        lines.append(f"- [{'x' if done else ' '}] {milestone.id} — {_after_title(rest, milestone.title)}")
        if milestone.id != current:
            continue
        for item in milestone.items:
            if item.struck:
                continue
            ticked, evidence = held.get(item.id, (False, ""))
            lines.append(f"  - [{'x' if ticked else ' '}] {item.id} {_after_title(evidence, item.text)}")
    progress.sections["Milestones"] = "\n".join(lines)


def _after_title(rest: str, title: str) -> str:
    """A line's text after its id: kept when it has evidence or a note (it begins with the title), else the title."""
    rest = rest.lstrip("— ").strip()
    return rest if rest.startswith(title[:30]) and rest else title


# --- the repo ----------------------------------------------------------------


@dataclass
class Record:
    """A project's files in a target, read when asked for, written through the operations below."""

    target: ExecTarget

    def read(self, name: str) -> str | None:
        return self.target.read_file(name)

    def plan(self) -> Plan:
        return parse_plan(self.read(PLAN) or "")

    def progress(self) -> Progress:
        text = self.read(PROGRESS)
        if text is None:
            text = new_progress(self.plan())
        return Progress.parse(text)

    def write_progress(self, progress: Progress) -> None:
        sync_milestones(progress, self.plan())
        self.target.write_file(PROGRESS, progress.render())

    def current(self) -> Milestone | None:
        plan = self.plan()
        found = current_id(self.progress().head)
        return plan.find(found) if found else (plan.milestones[0] if plan.milestones else None)

    def view(self, log_entries: int = 1) -> str:
        """What a session needs: the current milestone with each item's tick and evidence, blockers, feedback,
        and the last log entries. For the start of a burst and a window cut (handover), and current_milestone."""
        plan, progress = self.plan(), self.progress()
        current = plan.find(current_id(progress.head) or "") or (plan.milestones[0] if plan.milestones else None)
        if current is None:
            return "PLAN.md has no milestones."
        held_all = ticks("## Milestones\n" + progress.section("Milestones"))
        if held_all.get(current.id, (False,))[0] and current is plan.milestones[-1]:
            return f"Every milestone in PLAN.md is done; {current.id} — {current.title} was the last."
        held = ticks("## Milestones\n" + progress.section("Milestones"))
        done = sum(1 for m in plan.milestones if held.get(m.id, (False, ""))[0])
        lines = [f"Current milestone: {current.id} — {current.title} "
                 f"({done} of {len(plan.milestones)} milestones done)", ""]
        if current.build:
            lines += ["Build:", current.build, ""]
        lines.append("Accept:")
        for item in current.items:
            if item.struck:
                continue
            ticked, evidence = held.get(item.id, (False, ""))
            note = f" — {evidence.split(' — ', 1)[1]}" if ticked and " — " in evidence else ""
            lines.append(f"- [{'x' if ticked else ' '}] {item.id} {item.text}{note}")
        if current.checkpoint:
            lines += ["", f"Checkpoint (for the reviewer, never blocking): {current.checkpoint}"]
        blockers = progress.section("Blockers").strip()
        lines += ["", "Blockers:", blockers or "(none)"]
        if feedback := (self.read(FEEDBACK) or "").strip():
            lines += ["", f"<feedback from {FEEDBACK}: the person's steering; it comes first>", feedback, "</feedback>"]
        entries = _log_entries(progress.section("Log"))
        if entries and log_entries:
            lines += ["", "Last log entry:" if log_entries == 1 else "Last log entries:", *entries[-log_entries:]]
        return "\n".join(lines).rstrip()

    # --- operations: what the tools do -------------------------------------

    def log(self, heading: str, *, done: str = "", next_step: str = "", note: str = "") -> None:
        """Add to the log entry under `heading` (made at the end of the log if it isn't there)."""
        progress = self.progress()
        log = progress.section("Log")
        # Each entry ends in one newline, so the next one's heading starts a line of its own.
        entries = [part.rstrip("\n") + "\n" for part in re.split(r"(?=^###\s)", log, flags=re.M) if part.strip()]
        at = next((i for i, entry in enumerate(entries) if entry.split("\n", 1)[0] == f"### {heading}"), None)
        if at is None:
            entries.append(f"### {heading}\n")
            at = len(entries) - 1
        entry = entries[at]
        if done:
            entry += "".join(f"- {line.strip().removeprefix('- ')}\n" for line in done.strip().splitlines() if line.strip())
        if note:
            entry += f"{note.strip()}\n"
        if next_step:
            entry = re.sub(r"^Next: .*\n", "", entry, flags=re.M) + f"Next: {next_step.strip()}\n"
        entries[at] = entry
        progress.sections["Log"] = "\n".join(entries).strip("\n")
        self.write_progress(progress)

    def add_blocker(self, text: str) -> None:
        progress = self.progress()
        body = progress.section("Blockers").rstrip()
        progress.sections["Blockers"] = (body + "\n" if body else "") + f"- {text.strip()} (since {today()})"
        self.write_progress(progress)

    def resolve_blocker(self, words: str) -> str:
        """Take the blocker matching `words` off the list. Returns its text."""
        progress = self.progress()
        lines = progress.section("Blockers").splitlines()
        matching = [i for i, line in enumerate(lines) if line.lstrip().startswith(("-", "*"))
                    and words.strip().lower() in line.lower()]
        if len(matching) != 1:
            listed = "\n".join(line for line in lines if line.strip()) or "(none)"
            raise ProjectError(f"{'No' if not matching else 'More than one'} blocker matches {words!r}. "
                               f"The blockers:\n{listed}")
        gone = lines.pop(matching[0])
        progress.sections["Blockers"] = "\n".join(lines).strip("\n")
        self.write_progress(progress)
        return gone.lstrip(" -*")

    def measure(self, what: str, value: str, target: str = "") -> None:
        progress = self.progress()
        body = progress.section("Measured").strip() or MEASURED_HEADER
        cells = [today(), what, value, target or "—"]
        row = "| " + " | ".join(cell.replace("|", "\\|").replace("\n", " ").strip() for cell in cells) + " |"
        progress.sections["Measured"] = body + "\n" + row
        self.write_progress(progress)

    def decide(self, title: str, what: str, why: str, alternatives: str = "", *, milestone: str = "",
               plan_change: bool = False) -> str:
        """Add the next D-NNN to DECISIONS.md. Returns its id."""
        text = self.read(DECISIONS) or "# Decisions\n\nAppend-only log of decisions and deviations from `PLAN.md`.\n"
        number = max((int(n) for n in DECISION.findall(text)), default=0) + 1
        ident = f"D-{number:03d}"
        where = ", ".join(part for part in (milestone, today()) if part)
        entry = [f"## {ident} — {'Plan change: ' if plan_change else ''}{title.strip()} ({where})", "",
                 f"- **What:** {what.strip()}", f"- **Why:** {why.strip()}"]
        if alternatives.strip():
            entry.append(f"- **Alternatives:** {alternatives.strip()}")
        self.target.write_file(DECISIONS, text.rstrip() + "\n\n" + "\n".join(entry) + "\n")
        return ident

    def tick(self, item_id: str, evidence: str) -> Item:
        """Tick one of the current milestone's Accept items, with what shows it is met."""
        plan, progress = self.plan(), self.progress()
        current = plan.find(current_id(progress.head) or "")
        if current is None:
            raise ProjectError("PROGRESS.md names no current milestone that is in the plan.")
        item = next((i for i in current.items if i.id == item_id.strip() and not i.struck), None)
        if item is None:
            listed = ", ".join(i.id for i in current.items if not i.struck)
            raise ProjectError(f"{item_id} is not an Accept item of the current milestone, {current.id} ({listed}).")
        if not evidence.strip():
            raise ProjectError("Give the evidence: the test or command that shows it is met, and its result.")
        sync_milestones(progress, plan)
        line = f"  - [x] {item.id} {item.text} — {' '.join(evidence.split())} ({today()})"
        progress.sections["Milestones"] = re.sub(rf"^\s*[-*]\s+\[[ xX]\]\s+{re.escape(item.id)}\b.*$",
                                                 lambda _: line, progress.section("Milestones"), flags=re.M)
        self.write_progress(progress)
        return item

    def unticked(self) -> list[Item]:
        """The current milestone's Accept items not yet ticked."""
        plan = self.plan()
        progress_text = self.read(PROGRESS) or ""
        current = plan.find(current_id(progress_text) or "")
        held = ticks(progress_text)
        return [] if current is None else [i for i in current.items if not i.struck and not held.get(i.id, (0,))[0]]

    def criteria(self) -> dict[str, int] | None:
        """The current milestone's Accept items: how many are ticked, of how many."""
        current = self.current()
        if current is None:
            return None
        total = len([i for i in current.items if not i.struck])
        return {"ticked": total - len(self.unticked()), "total": total}

    def advance(self, base_progress: str | None) -> str | None:
        """Move on to the next milestone when the current one's Accept items are all ticked in `base_progress`,
        PROGRESS.md as the base branch has it: its PR was merged. Returns the new milestone's id, if it moved."""
        if not base_progress:
            return None
        plan, progress = self.plan(), self.progress()
        ident = current_id(progress.head)
        current = plan.find(ident or "")
        if current is None:
            return None
        held = ticks(base_progress)
        if any(not item.struck and not held.get(item.id, (0,))[0] for item in current.items):
            return None
        at = plan.milestones.index(current)
        later = plan.milestones[at + 1] if at + 1 < len(plan.milestones) else None
        lines = progress.section("Milestones").splitlines()
        progress.sections["Milestones"] = "\n".join(
            re.sub(r"\[ \]", "[x]", line, count=1) if re.match(rf"^[-*]\s+\[ \]\s+{re.escape(current.id)}\b", line)
            else line for line in lines)
        if later is None:
            self.write_progress(progress)
            return None
        progress.head = CURRENT.sub(f"Current: {later.id} — {later.title}", progress.head)
        self.write_progress(progress)
        return later.id

    def amend(self, target: str, text: str, title: str, why: str, alternatives: str = "") -> tuple[str, str]:
        """Change PLAN.md: a milestone's section replaced (`M3`; split into M3a and M3b; empty text drops it), new
        milestones added (`after M3`), or another `##` section's body replaced (its heading, or a word of it). The
        result must pass check_plan, and Accept items are struck (`~~M3.2 ...~~`), never deleted, unless their
        milestone goes. Records a plan-change decision with what the plan said before. Returns the decision's id
        and the kind of change: "roadmap" (milestones added, dropped or reshaped, or anything outside the
        roadmap) or "item" (within a milestone)."""
        old = self.read(PLAN) or ""
        spans = _milestone_spans(old)
        wanted = target.strip()
        text = text.strip()
        if re.fullmatch(r"M\d+[a-z]?", wanted):
            if wanted not in spans:
                raise ProjectError(f"There is no {wanted} in the roadmap ({', '.join(spans)}).")
            start, end = spans[wanted]
            was = old[start:end].strip()
            new = old[:start] + (text + "\n\n" if text else "") + old[end:]
            section = False
        elif re.fullmatch(r"after\s+M\d+[a-z]?", wanted, re.I):
            after = wanted.split()[1]
            if after not in spans:
                raise ProjectError(f"There is no {after} in the roadmap ({', '.join(spans)}).")
            if not text:
                raise ProjectError("Give the new milestones' text.")
            end = spans[after][1]
            was = ""
            new = old[:end].rstrip("\n") + "\n\n" + text + "\n\n" + old[end:].lstrip("\n")
            section = False
        else:
            found = [m for m in re.finditer(r"^##\s+(.+?)\s*$", old, re.M)
                     if wanted.lower().lstrip("# ") in m.group(1).lower()]
            if len(found) != 1:
                headings = ", ".join(m.group(1) for m in re.finditer(r"^##\s+(.+?)\s*$", old, re.M))
                raise ProjectError(f"Name a milestone (M3), `after M3`, or one `##` section ({headings}).")
            if re.search(r"roadmap", found[0].group(1), re.I):
                raise ProjectError("Change the roadmap a milestone at a time: M3, or after M3.")
            rest = old[found[0].end():]
            nxt = re.search(r"^##\s", rest, re.M)
            end = found[0].end() + (nxt.start() if nxt else len(rest))
            was = old[found[0].end():end].strip()
            new = old[:found[0].end()] + "\n\n" + text + "\n\n" + old[end:].lstrip("\n")
            section = True
        if problems := check_plan(new):
            raise ProjectError("The plan would not be laid out as the harness reads it:\n" + "\n".join(problems))
        before, after_plan = parse_plan(old), parse_plan(new)
        kept = {m.id for m in after_plan.milestones}
        lost = [item.id for m in before.milestones if any(_number(k) == _number(m.id) for k in kept)
                for item in m.items if not item.struck and item.id not in {i.id for n in after_plan.milestones
                                                                           for i in n.items}]
        if lost:
            raise ProjectError(f"{', '.join(lost)} would be gone: strike an item through (`- ~~M3.2 ...~~`) rather "
                               "than deleting it, so its id is never used again.")
        reshaped = [m.id for m in before.milestones] != [m.id for m in after_plan.milestones]
        kind = "roadmap" if section or reshaped else "item"
        self.target.write_file(PLAN, new)
        progress = self.progress()
        ident = current_id(progress.head)
        if after_plan.find(ident or "") is None:  # the current milestone was split or dropped
            later = next((m for m in after_plan.milestones if m.id not in {b.id for b in before.milestones}), None)
            later = later or (after_plan.milestones[0] if after_plan.milestones else None)
            if later is not None:
                progress.head = CURRENT.sub(f"Current: {later.id} — {later.title}", progress.head)
        self.write_progress(progress)
        current = self.current()
        cut = was if len(was) <= 1500 else was[:1500] + " [...]"
        decision = self.decide(title, f"PLAN.md, {wanted}, changed." + (f" It said:\n\n```\n{cut}\n```" if was else ""),
                               why, alternatives, milestone=current.id if current else "", plan_change=True)
        return decision, kind

    def add_backlog(self, idea: str) -> None:
        text = self.read(BACKLOG) or ("# Backlog\n\nOut-of-scope ideas and deferred bugs. Nothing here is built "
                                      "without a person saying so.\n\n## Ideas\n")
        line = f"- {idea.strip()} ({today()})"
        ideas = re.search(r"^##\s+Ideas\s*$", text, re.M)
        if ideas is None:
            text = text.rstrip() + "\n\n## Ideas\n\n" + line + "\n"
        else:
            rest = text[ideas.end():]
            nxt = re.search(r"^##\s", rest, re.M)
            body, after = (rest[:nxt.start()], rest[nxt.start():]) if nxt else (rest, "")
            body = (body.rstrip() + "\n" if body.strip() else "\n\n") + line + "\n"
            text = text[:ideas.end()] + body + ("\n" + after if after else "")
        self.target.write_file(BACKLOG, text)


def _log_entries(log: str) -> list[str]:
    return [entry.strip() for entry in re.split(r"(?=^###\s)", log, flags=re.M) if entry.strip().startswith("###")]


def plan_change(before: str, after: str) -> str:
    """How PLAN.md changed: "roadmap" (milestones added, dropped or reshaped, or anything outside the roadmap: the
    goal, the scope, the design), "item" (only within milestones), or "" (not at all)."""
    if " ".join(before.split()) == " ".join(after.split()):
        return ""

    def outside(text: str) -> str:
        spans = _milestone_spans(text)
        if spans:
            start, end = min(s for s, _ in spans.values()), max(e for _, e in spans.values())
            text = text[:start] + text[end:]
        return " ".join(text.split())

    ids = [[m.id for m in parse_plan(text).milestones] for text in (before, after)]
    return "roadmap" if ids[0] != ids[1] or outside(before) != outside(after) else "item"


def is_project(target: ExecTarget) -> bool:
    """A repo with a plan of milestones: its record is these files, not the harness's task document."""
    text = target.read_file(PLAN)
    return text is not None and bool(parse_plan(text).milestones)
