"""Where a burst starts from: the repo, not an earlier burst's conversation.

The task document and git are the record a task carries between bursts and rounds. Restoring the previous
burst's messages instead brought a round's stale goal back with them: a next round began under all of round
one's conversation, and the window, cutting it back, kept round one's prompt and first read of the document
pinned at the top, contradicting the work at hand. So with `resume_from = "repo"` (the default) every headless
burst starts a fresh conversation: its first prompt carries where_you_are() (the task document as it is now
and what git says), and an answer to a question the last burst asked comes with that question. The previous
burst's snapshot is set aside, not deleted; the burst's transcript has every message anyway. `chat`, with a
person at the prompt, still restores it, as does `resume_from = "snapshot"`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import git, state
from .context import HarnessContext

RESUME_FROM = ("repo", "snapshot")
LOG_LIMIT = 30  # commits on the branch listed, newest first
SECTION_LIMIT = 4000  # characters of git's output shown, each


def session_dir(state_dir: Path, slug: str) -> Path:
    """Where the session manager keeps a task's snapshots (agent.build_agent's storage, the slug its id)."""
    return state_dir / "session" / slug


def set_aside(state_dir: Path, slug: str) -> bool:
    """Move the task's snapshot out of the way, so the next agent built starts with no messages. One previous
    snapshot is kept beside it, as `<slug>.previous`. Returns whether there was one."""
    current = session_dir(state_dir, slug)
    if not current.exists():
        return False
    previous = current.with_name(f"{slug}.previous")
    shutil.rmtree(previous, ignore_errors=True)
    current.rename(previous)
    return True


def _asks_file(state_dir: Path, slug: str) -> Path:
    return state_dir / "asks" / f"{slug}.json"


def keep_asks(state_dir: Path, slug: str, asks: list[str]) -> None:
    """What a burst that ended waiting asked, for the burst that brings the answer."""
    path = _asks_file(state_dir, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asks))


def take_asks(state_dir: Path, slug: str) -> list[str]:
    """The questions the last burst left waiting on, once: they are answered, or not, by this burst."""
    path = _asks_file(state_dir, slug)
    try:
        asks = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    path.unlink(missing_ok=True)
    return [str(ask) for ask in asks] if isinstance(asks, list) else []


def _cut(text: str) -> str:
    return text if len(text) <= SECTION_LIMIT else text[:SECTION_LIMIT] + "\n... [cut]"


def where_you_are(ctx: HarnessContext) -> str:
    """The task document as it is now, the branch's commits since the base, and anything uncommitted."""
    cfg, target = ctx.cfg, ctx.target
    doc = target.read_file(state.doc_relpath(cfg, ctx.slug))
    try:
        log = git.run(target, "log", "--oneline", f"-{LOG_LIMIT}", f"{cfg.remote}/{cfg.base_branch}..HEAD",
                      check=False).strip()
    except git.GitError:
        log = ""
    try:
        status = git.run(target, "status", "--short", check=False).strip()
    except git.GitError:
        status = ""
    return (
        f"<task-document path=\"{state.doc_relpath(cfg, ctx.slug)}\">\n"
        f"{(doc or '(none yet)').strip()}\n</task-document>\n\n"
        f"<commits branch=\"{ctx.branch}\" since=\"{cfg.remote}/{cfg.base_branch}\">\n"
        f"{_cut(log) or '(none yet)'}\n</commits>\n\n"
        f"<uncommitted>\n{_cut(status) or '(nothing)'}\n</uncommitted>"
    )
