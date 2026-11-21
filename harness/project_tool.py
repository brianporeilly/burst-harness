"""The project tool: a project's record kept from a shell, for an agent without the harness's own tools.

The Claude Code engine copies this file and project.py into its target beside each other, and tells Claude to run
`python3 <dir>/project_tool.py <command>` from the repo. The commands are the Strands tools' (tools.py,
project_tools): what is written, and its format, are project.py's either way. Run with no command for the list.

Beside it, project_tool.json says what the harness knows and the tool can't: this burst's log heading, or when the
burst started and its branch to make one. What the tool records is appended to records.jsonl there, one kind a
line, which the harness counts into its metrics (metrics.records) when the burst ends.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

try:
    from . import project
except ImportError:  # run as a script, beside a copy of project.py
    import project  # type: ignore[no-redef]

HERE = Path(__file__).resolve().parent
SETTINGS, RECORDS = HERE / "project_tool.json", HERE / "records.jsonl"


class Repo:
    """The repo the tool is run in, as project.Record reads and writes it."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def read_file(self, relative: str) -> str | None:
        path = self.root / relative
        return path.read_text() if path.is_file() else None

    def write_file(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def _root() -> Path:
    found = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
    return Path(found.stdout.strip()) if found.returncode == 0 and found.stdout.strip() else Path.cwd()


def _settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def _heading(record: project.Record) -> str:
    """This burst's log entry: the harness's, or one made the same way."""
    settings = _settings()
    if settings.get("heading"):
        return settings["heading"]
    started = settings.get("started")
    when = datetime.fromtimestamp(started, UTC) if started else datetime.now(UTC)
    current = record.current()
    branch = settings.get("branch") or subprocess.run(
        ["git", "branch", "--show-current"], capture_output=True, text=True, check=False).stdout.strip()
    return f"{when.strftime('%Y-%m-%d %H:%MZ')} — {current.id if current else 'no milestone'} · {branch}"


def _count(kind: str, **more: str) -> None:
    try:
        with RECORDS.open("a") as out:
            out.write(json.dumps({"kind": kind, **more}) + "\n")
    except OSError:
        pass  # a count the metrics go without, never a failed record


def _text(value: str | None) -> str:
    """An argument, or stdin for `-`: for text with quotes or newlines a shell would mangle."""
    return sys.stdin.read() if value == "-" else (value or "")


def run(args: argparse.Namespace, record: project.Record) -> str:
    command = args.command
    if command == "status":
        return record.view()
    if command == "plan":
        return project.plan_part(record.read(project.PLAN) or "", args.section)
    if command == "check":
        ok, said = project.check_summary(record.read(project.PLAN), record.read(project.PROGRESS))
        if not ok:
            raise project.ProjectError(said)
        return said
    if command == "tick":
        ticked = record.tick(args.item, _text(args.evidence))
        _count("ticks")
        left = record.unticked()
        return (f"Ticked {ticked.id}. " + (f"Still to meet: {', '.join(i.id for i in left)}." if left else
                                           "Every Accept item of this milestone is met: finish with a PR."))
    if command == "log":
        if not (args.done or args.next):
            raise project.ProjectError("Give --done, --next, or both.")
        record.log(_heading(record), done=_text(args.done), next_step=_text(args.next))
        _count("progress")
        return f"Recorded in {project.PROGRESS}: log entry updated."
    if command == "blocker":
        record.add_blocker(_text(args.text))
        _count("progress")
        return f"Recorded in {project.PROGRESS}: blocker added."
    if command == "resolve":
        gone = record.resolve_blocker(args.words)
        record.log(_heading(record), note=f"Resolved: {gone}")
        _count("progress")
        return f"Recorded in {project.PROGRESS}: blocker resolved ({gone})."
    if command == "measure":
        record.measure(args.what, args.value, args.target or "")
        _count("progress")
        return f"Recorded in {project.PROGRESS}: measurement added."
    if command == "decide":
        current = record.current()
        ident = record.decide(args.title, _text(args.what), _text(args.why), args.alternatives or "",
                              milestone=current.id if current else "")
        _count("decisions")
        return f"Recorded {ident} in {project.DECISIONS}."
    if command == "backlog":
        record.add_backlog(_text(args.idea))
        _count("backlog")
        return f"Added to {project.BACKLOG}."
    if command == "amend":
        ident, kind = record.amend(args.target, _text(args.text), args.title, _text(args.why), args.alternatives or "")
        _count("plan_changes", plan_change=kind)
        current = record.current()
        return (f"Changed PLAN.md and recorded {ident}." + (f" The current milestone is {current.id}." if current else "")
                + (" It changes the roadmap: the PR puts it first, and the next round waits for a person."
                   if kind == "roadmap" else ""))
    raise project.ProjectError(f"No command {command}.")


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="project_tool.py", description="Keep a project's PLAN.md, PROGRESS.md, DECISIONS.md and BACKLOG.md "
        "in their format. Run it from the repo. Where a text argument is `-`, it is read from stdin.")
    sub = top.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="The current milestone: what to build, its Accept items, blockers, the last log.")
    sub.add_parser("plan", help="One part of PLAN.md: a ## heading (or a word of it), or a milestone id (M3)."
                   ).add_argument("section")
    sub.add_parser("check", help="Check PLAN.md and PROGRESS.md against the layout the harness reads.")
    tick = sub.add_parser("tick", help="Tick a current Accept item once it is met, with the evidence.")
    tick.add_argument("item", help="Its id, such as M3.2.")
    tick.add_argument("evidence", help="What shows it is met: the test or command, and its result.")
    log = sub.add_parser("log", help="Add to this session's log entry: what was done, and the next step.")
    log.add_argument("--done", help="Outcomes, one a line.")
    log.add_argument("--next", help="The single next step.")
    sub.add_parser("blocker", help="Add a blocker you can't fix yourself.").add_argument("text")
    sub.add_parser("resolve", help="Take a blocker off the list, by a few of its words.").add_argument("words")
    measure = sub.add_parser("measure", help="Add a row to the Measured table.")
    measure.add_argument("what")
    measure.add_argument("value", help="With units.")
    measure.add_argument("--target", help="Its target, if the plan sets one.")
    decide = sub.add_parser("decide", help="Add a decision to DECISIONS.md, numbered for you.")
    decide.add_argument("title")
    decide.add_argument("--what", required=True)
    decide.add_argument("--why", required=True)
    decide.add_argument("--alternatives")
    sub.add_parser("backlog", help="Put an idea outside the plan in BACKLOG.md.").add_argument("idea")
    amend = sub.add_parser("amend", help="Change PLAN.md, recorded as a decision: a milestone (M3) replaced or "
                           "dropped, milestones added (`after M3`), or a ## section's body replaced.")
    amend.add_argument("target")
    amend.add_argument("--text", required=True, help="The new text in the plan's layout (`-` for stdin); empty "
                       "drops a milestone. Keep items' ids; strike an item (`- ~~M3.2 ...~~`) rather than delete it.")
    amend.add_argument("--title", required=True)
    amend.add_argument("--why", required=True)
    amend.add_argument("--alternatives")
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        print(run(args, project.Record(Repo(_root()))))  # type: ignore[arg-type]
    except ValueError as err:  # project.ProjectError is one: what the record refuses, said as the tools say it
        print(f"Rejected: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
