"""Keeping the build cache (build_cache) from filling its volume: run at the start of each burst, in the sandbox,
by its python3, this file's source sent on stdin (cli._build_cache). So it imports nothing of the harness.

  python3 - ROOT KEY DAYS MAX_GB

What fills it is cargo's output in ROOT/target/<repo>: each time a crate is built with something changed (its
features, its dependencies, the workspace's code for its tests and binaries) cargo writes the crate again under a
new hash in `deps/`, and never removes the old one. A game's test binaries are a gigabyte each. So, for KEY's
target directory:

- a unit (a crate's files in `deps/` and its `.fingerprint/` entry, sharing a name and a hash) is removed when
  it was last built more than DAYS days ago and the same crate has been built since under another hash;
- then, if the directory is still over MAX_GB, the units built longest ago are removed until it is under;
- and any other repo's target directory nothing has been built in for STALE_DAYS days is removed whole.

A unit removed that a build still wanted is built again by cargo (its output is missing, so it is out of date):
pruning costs time, never a broken build. Cargo locks a target directory while it builds, so a directory is
pruned only with its locks taken (without waiting: one a build holds is left for the next burst), and a build
that starts meanwhile waits for them."""

import fcntl
import os
import re
import shutil
import sys
import time

# The files cargo locks a profile's directory with (`debug/`, `wasm32-unknown-unknown/release/`...).
LOCKS = (".cargo-lock", ".cargo-build-lock", ".cargo-artifact-lock")
# A unit's file or directory: libNAME-HASH.rlib, NAME-HASH.d, NAME-HASH (a test or a binary), .fingerprint/NAME-HASH.
UNIT = re.compile(r"^(?:lib)?(?P<name>.+)-(?P<hash>[0-9a-f]{16})(?:\.[^-]*)?$")
# A profile's directories that are cargo's output, not more profiles: not looked into for locks or profiles.
OUTPUT = {"deps", "build", ".fingerprint", "incremental", "examples"}
STALE_DAYS = 30
DAY = 86400
GB = 1 << 30


def profiles(top):
    """The profile directories under a target directory: those holding a `.fingerprint`."""
    found = []
    for path, dirs, _ in os.walk(top):
        if ".fingerprint" in dirs:
            found.append(path)
        dirs[:] = [name for name in dirs if name not in OUTPUT]
    return found


def lock(top):
    """Every cargo lock under TOP, taken without waiting: the open files, or None when a build holds one."""
    held = []
    for path, dirs, files in os.walk(top):
        dirs[:] = [name for name in dirs if name not in OUTPUT]
        for name in files:
            if name not in LOCKS:
                continue
            fd = os.open(os.path.join(path, name), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                unlock(held)
                return None
            held.append(fd)
    return held


def unlock(held):
    for fd in held:
        os.close(fd)


def size(path):
    """Bytes on disk under PATH, a file hard-linked twice (cargo links a profile's outputs from `deps/`) once."""
    seen, total = set(), 0
    for where, _, files in os.walk(path):
        for name in files:
            try:
                st = os.lstat(os.path.join(where, name))
            except OSError:
                continue
            if (st.st_dev, st.st_ino) not in seen:
                seen.add((st.st_dev, st.st_ino))
                total += st.st_blocks * 512
    return total


def on_disk(path):
    try:
        return os.lstat(path).st_blocks * 512
    except OSError:
        return 0


def units(profile):
    """A profile's units: {(name, hash): (when last built, [paths])}."""
    found = {}
    for sub in ("deps", ".fingerprint"):
        try:
            entries = list(os.scandir(os.path.join(profile, sub)))
        except OSError:
            continue
        for entry in entries:
            if not (match := UNIT.match(entry.name)):
                continue
            try:
                built = entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            when, paths = found.get((match["name"], match["hash"]), (0.0, []))
            found[(match["name"], match["hash"])] = (max(when, built), paths + [entry.path])
    return found


def remove(paths):
    for path in paths:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.unlink(path)
            except OSError:
                pass


def superseded(found, now, days):
    """The units built more than DAYS ago whose crate has been built since under another hash."""
    newest = {}
    for (name, _), (when, _) in found.items():
        newest[name] = max(newest.get(name, 0.0), when)
    return [key for key, (when, _) in found.items() if when < now - days * DAY and when < newest[key[0]]]


def prune(top, now, days, max_bytes):
    """Prune one repo's target directory (its locks held): the bytes it had, and has."""
    before, left = size(top), []
    for profile in profiles(top):
        its = units(profile)
        for key in superseded(its, now, days):
            remove(its.pop(key)[1])
        left += its.values()
    after = size(top)
    for _, paths in sorted(left, key=lambda unit: unit[0]):  # over the cap: those built longest ago first
        if after <= max_bytes:
            break
        after -= sum(size(path) if os.path.isdir(path) else on_disk(path) for path in paths)
        remove(paths)
    return before, size(top)


def newest_build(top):
    """When anything was last built in a target directory: its profiles' `deps` and `.fingerprint` change then."""
    when = 0.0
    for profile in profiles(top):
        for sub in ("deps", ".fingerprint"):
            try:
                when = max(when, os.stat(os.path.join(profile, sub)).st_mtime)
            except OSError:
                pass
    return when or os.stat(top).st_mtime


def main(root, key, days, max_gb, now=None):
    now = time.time() if now is None else now
    targets = os.path.join(root, "target")
    said = []
    mine = os.path.join(targets, key)
    if os.path.isdir(mine):
        if (held := lock(mine)) is None:
            said.append(f"{key} is being built in another sandbox: not pruned this time.")
        else:
            try:
                before, after = prune(mine, now, days, max_gb * GB)
            finally:
                unlock(held)
            if after < before:
                said.append(f"removed {(before - after) / GB:.1f}G of old build output from {key}, "
                            f"which has {after / GB:.1f}G (at most {max_gb}G).")
    for other in sorted(os.listdir(targets)) if os.path.isdir(targets) else ():
        top = os.path.join(targets, other)
        if other == key or not os.path.isdir(top) or newest_build(top) > now - STALE_DAYS * DAY:
            continue
        if (held := lock(top)) is None:
            continue
        try:
            gone = size(top)
            shutil.rmtree(top, ignore_errors=True)
        finally:
            unlock(held)
        said.append(f"removed {other} ({gone / GB:.1f}G), where nothing was built in {STALE_DAYS} days.")
    for line in said:
        print(f"Build cache: {line}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
