"""What a sandbox may use, for the agent's instructions: its memory limit, and its CPUs.

A sandbox's processes share a memory limit (a pod's, a container's), and one that goes over it is killed, with
what was running beside it: a build that runs as many jobs as the machine has CPUs can get there on its own. The
agent is told the limit, so it can keep its builds and tests within it (fewer parallel jobs, one build at a
time) rather than learning it by being killed.

The limit is read inside the sandbox, from its cgroup (v2's memory.max and cpu.max, or v1's), which is what the
kernel holds it to whatever kind of target it is. A sandbox with no limit of its own (a plain host, this
machine) gets no note. It is read once per sandbox, for the lead, its delegates and a job alike.
"""

from __future__ import annotations

from typing import Any

from .exec_target import LocalTarget

# memory|cpu.max (v2: "QUOTA PERIOD" or "max PERIOD")|v1 quota|v1 period|the machine's CPUs
PROBE = (
    "m=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null); "
    "c=$(cat /sys/fs/cgroup/cpu.max 2>/dev/null); "
    "q=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us 2>/dev/null); p=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us 2>/dev/null); "
    "t=$(awk '/^MemTotal:/ {print $2 * 1024}' /proc/meminfo 2>/dev/null); n=$(nproc 2>/dev/null); "
    'echo "$m|$c|$q|$p|$t|$n"'
)
NOTE = ("Resources: this sandbox may use at most {memory} of memory{cpus}. A process that goes over the memory is "
        "killed, and the sandbox's other processes may go with it, so keep what runs at once within that: give "
        "builds and test runners fewer parallel jobs (CARGO_BUILD_JOBS, make -j, a test runner's workers) when they "
        "use a lot of memory, and run one heavy build at a time.")
# Read once per sandbox: the lead's prompt, its delegates', a job's.
_seen: dict[str, str] = {}


def note(target: Any) -> str:
    """The line for the agent's instructions, or "" when the sandbox has no memory limit of its own (or it
    could not be read)."""
    if isinstance(target, LocalTarget) or not hasattr(target, "run_shell"):
        return ""
    where = target.describe()
    if where not in _seen:
        try:
            found = target.run_shell(PROBE, check=False, cwd="/")
            memory, cpus = limits(found.stdout.strip()) if found.returncode == 0 else ("", "")
        except Exception:  # noqa: BLE001 -- a note the agent goes without, never a failed run
            memory, cpus = "", ""
        _seen[where] = NOTE.format(memory=memory, cpus=f" and {cpus}" if cpus else "") if memory else ""
        if memory:
            print(f"The agent is told the sandbox's limits: {memory} of memory" + (f", {cpus}." if cpus else "."))
    return _seen[where]


CACHE_NOTE = """Builds here keep their output between bursts: {settings} {are} set for you, on a volume \
that outlives this sandbox. Don't set {them} to anything else, in a command or in the task document: a build \
anywhere else starts from nothing in every burst, and a large one takes many minutes each time."""

# The settings build_cache sets (cli._build_cache), in the order they are named.
CACHED = ("CARGO_HOME", "CARGO_TARGET_DIR")


def cache_note(target: Any) -> str:
    """The line for the agent's instructions when its builds are cached (build_cache), or ""."""
    env = dict(getattr(target, "env", ()) or ())
    found = [f"{name}={env[name]}" for name in CACHED if env.get(name)]
    if not found:
        return ""
    return CACHE_NOTE.format(settings=" and ".join(found), are="are" if len(found) > 1 else "is",
                             them="them" if len(found) > 1 else "it")


def limits(probe: str) -> tuple[str, str]:
    """From PROBE's output: the memory limit ("8Gi"), "" for none of its own; and its CPUs ("6 CPUs"), or ""."""
    parts = (probe.split("|") + [""] * 6)[:6]
    memory, cpu_max, quota, period, total, cpus = (part.strip() for part in parts)
    if not memory.isdigit():
        return "", ""  # "max", or nothing: no limit of its own
    limit = int(memory)
    if total.isdigit() and limit >= int(total):
        return "", ""  # v1's "unlimited" is a huge number, and a limit above the machine is none
    count = _cpus(cpu_max, quota, period) or (int(cpus) if cpus.isdigit() else 0)
    return size(limit), f"{_number(count)} CPU{'s' if count != 1 else ''}" if count else ""


def _cpus(cpu_max: str, quota: str, period: str) -> float:
    """The CPUs a cgroup's quota allows, or 0 for none."""
    if cpu_max:
        quota, _, period = cpu_max.partition(" ")
    try:
        allowed, every = int(quota), int(period)
    except ValueError:
        return 0
    return allowed / every if allowed > 0 and every > 0 else 0


def _number(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:.1f}"


def size(count: int) -> str:
    for unit, scale in (("Gi", 2**30), ("Mi", 2**20)):
        if count >= scale:
            value = count / scale
            return f"{value:.0f}{unit}" if value == int(value) or value >= 10 else f"{value:.1f}{unit}"
    return f"{count} bytes"
