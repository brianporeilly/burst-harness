"""A job's memory: files one run leaves for the next (`[job] memory = true`, job.py).

The memory is a directory on the driver, under the state directory: jobs/NAME/memory. As a run starts it is
copied into the sandbox, at /workspace/memory (empty the first time); what the run leaves there is copied back
when it ends, and becomes the memory the next run starts with. What to keep, and how, is the job's own: a
script's JSON of what it has seen, the model's notes in Markdown.

It is kept only from a run that reported: a run that failed half way could leave it half written, so the next
run starts from the last good one. It is kept only up to LIMIT bytes, of plain files and directories (no
links); a run that leaves more keeps nothing, and its report says so.

Each memory kept is also copied to the run's record (runs/RUN-ID/memory.tar.gz), for the last KEEP runs that
kept one: what a run changed can be looked at, and `harness job memory DIR --restore RUN-ID` goes back to it.

A run that reads untrusted data (mail, issues) writes its memory having read it, so the next run is told the
memory is its own earlier notes, which do not override its rules.
"""

from __future__ import annotations

import base64
import io
import os
import shlex
import shutil
import tarfile
import tempfile
from pathlib import Path

from .config import HarnessConfig
from .exec_target import SubprocessTarget

DIR = "memory"  # in the run's working directory, and under the job's state
LIMIT = 5 * 1024 * 1024
KEEP = 20
SNAPSHOT = "memory.tar.gz"  # in a run's record


class JobMemoryError(RuntimeError):
    """Memory that cannot be kept or restored as asked."""


def directory(cfg: HarnessConfig, name: str) -> Path:
    return cfg.resolve_state_dir(f"jobs/{name}/{DIR}")


def summary(path: Path) -> dict[str, int]:
    files = [p for p in path.rglob("*") if p.is_file()] if path.is_dir() else []
    return {"files": len(files), "bytes": sum(p.stat().st_size for p in files)}


def put(target: SubprocessTarget, source: Path) -> dict[str, int]:
    """Copy the memory into the sandbox, as its own user, through stdin. Returns what was copied."""
    dest = shlex.quote(f"{target.repo}/{DIR}")
    if not source.is_dir():
        target.run_shell(f"rm -rf {dest} && mkdir -p {dest}", cwd="/")
        return {"files": 0, "bytes": 0}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        tar.add(source, arcname=".")
    target.run_shell(f"rm -rf {dest} && mkdir -p {dest} && base64 -d | tar -xf - -C {dest}",
                     stdin=base64.b64encode(buffer.getvalue()).decode(), cwd="/")
    return summary(source)


def take(target: SubprocessTarget, dest: Path, snapshot_dir: Path) -> dict[str, int]:
    """Copy what the run left in the sandbox's memory back to `dest`, replacing it, and a copy of it to the
    run's record (`snapshot_dir`). Raises JobMemoryError, leaving `dest` as it was, if it cannot be kept."""
    source = shlex.quote(f"{target.repo}/{DIR}")
    # Read no more than a memory over the limit could be: its tar is the files and a header for each.
    cap = 2 * LIMIT + 1024 * 1024
    found = target.run_shell(f"test -d {source} || exit 3; cd {source} && tar -cf - . | head -c {cap} | base64",
                             check=False, cwd="/")
    if found.returncode == 3:
        raise JobMemoryError(f"the run left no {DIR} directory")
    if found.returncode != 0:
        raise JobMemoryError(f"it could not be read from the sandbox: {found.stderr.strip()[:200]}")
    data = base64.b64decode(found.stdout)
    if len(data) >= cap:
        raise JobMemoryError(f"it is over {LIMIT // (1024 * 1024)} MB")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fresh = Path(tempfile.mkdtemp(prefix=f".{DIR}-", dir=dest.parent))
    try:
        _extract(data, fresh)
        size = summary(fresh)
        if size["bytes"] > LIMIT:
            raise JobMemoryError(f"it is {size['bytes'] // 1024} KB, over {LIMIT // (1024 * 1024)} MB")
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(snapshot_dir / SNAPSHOT, mode="w:gz") as tar:
            tar.add(fresh, arcname=".")
        _replace(dest, fresh)
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
    return size


def _extract(data: bytes, into: Path) -> None:
    """Plain files and directories only, inside `into`: what came out of a sandbox is not trusted."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            members = [m for m in tar.getmembers() if m.isfile() or m.isdir()]
            tar.extractall(into, members=members, filter="data")
    except (tarfile.TarError, OSError) as err:
        raise JobMemoryError(f"it could not be unpacked: {err}") from err


def _replace(dest: Path, fresh: Path) -> None:
    """Put `fresh`'s contents at `dest`, emptying `fresh`; the old memory is gone only once the new is in."""
    old = dest.with_name(f".{dest.name}-old")
    shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        dest.rename(old)
    fresh.rename(dest)
    os.chmod(dest, 0o700)
    shutil.rmtree(old, ignore_errors=True)


def prune(runs: Path) -> None:
    """Keep the snapshots of the last KEEP runs that have one."""
    found = sorted(runs.glob(f"*/{SNAPSHOT}"), key=lambda p: p.parent.name, reverse=True) if runs.is_dir() else []
    for old in found[KEEP:]:
        old.unlink(missing_ok=True)


def restore(cfg: HarnessConfig, name: str, runs: Path, run_id: str) -> dict[str, int]:
    """Make a run's snapshot the job's memory again."""
    snapshot = runs / run_id / SNAPSHOT
    if "/" in run_id or not snapshot.is_file():
        raise JobMemoryError(f"Run {run_id} of {name} has no memory kept (only the last {KEEP} runs that kept one do).")
    dest = directory(cfg, name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fresh = Path(tempfile.mkdtemp(prefix=f".{DIR}-", dir=dest.parent))
    try:
        _extract(snapshot.read_bytes(), fresh)
        _replace(dest, fresh)
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
    return summary(dest)


def clear(cfg: HarnessConfig, name: str) -> None:
    shutil.rmtree(directory(cfg, name), ignore_errors=True)
