"""Job sources: git repos of jobs the orchestrator keeps a copy of, beside its job directories (--jobs).

A source is a repo URL, a branch (its default branch if none is given), a folder in it (the repo's top if
none), and the credential it is read with: JOBS_GIT_TOKEN unless it names another, a token that can read the
repo and nothing more (for GitHub, a fine-grained token with read access to that repo's contents). A public
repo needs none: with the credential unset, the repo is read without one.

Sources come from the configuration, ORCHESTRATOR_JOB_REPOS: entries separated by spaces or new lines, each

    [NAME=]URL[#BRANCH[:FOLDER]]       https://github.com/you/jobs.git#main:jobs

(NAME is the repo's name unless given), and from the Settings tab, kept in job-sources.json in the
orchestrator's own state directory, so a private repo's address need not be in a deployment's manifests.

Each source is fetched (--depth 1) into a bare repo of its own under the state directory, every `interval`
seconds and whenever it is synced from the page. A new commit is unpacked into a folder named for it, and a
`current` link is moved to that folder in one step, so a job is never read half updated, and a run copies its
job from one commit (its record says which). The folder before is kept until the next commit comes; older
ones are removed. Nothing is ever written back to the repo.

A job's name is the name of its folder, here as in --jobs, and must be unique: the job directories come first,
then the sources in order, and a job of a name already found is left out, which its source's row says.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from harness import credentials

ENV = "ORCHESTRATOR_JOB_REPOS"
TOKEN = "JOBS_GIT_TOKEN"
INTERVAL = 300
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
CREDENTIAL = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
BRANCH = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]{0,199}")
SHA = re.compile(r"[0-9a-f]{40}")
URL_CHARS = 500
GIT_TIMEOUT = 120
MAX_TREE = 50 * 1024 * 1024  # what a commit may unpack to: a repo of jobs, not of anything else


class SourcesError(ValueError):
    """A source that cannot be saved, removed or synced as asked."""


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    branch: str = ""      # "" for the repo's default branch
    path: str = ""        # the folder in the repo the jobs are in; "" for its top
    token: str = TOKEN    # the credential it is read with
    origin: str = "configuration"  # or "settings"


def check(source: Source) -> Source:
    """The source, or SourcesError saying what is wrong with it."""
    if not NAME.fullmatch(source.name):
        raise SourcesError(f"{source.name!r}: a source's name is lowercase letters, digits and dashes, at most 40.")
    url = source.url.strip()
    if len(url) > URL_CHARS or not (url.startswith(("https://", "file://")) or url.startswith("/")):
        raise SourcesError(f"{source.name}: the URL is an https:// address of a git repo (or a local path), "
                           f"at most {URL_CHARS} characters.")
    if "@" in url.split("://", 1)[-1].split("/", 1)[0]:
        raise SourcesError(f"{source.name}: the URL has no user or password in it: the token goes in "
                           f"the Credentials, as {source.token}.")
    if source.branch and (not BRANCH.fullmatch(source.branch) or ".." in source.branch):
        raise SourcesError(f"{source.name}: {source.branch!r} is not a branch name.")
    folder = PurePosixPath(source.path.strip("/")) if source.path.strip("/") else None
    if folder is not None and (folder.is_absolute() or ".." in folder.parts or folder.parts[0].startswith("-")):
        raise SourcesError(f"{source.name}: the folder is a path inside the repo, such as jobs.")
    if not CREDENTIAL.fullmatch(source.token):
        raise SourcesError(f"{source.name}: the credential is a name such as {TOKEN}.")
    return Source(source.name, url, source.branch, str(folder or ""), source.token, source.origin)


def parse(value: str) -> list[Source]:
    """ORCHESTRATOR_JOB_REPOS's entries."""
    found: dict[str, Source] = {}
    for entry in value.split():
        name, _, rest = entry.partition("=") if re.match(r"[a-z0-9-]+=", entry) else ("", "", entry)
        url, _, ref = rest.partition("#")
        branch, _, folder = ref.partition(":")
        name = name or _repo_name(url)
        source = check(Source(name, url, branch, folder))
        if name in found:
            raise SourcesError(f"{ENV}: two sources are named {name}; give one a name of its own (NAME=URL).")
        found[name] = source
    return list(found.values())


def _repo_name(url: str) -> str:
    last = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower()
    return re.sub(r"[^a-z0-9-]+", "-", last).strip("-")[:40] or "jobs"


class JobSources:
    def __init__(self, root: Path, store: Path, configured: list[Source] | None = None, *,
                 interval: int = INTERVAL) -> None:
        self.root = root            # where the copies are kept: root/NAME/{repo.git,trees/SHA,current}
        self.store = store          # job-sources.json, for those saved in Settings
        self.configured = {s.name: s for s in configured or []}
        self.interval = interval
        self.lock = threading.Lock()       # the store
        self.syncing = threading.Lock()    # one sync at a time
        self.status: dict[str, dict[str, Any]] = {}  # name -> {synced, error, checked}, since this start

    # --- what is configured and saved ---

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.store.read_text())
        except FileNotFoundError:
            return {}
        sources = data.get("sources") if isinstance(data, dict) else None
        return sources if isinstance(sources, dict) else {}

    def _save(self, sources: dict[str, dict[str, Any]]) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        part = self.store.with_suffix(".part")
        part.write_text(json.dumps({"sources": sources}, indent=2) + "\n")
        part.replace(self.store)

    def sources(self) -> dict[str, Source]:
        """Every source, the configuration's first, in order."""
        with self.lock:
            stored = self._load()
        found = dict(self.configured)
        for name, record in sorted(stored.items()):
            if name in found:
                continue  # set() refuses this; a configuration that gained the name since wins
            try:
                found[name] = check(Source(name, str(record.get("url", "")), str(record.get("branch", "")),
                                           str(record.get("path", "")), str(record.get("token") or TOKEN), "settings"))
            except SourcesError as err:
                self.status.setdefault(name, {})["error"] = str(err)
        return found

    def set(self, name: str, raw: dict[str, Any], *, user: str = "") -> Source:
        """Save a source of the Settings tab's (url, and branch, path, token if not the defaults)."""
        if name in self.configured:
            raise SourcesError(f"{name} is the configuration's ({ENV}): change it there, or save this one "
                               "under another name.")
        with self.lock:
            stored = self._load()
            record = dict(stored.get(name, {}))
            for key in ("url", "branch", "path", "token"):
                if key in raw:
                    value = raw[key]
                    if value is not None and not isinstance(value, str):
                        raise SourcesError(f"{key} is text.")
                    record[key] = (value or "").strip()
            if not record.get("url"):
                raise SourcesError(f"{name} needs the URL of its repo.")
            source = check(Source(name, record["url"], record.get("branch", ""), record.get("path", ""),
                                  record.get("token") or TOKEN, "settings"))
            changed = {k: getattr(source, k) for k in ("url", "branch", "path")} != \
                {k: (stored.get(name) or {}).get(k, "") for k in ("url", "branch", "path")}
            stored[name] = {"url": source.url, "branch": source.branch, "path": source.path, "token": source.token,
                            "updated": _now(), "updated_by": user}
            self._save(stored)
        if changed:  # another repo or branch: what was copied of the old one is not this source's
            shutil.rmtree(self.root / name, ignore_errors=True)
            self.status.pop(name, None)
        return source

    def remove(self, name: str) -> None:
        if name in self.configured:
            raise SourcesError(f"{name} is the configuration's ({ENV}): remove it there.")
        with self.lock:
            stored = self._load()
            if name not in stored:
                raise SourcesError(f"No source named {name} is saved here.")
            del stored[name]
            self._save(stored)
        with self.syncing:
            shutil.rmtree(self.root / name, ignore_errors=True)
        self.status.pop(name, None)

    # --- the copies ---

    def commit(self, name: str) -> str:
        """The commit a source's jobs are read from now, or ""."""
        try:
            sha = os.readlink(self.root / name / "current").rsplit("/", 1)[-1]
        except OSError:
            return ""
        return sha if SHA.fullmatch(sha) else ""

    def directories(self) -> list[tuple[Source, Path]]:
        """Each source that has been copied, and the folder its jobs are in (at its current commit)."""
        found = []
        for source in self.sources().values():
            if sha := self.commit(source.name):
                found.append((source, self.root / source.name / "trees" / sha / source.path))
        return found

    def origin(self, path: Path) -> tuple[str, str] | None:
        """(source, commit) of a job's directory, if it is one of a source's."""
        try:
            parts = path.resolve().relative_to(self.root.resolve()).parts
        except ValueError:
            return None
        return (parts[0], parts[2]) if len(parts) > 3 and parts[1] == "trees" and SHA.fullmatch(parts[2]) else None

    def sync(self, name: str | None = None) -> dict[str, dict[str, Any]]:
        """Fetch one source, or all; the status of each synced."""
        sources = self.sources()
        if name is not None and name not in sources:
            raise SourcesError(f"No source named {name}.")
        done = {}
        with self.syncing:
            for source in [sources[name]] if name else sources.values():
                status = {"checked": _now()}
                try:
                    before = self.commit(source.name)
                    sha = self._fetch(source)
                    status.update(synced=status["checked"], error="")
                    if sha != before:
                        print(f"sources: {source.name} at {sha[:12]}" + (f" (was {before[:12]})" if before else ""),
                              file=sys.stderr, flush=True)
                except SourcesError as err:
                    status["error"] = str(err)
                    print(f"sources: {source.name}: {err}", file=sys.stderr, flush=True)
                self.status[source.name] = {**self.status.get(source.name, {}), **status}
                done[source.name] = self.status[source.name]
        return done

    def _fetch(self, source: Source) -> str:
        """Fetch the source's branch, and unpack a new commit; the commit."""
        home = self.root / source.name
        repo = home / "repo.git"
        if not (repo / "HEAD").is_file():
            home.mkdir(parents=True, exist_ok=True)
            self._git(source, "init", "--quiet", "--bare", str(repo))
        branch = source.branch or self._default_branch(source)
        self._git(source, "--git-dir", str(repo), "fetch", "--quiet", "--depth", "1", "--no-tags", source.url,
                  f"+refs/heads/{branch}:refs/source/{branch}")
        sha = self._git(source, "--git-dir", str(repo), "rev-parse", f"refs/source/{branch}^{{commit}}").strip()
        if not SHA.fullmatch(sha):
            raise SourcesError(f"git gave {sha!r} for {branch}.")
        before = self.commit(source.name)
        if sha == before:
            return sha
        trees = home / "trees"
        tree = trees / sha
        if not tree.is_dir():
            archive = self._git(source, "--git-dir", str(repo), "archive", "--format=tar", sha, text=False)
            if len(archive) > MAX_TREE:
                raise SourcesError(f"commit {sha[:12]} is over {MAX_TREE // (1024 * 1024)} MB: not a repo of jobs.")
            part = trees / f"{sha}.part"
            shutil.rmtree(part, ignore_errors=True)
            part.mkdir(parents=True)
            try:
                with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
                    tar.extractall(part, filter="data")  # no links out, no devices, no absolute paths
            except (tarfile.TarError, OSError) as err:
                shutil.rmtree(part, ignore_errors=True)
                raise SourcesError(f"commit {sha[:12]} could not be unpacked: {err}") from err
            part.replace(tree)
        if source.path and not (tree / source.path).is_dir():
            raise SourcesError(f"{source.path} is not a folder in {branch} at {sha[:12]}.")
        link = home / "current.new"
        link.unlink(missing_ok=True)
        link.symlink_to(f"trees/{sha}")
        link.replace(home / "current")  # in one step: a job is read from one commit or the other
        for old in trees.iterdir():  # keep this commit's and the one before, which a run may be copying
            if old.name not in (sha, before):
                shutil.rmtree(old, ignore_errors=True)
        self._git(source, "--git-dir", str(repo), "gc", "--quiet", "--prune=now")
        return sha

    def _default_branch(self, source: Source) -> str:
        out = self._git(source, "ls-remote", "--symref", source.url, "HEAD")
        if found := re.search(r"^ref: refs/heads/(\S+)\s+HEAD$", out, re.MULTILINE):
            return found.group(1)
        raise SourcesError("the repo has no default branch (is it empty?): give a branch.")

    def _git(self, source: Source, *args: str, text: bool = True) -> Any:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
               "GIT_CONFIG_NOSYSTEM": "1"}
        secret = credentials.get(source.token) if source.url.startswith("https://") else ""
        if secret:  # in the environment, not the arguments: not in a process listing, and never in the repo
            basic = base64.b64encode(f"x-access-token:{secret}".encode()).decode()
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                       GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
        try:
            done = subprocess.run(["git", "-c", "credential.helper=", "-c", "protocol.allow=never",
                                   "-c", "protocol.https.allow=always", "-c", "protocol.file.allow=always", *args],
                                  capture_output=True, env=env, timeout=GIT_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise SourcesError(f"git {args[0]}: {err}") from err
        if done.returncode != 0:
            message = done.stderr.decode(errors="replace").strip().splitlines()
            reason = (message[-1] if message else f"git exited with {done.returncode}")
            if secret:
                reason = reason.replace(secret, "***")
            hint = (f" (is {source.token} set, and can it read the repo?)"
                    if re.search(r"authentication|could not read|not found|403|401", reason, re.I) else "")
            raise SourcesError(f"git {args[0] if args[0] != '--git-dir' else args[2]}: {reason}{hint}")
        return done.stdout.decode(errors="replace") if text else done.stdout

    def start(self) -> None:
        """Sync every source now, and then every `interval` seconds, in a thread of its own."""
        def loop() -> None:
            while True:
                try:
                    self.sync()
                except Exception as err:  # noqa: BLE001 -- the next round tries again
                    print(f"sources: {type(err).__name__}: {err}", file=sys.stderr, flush=True)
                time.sleep(self.interval)

        threading.Thread(target=loop, name="job-sources", daemon=True).start()

    # --- for the page ---

    def listing(self, clashes: dict[str, list[str]] | None = None, found: dict[str, Path] | None = None) -> dict[str, Any]:
        """The sources, each with where it stands; `found`, the jobs as the runner found them, and `clashes`,
        the job names it left out of some source, to say which of a source's jobs are its."""
        with self.lock:
            stored = self._load()
        rows = []
        for name, source in self.sources().items():
            sha = self.commit(name)
            tree = self.root / name / "trees" / sha / source.path if sha else None
            jobs = sorted(p.parent.name for p in tree.glob("*/job.toml")) if tree and tree.is_dir() else []
            if tree and (tree / "job.toml").is_file():
                jobs = [tree.name]
            mine = [j for j in jobs if found is None or (found.get(j) and self.origin(found[j]) == (name, sha))]
            status = self.status.get(name, {})
            record = stored.get(name, {})
            rows.append({**{k: v for k, v in asdict(source).items()}, "commit": sha,
                         "token_set": bool(credentials.get(source.token)),
                         "synced": status.get("synced"), "checked": status.get("checked"),
                         "error": status.get("error", ""), "jobs": mine,
                         "left_out": [j for j in jobs if j not in mine and j in (clashes or {})],
                         "updated": record.get("updated"), "updated_by": record.get("updated_by", "")})
        return {"sources": rows, "interval": self.interval}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
