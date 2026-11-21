"""Keeping the record and the docs current: steering in the prompt, and soft checks (docs/project-layout.md, step 3).

The record (a project's PROGRESS.md and DECISIONS.md, or the task document) is all the next session has, so:

  record_nudge_calls  (40)  that many tool calls with nothing recorded: the next tool result says so
  record_send_back  (true)  a burst that ends (pause, finish) after MIN_WORK tool calls having recorded nothing is
                            sent back once to record where it stands (NOTHING_RECORDED)

Documentation is steered, not checked hard. `docs_style` picks the guidance in the prompt: "diataxis" (the
default), "none", or a path to a file in the repo describing the project's own. At finish, a change to code with no
doc changed gets one reminder, which finishing again passes; the PR says which docs changed, and the review asks.
"""

from __future__ import annotations

from typing import Any

# The harness's tools that add to the record: either kind of repo's.
RECORDING = frozenset(("record_progress", "record_decision", "tick_criterion", "amend_plan", "add_backlog",
                       "update_task_doc"))
MIN_WORK = 10  # tool calls before a burst that recorded nothing is sent back to record it
STYLE_MAX_BYTES = 8192

NUDGE = ("\n[harness] {calls} tool calls since anything was recorded. The record is all the next session has: "
         "record what is done and what is next now ({tools}), then carry on.")
NOTHING_RECORDED = ("Not yet: this burst recorded nothing, and the next session has only the record. Record where the "
                    "work stands first ({tools}): what was done, what was decided, what is next. Then {end} again.")
DOCS_REMINDER = ("Before the PR: the change touches code ({files}) but no documentation. If it adds or changes what a "
                 "user or a developer meets (a command, a setting, an API, how to build or run it), document it "
                 "({style}). If nothing needs documenting, finish again as you are.")

STYLES = {
    "diataxis": (
        "Documentation: keep it current in the same change as the behaviour it describes. It follows Diátaxis: "
        "under docs/, tutorials (learning by doing), how-to guides (one task, step by step), reference (exact facts: "
        "commands, settings, APIs) and explanation (why things are as they are), each page one kind; README.md says "
        "what the project is and how to start. Write for the people who will use or work on it. The work's progress "
        "goes in the record, not in the docs."),
}
DOCS_LABEL = {"diataxis": "Diátaxis, under docs/, and README.md"}


def tools_named(project: bool) -> str:
    return ("record_progress, tick_criterion, record_decision" if project else
            "record_progress, record_decision, update_task_doc")


def docs_note(cfg: Any, target: Any) -> str:
    """For the lead's prompt: the project's documentation style, or "" for none."""
    style = (cfg.docs_style or "").strip()
    if not style or style == "none":
        return ""
    if style in STYLES:
        return "\n\n" + STYLES[style]
    text = target.read_file(style)
    if text is None:
        return ""
    text = text if len(text.encode()) <= STYLE_MAX_BYTES else text[:STYLE_MAX_BYTES] + "\n[cut]"
    return (f"\n\nDocumentation: keep it current in the same change as the behaviour it describes, in this project's "
            f"style ({style}):\n<docs-style>\n{text.strip()}\n</docs-style>")


class Watch:
    """Tool calls since the record was last added to, for the nudge and the end's check."""

    def __init__(self, cfg: Any, ctx: Any) -> None:
        self.every = cfg.record_nudge_calls
        self.ctx = ctx

    def watch(self, agent: Any) -> None:
        from strands.hooks import AfterToolCallEvent

        def after_call(event: AfterToolCallEvent) -> None:
            ctx = self.ctx
            if ctx.setup_round:  # the setup round writes the plan; there is no record to keep yet
                return
            name = str((event.tool_use or {}).get("name"))
            ctx.tool_calls += 1
            if name in RECORDING:
                ctx.since_record = 0
                return
            ctx.since_record += 1
            if self.every and ctx.since_record % self.every == 0 and isinstance(event.result, dict):
                text = NUDGE.format(calls=ctx.since_record, tools=tools_named(ctx.project))
                event.result = {**event.result, "content": [*(event.result.get("content") or []), {"text": text}]}

        agent.hooks.add_callback(AfterToolCallEvent, after_call)


def recorded_nothing(ctx: Any, end: str) -> str | None:
    """Why the burst should record before it ends, the first time it would end having recorded nothing after real
    work; else None. `end` names the tool, for the message."""
    if not ctx.cfg.record_send_back or getattr(ctx, "setup_round", False) or ctx.told_to_record or ctx.tool_calls < MIN_WORK or ctx.records or ctx.since_record < ctx.tool_calls:
        return None
    ctx.told_to_record = True
    return NOTHING_RECORDED.format(tools=tools_named(ctx.project), end=end)


DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
TEST_PARTS = ("test", "tests", "spec", "specs", "__tests__", "testdata", "fixtures")


def docs_reminder(ctx: Any, changed: list[str], record_files: frozenset[str]) -> str | None:
    """The one reminder at finish, when the change touches code and no doc; else None."""
    style = (ctx.cfg.docs_style or "").strip()
    if ctx.docs_reminded or not style or style == "none":
        return None
    # The harness's own task documents (docs_dir) are the record, not documentation.
    own = (ctx.cfg.docs_dir.rstrip("/") + "/") if ctx.cfg.docs_dir else None
    changed = [name for name in changed if not (own and name.startswith(own))]
    docs = [name for name in changed if name not in record_files and (
        name.startswith("docs/") or name.lower().startswith("readme") or name.endswith(DOC_SUFFIXES))]
    code = [name for name in changed if name not in record_files and name not in docs
            and not any(part in TEST_PARTS or part.startswith("test_") for part in name.lower().split("/"))
            and not name.startswith(".")]
    if docs or not code:
        return None
    ctx.docs_reminded = True
    listed = ", ".join(code[:5]) + (f" and {len(code) - 5} more" if len(code) > 5 else "")
    return DOCS_REMINDER.format(files=listed, style=DOCS_LABEL.get(style, f"as {style} describes"))
