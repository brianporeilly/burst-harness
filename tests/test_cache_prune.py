"""Pruning the build cache (harness/cache_prune.py), on a made-up cargo target directory: a crate built again
under another hash leaves its old build behind, which goes once it is old; a repo over its cap loses what was built
longest ago; a repo nothing was built in for a month goes whole; and a directory a build holds the lock on is left."""

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

from harness import cache_prune, cli
from harness.config import HarnessConfig

NOW = time.time()
DAY = 86400
H1, H2, H3 = "1" * 16, "2" * 16, "3" * 16


def _unit(profile: Path, name: str, hash_: str, age_days: float, size: int = 10) -> list[Path]:
    """A unit as cargo leaves it: its files in deps/, its .fingerprint/ entry, all built AGE_DAYS ago."""
    deps, fingerprint = profile / "deps", profile / ".fingerprint" / f"{name}-{hash_}"
    deps.mkdir(parents=True, exist_ok=True)
    fingerprint.mkdir(parents=True, exist_ok=True)
    paths = [deps / f"lib{name}-{hash_}.rlib", deps / f"{name}-{hash_}.d", fingerprint / "lib-x"]
    for path in paths:
        path.write_bytes(b"x" * size)
    for path in [*paths, fingerprint]:
        os.utime(path, (NOW - age_days * DAY,) * 2)
    return paths


def _repo(tmp_path: Path, key: str = "github.com_me_proj") -> Path:
    debug = tmp_path / "target" / key / "debug"
    debug.mkdir(parents=True)
    (debug / ".cargo-lock").touch()
    return debug


def test_an_old_build_of_a_crate_built_again_since_is_removed(tmp_path, capsys):
    debug = _repo(tmp_path)
    old_game = _unit(debug, "game", H1, 10, size=4 << 20)  # the workspace's test binary, from ten days ago
    new_game = _unit(debug, "game", H2, 0.1)
    young = _unit(debug, "game", H3, 1)  # superseded, but not old yet: kept for now
    syn = _unit(debug, "syn", H1, 20)  # old, but the only build of its crate: in use, kept
    cache_prune.main(str(tmp_path), "github.com_me_proj", 3, 30, now=NOW)
    assert not any(path.exists() for path in old_game) and not (debug / ".fingerprint" / f"game-{H1}").exists()
    assert all(path.exists() for path in new_game + young + syn)
    assert "Build cache: removed 0.0G of old build output from github.com_me_proj" in capsys.readouterr().out


def test_a_repo_over_its_cap_loses_what_was_built_longest_ago(tmp_path, monkeypatch):
    debug = _repo(tmp_path)
    oldest = _unit(debug, "bevy", H1, 2, size=1 << 20)
    older = _unit(debug, "wgpu", H1, 1, size=1 << 20)
    newest = _unit(debug, "game", H1, 0, size=1 << 20)
    monkeypatch.setattr(cache_prune, "GB", 1 << 20)  # 3 MiB a unit, and a cap of 7 "G" is 7 MiB here
    cache_prune.main(str(tmp_path), "github.com_me_proj", 3, 7, now=NOW)
    assert not oldest[0].exists() and older[0].exists() and newest[0].exists()


def test_a_repo_nothing_was_built_in_for_a_month_goes(tmp_path, capsys):
    _repo(tmp_path)
    stale = _repo(tmp_path, "github.com_me_old")
    _unit(stale, "game", H1, 40)
    for sub in ("deps", ".fingerprint"):
        os.utime(stale / sub, (NOW - 40 * DAY,) * 2)
    lively = _repo(tmp_path, "github.com_me_lively")
    _unit(lively, "game", H1, 40)
    _unit(lively, "game", H2, 2)  # built two days ago: its directories changed then
    cache_prune.main(str(tmp_path), "github.com_me_proj", 3, 30, now=NOW)
    assert not stale.parent.exists() and lively.exists()
    assert "removed github.com_me_old" in capsys.readouterr().out


def test_a_directory_being_built_is_left_alone(tmp_path, capsys):
    debug = _repo(tmp_path)
    old = _unit(debug, "game", H1, 10)
    _unit(debug, "game", H2, 0)
    fd = os.open(debug / ".cargo-lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # cargo, building in another sandbox
        # as the sandbox runs it: only the source, on stdin, nothing of the harness importable (-I)
        out = subprocess.run([sys.executable, "-I", "-", str(tmp_path), "github.com_me_proj", "3", "30"], text=True,
                             input=Path(cache_prune.__file__).read_text(), capture_output=True, check=True).stdout
    finally:
        os.close(fd)
    assert all(path.exists() for path in old) and "being built in another sandbox" in out


def test_each_burst_prunes_unless_turned_off():
    from dataclasses import replace

    from harness.exec_target import CommandResult

    class Target:
        def __init__(self):
            self.ran = []

        def run_shell(self, command, **kwargs):
            self.ran.append((command, kwargs.get("stdin")))
            return CommandResult(0, "Build cache: removed 9.0G of old build output from k" if kwargs.get("stdin")
                                 else "", "")

    cfg = HarnessConfig.load(None, {})
    target = Target()
    assert cli._build_cache(target, "/cache", "k", cfg)[1] == ("CARGO_TARGET_DIR", "/cache/target/k")
    (command, script), = target.ran[1:]
    assert command == "command -v python3 >/dev/null || exit 0; python3 - /cache k 3 30"
    assert script == Path(cache_prune.__file__).read_text()

    target = Target()
    cli._build_cache(target, "/cache", "k", replace(cfg, build_cache_days=0))
    assert len(target.ran) == 1  # the directories made, nothing pruned
