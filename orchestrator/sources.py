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

Review: whoever can push to the repo could otherwise give a job any secret, write rule or network, so a commit
that changes a job's access waits for a person to Accept it (`accept`, the Jobs tab), and until then the job
runs as it was last accepted. A job's access is everything in its job.toml but its schedule (`[job] trigger`),
`memory`, `quiet` and `plan_ttl`, with the contents of the files it names there (a secret's ca_file,
claude_json, claude_settings). Its prompt and scripts are not: a commit changing only those goes straight
through. A job new to the source waits for Accept too, and one gone from it is gone. A source's jobs are read
from SOURCE/jobs/JOB, a link to the job in the tree of the commit it was accepted at; that tree is kept while a
job is held at it. What was accepted, and what waits, is in SOURCE/review.json. When the review first meets a
source that was already synced (an orchestrator from before it), the jobs as they are then are taken as
accepted, since they were trusted until then.

Whatever wants to hear about the review (the Slack bot, which asks the listed users) watches it (`watch`):
told when a job starts to wait ("waiting"), waits at a newer commit with the same access ("moved"), is
accepted ("accepted"), or no longer waits because its access went back to what was accepted or it left the
repo ("withdrawn"). Where it was said (`notices`, a Slack message's place) is kept with what waits.
"""

from __future__ import annotations

import base64
import hashlib
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
import tomllib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from collections.abc import Callable
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
REVIEW_FILE = "review.json"
# [job] settings that are not a job's access: a commit changing only these (and the prompt, the scripts) needs
# no Accept.
NOT_ACCESS = ("trigger", "memory", "quiet", "plan_ttl")
# Settings naming a file in the job whose contents are part of its access.
FILE_SETTINGS = ("claude_json", "claude_settings")


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
        self.watchers: list[Callable[[str, str, str, dict[str, Any]], None]] = []
        with self.syncing:  # sources synced before the review: their jobs as they are now, accepted
            for source in self.sources().values():
                if (sha := self.commit(source.name)) and not (self.root / source.name / REVIEW_FILE).is_file():
                    self._review(source, sha, baseline=True)

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
        """Each source that has been copied, and the folder its jobs are read from: links to each job as it was
        last accepted."""
        found = []
        for source in self.sources().values():
            if (jobs := self.root / source.name / "jobs").is_dir():
                found.append((source, jobs))
        return found

    def origin(self, path: Path) -> tuple[str, str] | None:
        """(source, commit) of a job's directory, if it is one of a source's."""
        try:
            parts = path.resolve().relative_to(self.root.resolve()).parts
        except ValueError:
            return None
        return (parts[0], parts[2]) if len(parts) > 3 and parts[1] == "trees" and SHA.fullmatch(parts[2]) else None

    # --- review: what changes a job's access waits for Accept ---

    def _tree_jobs(self, source: Source, sha: str) -> dict[str, Path]:
        """The jobs in a commit's tree: name -> folder."""
        tree = self.root / source.name / "trees" / sha / source.path
        if (tree / "job.toml").is_file():
            return {tree.name: tree}
        return {p.parent.name: p.parent for p in sorted(tree.glob("*/job.toml"))} if tree.is_dir() else {}

    def review(self, name: str) -> dict[str, Any]:
        """A source's review.json: {"seen": commit, "accepted": {job: ...}, "pending": {job: ...}}."""
        try:
            data = json.loads((self.root / name / REVIEW_FILE).read_text())
        except (OSError, ValueError):
            return {"accepted": {}, "pending": {}}
        return {"accepted": {}, "pending": {}, **data} if isinstance(data, dict) else {"accepted": {}, "pending": {}}

    def _save_review(self, name: str, data: dict[str, Any]) -> None:
        path = self.root / name / REVIEW_FILE
        part = path.with_suffix(".part")
        part.write_text(json.dumps(data, indent=1) + "\n")
        part.replace(path)

    def watch(self, watcher: Callable[[str, str, str, dict[str, Any]], None]) -> None:
        """Call watcher(event, source, job, item) as the review changes (see the top of this file)."""
        self.watchers.append(watcher)

    def _tell(self, told: list[tuple[str, str, str, dict[str, Any]]]) -> None:
        for event in told:
            for watcher in self.watchers:
                try:
                    watcher(*event)
                except Exception as err:  # noqa: BLE001 -- a watcher's trouble is not the review's
                    print(f"sources: telling {event[1]}/{event[2]} {event[0]}: {err}", file=sys.stderr, flush=True)

    def note(self, name: str, job_name: str, commit: str, notices: list[dict[str, str]]) -> None:
        """Keep where a job's waiting was said, while it still waits at that commit."""
        with self.syncing:
            data = self.review(name)
            item = data["pending"].get(job_name)
            if item is not None and item.get("commit") == commit:
                item["notices"] = notices
                self._save_review(name, data)

    def _link(self, source: Source, job_name: str, folder: Path | None) -> None:
        """Point SOURCE/jobs/JOB at a job's folder in a tree, in one step; None removes it."""
        jobs = self.root / source.name / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        if folder is None:
            (jobs / job_name).unlink(missing_ok=True)
            return
        link = jobs / f".{job_name}.new"
        link.unlink(missing_ok=True)
        link.symlink_to(os.path.relpath(folder, jobs))
        link.replace(jobs / job_name)

    def _review(self, source: Source, sha: str, *, baseline: bool = False) -> None:
        """Hold each job of the commit to what was accepted: a job whose access is as accepted (or every job, for
        a baseline) is taken at this commit, and one whose access changed, or that is new, waits. Called with
        `syncing` held."""
        data = self.review(source.name)
        accepted, pending = data["accepted"], data["pending"]
        told: list[tuple[str, str, str, dict[str, Any]]] = []
        found = self._tree_jobs(source, sha)
        (self.root / source.name / "jobs").mkdir(parents=True, exist_ok=True)
        for job_name, folder in found.items():
            access = job_access(folder)
            mark = _fingerprint(access)
            was = accepted.get(job_name)
            if baseline or (was and was.get("fingerprint") == mark):
                self._link(source, job_name, folder)
                accepted[job_name] = {"commit": sha, "fingerprint": mark, "access": access,
                                      "by": was.get("by", "") if was else "", "at": was.get("at") if was else _now(),
                                      **({"baseline": True} if baseline and not was else {})}
                if (gone := pending.pop(job_name, None)) is not None:
                    told.append(("withdrawn", source.name, job_name, gone))
            else:
                before = pending.get(job_name) or {}
                same = before.get("fingerprint") == mark  # waiting as it was, at a newer commit
                pending[job_name] = {"commit": sha, "fingerprint": mark, "access": access, "new": was is None,
                                     "changes": changes(was["access"] if was else {}, access),
                                     "since": before.get("since") or _now(),
                                     **({"notices": before["notices"]} if same and before.get("notices") else {})}
                if not same:
                    told.append(("waiting", source.name, job_name, {**pending[job_name],
                                                                     "replaces": before.get("notices") or []}))
                elif before.get("commit") != sha:
                    told.append(("moved", source.name, job_name, pending[job_name]))
        for job_name in [j for j in accepted if j not in found]:  # gone from the repo: gone from the jobs
            self._link(source, job_name, None)
            del accepted[job_name]
        for job_name in [j for j in pending if j not in found]:
            told.append(("withdrawn", source.name, job_name, pending.pop(job_name)))
        data["seen"] = sha
        self._save_review(source.name, data)
        self._tell(told)

    def held(self, name: str) -> set[str]:
        """The commits a source's jobs are held at: their trees are kept."""
        return {str(r.get("commit")) for r in self.review(name)["accepted"].values()}

    def accept(self, name: str, job_name: str, commit: str, *, user: str = "") -> dict[str, Any]:
        """Accept the change waiting for a job: the one at `commit`, which the person saw. The job runs from it
        from now on."""
        source = self.sources().get(name)
        if source is None:
            raise SourcesError(f"No source named {name}.")
        with self.syncing:
            data = self.review(name)
            waiting = data["pending"].get(job_name)
            if waiting is None:
                raise SourcesError(f"Nothing of {job_name} waits for Accept in {name}.")
            if waiting["commit"] != commit:
                raise SourcesError(f"{job_name} has changed again since (now at {waiting['commit'][:12]}): look at "
                                   "what it changes now, and accept that.")
            folder = self._tree_jobs(source, commit).get(job_name)
            if folder is None or _fingerprint(job_access(folder)) != waiting["fingerprint"]:
                raise SourcesError(f"{job_name} at {commit[:12]} is not there to accept: sync {name} again.")
            self._link(source, job_name, folder)
            data["accepted"][job_name] = {"commit": commit, "fingerprint": waiting["fingerprint"],
                                          "access": waiting["access"], "by": user, "at": _now()}
            del data["pending"][job_name]
            self._save_review(name, data)
        self._tell([("accepted", name, job_name, {**waiting, "by": user})])
        return data["accepted"][job_name]

    def waiting(self) -> list[dict[str, Any]]:
        """What waits for Accept, in every source: for the Jobs tab."""
        found = []
        for name in self.sources():
            for job_name, item in sorted(self.review(name)["pending"].items()):
                found.append({"source": name, "job": job_name,
                              **{k: item.get(k) for k in ("commit", "new", "changes", "since")},
                              "notices": item.get("notices") or []})
        return found

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
                    if self.review(source.name).get("seen") != sha:
                        self._review(source, sha)
                        self._prune(source, sha, before)
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
        self._git(source, "--git-dir", str(repo), "gc", "--quiet", "--prune=now")
        return sha

    def _prune(self, source: Source, sha: str, before: str) -> None:
        """Remove the trees no longer needed: those of neither this commit, the one before (which a run may be
        copying), nor one a job is held at."""
        keep = {sha, before, *self.held(source.name)}
        trees = self.root / source.name / "trees"
        for old in trees.iterdir() if trees.is_dir() else ():
            if old.name not in keep:
                shutil.rmtree(old, ignore_errors=True)

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
            linked = self.root / name / "jobs"
            jobs = sorted(p.name for p in linked.iterdir() if not p.name.startswith(".")) if linked.is_dir() else []
            mine = [j for j in jobs if found is None or (found.get(j) and (self.origin(found[j]) or ("",))[0] == name)]
            status = self.status.get(name, {})
            record = stored.get(name, {})
            rows.append({**{k: v for k, v in asdict(source).items()}, "commit": sha,
                         "token_set": bool(credentials.get(source.token)),
                         "synced": status.get("synced"), "checked": status.get("checked"),
                         "error": status.get("error", ""), "jobs": mine,
                         "waiting": sorted(self.review(name)["pending"]),
                         "left_out": [j for j in jobs if j not in mine and j in (clashes or {})],
                         "updated": record.get("updated"), "updated_by": record.get("updated_by", "")})
        return {"sources": rows, "interval": self.interval}


def job_access(folder: Path) -> dict[str, Any]:
    """What of a job's job.toml is its access, flattened ("[job] secrets", "egress_allow"...), with the hashes of
    the files it names; for one that does not parse, its text's hash."""
    try:
        text = (folder / "job.toml").read_text()
        values = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as err:
        raw = (folder / "job.toml").read_bytes() if (folder / "job.toml").is_file() else b""
        return {"job.toml": f"does not parse ({type(err).__name__}), sha256 {hashlib.sha256(raw).hexdigest()[:16]}"}
    table = values.pop("job", {})
    access: dict[str, Any] = {key: value for key, value in values.items()}
    if isinstance(table, dict):
        access |= {f"[job] {key}": value for key, value in table.items() if key not in NOT_ACCESS}
    else:
        access["[job]"] = table
    named = [values.get(key) for key in FILE_SETTINGS]
    secrets = table.get("secrets") if isinstance(table, dict) else None
    named += [item.get("ca_file") for item in secrets.values() if isinstance(item, dict)] if isinstance(secrets, dict) else []
    for name in sorted({n for n in named if isinstance(n, str) and n}):
        path = (folder / name).resolve()
        inside = path.is_relative_to(folder.resolve())
        access[f"file {name}"] = (hashlib.sha256(path.read_bytes()).hexdigest()[:16] if inside and path.is_file()
                                  else "outside the job" if not inside else "missing")
    return json.loads(json.dumps(access, default=str))  # as review.json keeps it (a TOML date as text)


def _fingerprint(access: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(access, sort_keys=True, default=str).encode()).hexdigest()


def changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Each setting of a job's access that differs: what it was and is (None for not set)."""
    return [{"setting": key, "before": before.get(key), "after": after.get(key)}
            for key in sorted({*before, *after}) if before.get(key) != after.get(key)]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
