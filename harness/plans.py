"""Plans: what a job in act mode proposes to do, and what a person approved of it.

A run of a job with `mode = "act"` is read-only, as a report job's is. Besides its report it writes a plan,
/workspace/plan.json:

    {"items": [{"id": "sender:news@example.com",
                "title": "Archive 40 messages from news@example.com",
                "detail": "Subjects: ... (optional, Markdown)",
                "data": {"ids": ["18c...", "..."]}}]}

Each item is approved or rejected as a whole, by its title and detail, which say what will be done; `data` is
for the job's apply script. The plan is kept here, under the state directory (jobs/NAME/plans/RUN-ID.json), and
nothing waits in memory: a decision is written to the plan's file, and applying it is a run of its own
(job.apply), whenever it is asked for. A new plan supersedes the job's open ones, and a plan expires after
`[job] plan_ttl` (a day by default): what it was made from has moved on.

States: open (deciding), applying, applied, apply_failed, rejected (all of it), superseded, expired.
"""

from __future__ import annotations

import fcntl
import json
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import HarnessConfig

PLAN_FILE = "plan.json"  # in the run's working directory
APPROVED_FILE = "approved.json"  # what the apply script is given, there
MAX_ITEMS = 50
MAX_BYTES = 1024 * 1024
TITLE_CHARS = 200
DETAIL_CHARS = 3000
DEFAULT_TTL = 24 * 3600
ITEM_ID = re.compile(r"[A-Za-z0-9._:@/+=-]{1,100}")
DECISIONS = ("approve", "reject")


class PlanError(RuntimeError):
    """A plan that is not one, or that cannot be decided or applied as asked."""


def parse(text: str) -> list[dict[str, Any]]:
    """A run's plan.json, checked: its items, each with id, title, detail and data."""
    if len(text.encode()) > MAX_BYTES:
        raise PlanError(f"{PLAN_FILE} is over {MAX_BYTES // 1024} KB")
    try:
        found = json.loads(text)
    except ValueError as err:
        raise PlanError(f"{PLAN_FILE} is not JSON: {err}") from err
    items = found.get("items") if isinstance(found, dict) else None
    if not isinstance(items, list):
        raise PlanError(f"{PLAN_FILE} needs \"items\", a list")
    if len(items) > MAX_ITEMS:
        raise PlanError(f"{PLAN_FILE} has {len(items)} items; at most {MAX_ITEMS} (group them)")
    checked, seen = [], set()
    for n, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("title"), str):
            raise PlanError(f"{PLAN_FILE} item {n}: needs id and title, strings")
        if not ITEM_ID.fullmatch(item["id"]) or item["id"] in seen:
            raise PlanError(f"{PLAN_FILE} item {n}: id {item['id']!r} is not unique letters, digits and ._:@/+=-")
        if not item["title"].strip() or len(item["title"]) > TITLE_CHARS:
            raise PlanError(f"{PLAN_FILE} item {item['id']}: a title of 1 to {TITLE_CHARS} characters")
        detail = item.get("detail", "")
        if not isinstance(detail, str) or len(detail) > DETAIL_CHARS:
            raise PlanError(f"{PLAN_FILE} item {item['id']}: detail is a string of at most {DETAIL_CHARS} characters")
        seen.add(item["id"])
        checked.append({"id": item["id"], "title": item["title"].strip(), "detail": detail.strip(),
                        "data": item.get("data"), "decision": None})
    return checked


def plans_dir(cfg: HarnessConfig, name: str) -> Path:
    return cfg.resolve_state_dir(f"jobs/{name}/plans")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextmanager
def _locked(cfg: HarnessConfig, name: str) -> Iterator[Path]:
    """The job's plans, for one writer at a time: the orchestrator and the command line both decide."""
    directory = plans_dir(cfg, name)
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory


def _read(path: Path) -> dict[str, Any]:
    plan = json.loads(path.read_text())
    if plan.get("state") == "open" and time.time() > plan.get("expires_at", 0):
        plan["state"] = "expired"  # seen as it is; written when it next changes
    return plan


def _write(directory: Path, plan: dict[str, Any]) -> None:
    temporary = directory / f"{plan['run']}.json.new"
    temporary.write_text(json.dumps(plan, indent=1))
    temporary.replace(directory / f"{plan['run']}.json")


def store(cfg: HarnessConfig, name: str, run_id: str, items: list[dict[str, Any]], *, ttl: int = DEFAULT_TTL) -> dict[str, Any]:
    """Keep a run's plan, open for deciding, and supersede the job's other open plans."""
    with _locked(cfg, name) as directory:
        for path in directory.glob("*.json"):
            older = _read(path)
            if older["state"] in ("open", "expired") and older["run"] != run_id:
                _write(directory, {**older, "state": "superseded" if older["state"] == "open" else "expired",
                                   "closed": _now()})
        plan = {"run": run_id, "job": name, "state": "open", "created": _now(), "expires_at": time.time() + ttl,
                "expires": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + ttl)), "items": items}
        _write(directory, plan)
    return plan


def load(cfg: HarnessConfig, name: str, run_id: str = "") -> dict[str, Any]:
    """A plan, or with no run id the job's newest."""
    directory = plans_dir(cfg, name)
    if not run_id:
        found = list_plans(cfg, name, limit=1)
        if not found:
            raise PlanError(f"{name} has no plans.")
        return found[0]
    path = directory / f"{run_id}.json"
    if "/" in run_id or not path.is_file():
        raise PlanError(f"{name} has no plan from run {run_id}.")
    return _read(path)


def list_plans(cfg: HarnessConfig, name: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Newest first."""
    directory = plans_dir(cfg, name)
    paths = sorted(directory.glob("*.json"), reverse=True)[:limit] if directory.is_dir() else []
    found = []
    for path in paths:
        try:
            found.append(_read(path))
        except (OSError, ValueError):
            continue
    return found


def change(cfg: HarnessConfig, name: str, run_id: str, how: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Read a plan, let `how` change it (or raise PlanError), and write it back, under the lock."""
    with _locked(cfg, name) as directory:
        plan = load(cfg, name, run_id)
        how(plan)
        _write(directory, plan)
    return plan


def decide(cfg: HarnessConfig, name: str, run_id: str, decisions: dict[str, str], *, by: str) -> dict[str, Any]:
    """Approve or reject items of an open plan (ids to "approve" or "reject"; "*" for every item not decided)."""
    def how(plan: dict[str, Any]) -> None:
        if plan["state"] != "open":
            raise PlanError(f"The plan from run {plan['run']} is {plan['state']}: it can no longer be decided.")
        items = {item["id"]: item for item in plan["items"]}
        if unknown := sorted(set(decisions) - set(items) - {"*"}):
            raise PlanError(f"The plan from run {plan['run']} has no item {', '.join(unknown)}.")
        if bad := sorted(v for v in decisions.values() if v not in DECISIONS):
            raise PlanError(f"A decision is approve or reject (got {', '.join(bad)}).")
        for item in plan["items"]:
            decision = decisions.get(item["id"]) or (decisions.get("*") if item["decision"] is None else None)
            if decision:
                item.update(decision=decision, by=by, at=_now())
        if all(item["decision"] == "reject" for item in plan["items"]):
            plan.update(state="rejected", closed=_now())

    return change(cfg, name, run_id, how)


def start_applying(plan: dict[str, Any], run_id: str) -> None:
    """Mark a plan applying by that run, if it can be: open with every item decided (or already marked for it,
    by the orchestrator that started the run)."""
    if plan["state"] == "applying" and plan.get("apply_run") == run_id:
        return
    if plan["state"] != "open":
        raise PlanError(f"The plan from run {plan['run']} is {plan['state']}: there is nothing to apply.")
    if any(item["decision"] is None for item in plan["items"]):
        raise PlanError(f"The plan from run {plan['run']} has items not decided yet: approve or reject each "
                        "(or all of what is left) first.")
    plan.update(state="applying", apply_run=run_id)


def reopen(plan: dict[str, Any]) -> None:
    """An apply that never started: the plan can be applied again."""
    plan.update(state="open")
    plan.pop("apply_run", None)


def approved(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """What the apply script is given: the approved items' id, title and data."""
    return [{"id": i["id"], "title": i["title"], "data": i["data"]} for i in plan["items"] if i["decision"] == "approve"]
