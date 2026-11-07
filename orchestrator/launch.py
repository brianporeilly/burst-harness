"""How a task's burst, or a job's run, is run: a subprocess of the orchestrator, or on a cluster a Kubernetes
Job of its own (docs/burst-jobs.md).

Either way it is `python -m orchestrator.burst OUTPUT -- ARGV` (burst.py), which appends the harness's
output to the task's file (or the run's), where the orchestrator reads its result, and exits with its status.

As a Job, the burst is a pod with its own memory, which goes on while the orchestrator restarts: a running
burst's record names its Job, and a restarted orchestrator follows it again (`reattach`). The pod mounts the
volume the harness's state is on, at the same path, so the output file and the state between bursts are
where they are for a subprocess. Its credentials are not in the Job: the orchestrator keeps them for it under a
one-time token (`Handoffs`), which the pod trades for them as it starts (burst.py), so nothing a person set in
Settings is written to the cluster's records.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness import credentials
from harness.config import HarnessConfig
from harness.session import SessionError, seconds

from .burst import HANDOFF_TOKEN_ENV, HANDOFF_URL_ENV

# Where the harness is run from: this checkout, or the image's copy of it.
REPO = Path(__file__).resolve().parent.parent
# How long a stopped burst gets to release its sandbox before it is killed.
CANCEL_GRACE = 30
# Where a burst's pod trades its token for its credentials, on the orchestrator (app.py).
HANDOFF_PATH = "/internal/bursts/handoff"
# How long a token waits to be traded: the pod's scheduling and its image's pull.
HANDOFF_TTL = 15 * 60
# How often a Job is looked at while it runs.
POLL = 5.0
# Variables Kubernetes sets in a pod about itself and its namespace's Services: the orchestrator's, not a burst's.
POD_OWN = re.compile(r"HOSTNAME|KUBERNETES_[A-Z0-9_]*|[A-Z0-9_]+_(?:SERVICE_HOST|SERVICE_PORT[A-Z0-9_]*|PORT[A-Z0-9_]*)")
SERVICE_ACCOUNT_NAMESPACE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
# The exit status of a burst whose Job went away without saying how it ended.
GONE = -1


class LaunchError(RuntimeError):
    """A burst that cannot be started or followed: a setting missing, or kubectl failing."""


@dataclass
class Launched:
    """A burst under way: its process (a subprocess) or its Job's name."""

    process: subprocess.Popen[bytes] | None = None
    job: str = ""

    def record(self) -> dict[str, Any]:
        """What the task's record keeps of it."""
        return {"job": self.job} if self.job else {"pid": self.process.pid if self.process else None}


class Subprocess:
    """Bursts as subprocesses of the orchestrator: podman, docker, and a cluster unless told otherwise."""

    kind = "subprocess"

    def start(self, slug: str, number: int, argv: list[str], env: dict[str, str], granted: dict[str, str],
              output: Path, *, kind: str = "task") -> Launched:
        with open(output, "ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "orchestrator.burst", str(output), "--", *argv], cwd=REPO,
                env={**env, **granted}, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log,
                start_new_session=True,
            )
        return Launched(process=process)

    def wait(self, launched: Launched) -> int:
        assert launched.process is not None
        return launched.process.wait()

    def stop(self, launched: Launched) -> None:
        # SIGINT, as Ctrl-C would: the harness releases its sandbox (or keeps one with uncommitted work).
        assert launched.process is not None
        os.killpg(launched.process.pid, signal.SIGINT)
        threading.Timer(CANCEL_GRACE, _kill, args=(launched.process,)).start()

    def reattach(self, record: dict[str, Any]) -> Launched | None:
        return None  # a subprocess ended with the orchestrator that started it


def _kill(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass


@dataclass
class Handoffs:
    """Credentials kept for bursts that have not started yet, each under a token good for one use."""

    ttl: float = HANDOFF_TTL
    waiting: dict[str, tuple[dict[str, str], float]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def offer(self, values: dict[str, str]) -> str:
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self.lock:
            self.waiting = {k: v for k, v in self.waiting.items() if v[1] > now}
            self.waiting[_digest(token)] = (dict(values), now + self.ttl)
        return token

    def take(self, token: str) -> dict[str, str] | None:
        with self.lock:
            values, expires = self.waiting.pop(_digest(token), (None, 0.0))
        return values if values is not None and expires > time.monotonic() else None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Jobs:
    """Bursts as Kubernetes Jobs, in the orchestrator's namespace (ORCHESTRATOR_BURSTS=job)."""

    cfg: HarnessConfig
    namespace: str
    image: str
    volume: str  # the PersistentVolumeClaim the harness's state is on, mounted at its state_dir
    handoff_url: str
    account: str = "burst-task"
    memory: str = "2Gi"
    deadline: int = 8 * 3600
    poll: float = POLL
    handoffs: Handoffs = field(default_factory=Handoffs)
    kind = "job"

    @classmethod
    def from_env(cls, cfg: HarnessConfig, port: int) -> Jobs:
        """Settings from ORCHESTRATOR_BURST_*; what is not given is found out where it can be."""
        get = os.environ.get
        if cfg.target != "agent-sandbox":
            raise LaunchError("Bursts as Jobs are for a cluster: HARNESS_TARGET=agent-sandbox.")
        volume = get("ORCHESTRATOR_BURST_VOLUME", "").strip()
        if not volume:
            raise LaunchError("ORCHESTRATOR_BURST_VOLUME: the PersistentVolumeClaim (ReadWriteMany) the harness's "
                              f"state is on ({cfg.state_dir}), for bursts' pods to mount.")
        namespace = get("ORCHESTRATOR_BURST_NAMESPACE", "").strip() or (
            SERVICE_ACCOUNT_NAMESPACE.read_text().strip() if SERVICE_ACCOUNT_NAMESPACE.is_file() else "")
        if not namespace:
            raise LaunchError("ORCHESTRATOR_BURST_NAMESPACE: the namespace bursts' Jobs go in.")
        try:
            deadline = seconds(get("ORCHESTRATOR_BURST_DEADLINE", "8h"))
        except SessionError as err:
            raise LaunchError(f"ORCHESTRATOR_BURST_DEADLINE: {err}") from err
        # The orchestrator's pod, by its address: a token is traded within minutes of being made, while it is up.
        jobs = cls(cfg, namespace=namespace, image="", volume=volume,
                   handoff_url=get("ORCHESTRATOR_BURST_HANDOFF_URL", "").strip()
                   or f"http://{socket.gethostbyname(socket.gethostname())}:{port}{HANDOFF_PATH}",
                   account=get("ORCHESTRATOR_BURST_ACCOUNT", "").strip() or "burst-task",
                   memory=get("ORCHESTRATOR_BURST_MEMORY", "").strip() or "2Gi", deadline=deadline)
        # The orchestrator's own image, so one pin moves both.
        jobs.image = get("ORCHESTRATOR_BURST_IMAGE", "").strip() or jobs._kubectl(
            "get", "pod", socket.gethostname(), "-o", "jsonpath={.spec.containers[0].image}")
        return jobs

    def describe(self) -> str:
        return f"as Jobs in {self.namespace} ({self.image}, up to {self.memory})"

    def start(self, slug: str, number: int, argv: list[str], env: dict[str, str], granted: dict[str, str],
              output: Path, *, kind: str = "task") -> Launched:
        """kind: "task", a task's burst number `number` (burst-SLUG-N-XXXX), or "job", a run of the job `slug`
        (job-NAME-XXXX)."""
        name = f"burst-{slug}-{number}-{secrets.token_hex(2)}" if kind == "task" else f"job-{slug}-{secrets.token_hex(2)}"
        token = self.handoffs.offer(granted)
        manifest = self.manifest(name, (f"burst-harness/{kind}", slug), argv,
                                 {**env, HANDOFF_URL_ENV: self.handoff_url, HANDOFF_TOKEN_ENV: token}, output)
        try:
            self._kubectl("create", "-f", "-", stdin=json.dumps(manifest))
        except LaunchError:
            self.handoffs.take(token)
            raise
        return Launched(job=name)

    def manifest(self, name: str, label: tuple[str, str], argv: list[str], env: dict[str, str],
                 output: Path) -> dict[str, Any]:
        labels = {"app.kubernetes.io/name": "burst-harness", "app.kubernetes.io/component": "burst", label[0]: label[1]}
        state_dir = str(Path(self.cfg.state_dir).expanduser())
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                # The orchestrator decides whether a burst is started again, as for a subprocess.
                "backoffLimit": 0,
                "activeDeadlineSeconds": self.deadline,
                "ttlSecondsAfterFinished": 24 * 3600,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "serviceAccountName": self.account,
                        "automountServiceAccountToken": True,  # kubectl, for its sandbox
                        "restartPolicy": "Never",
                        "enableServiceLinks": False,
                        # Stopping deletes the Job: the pod gets this long to release its sandbox.
                        "terminationGracePeriodSeconds": CANCEL_GRACE,
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                                            "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
                        "containers": [{
                            "name": "burst",
                            "image": self.image,
                            "command": [sys.executable, "-m", "orchestrator.burst", str(output), "--", *argv],
                            "workingDir": str(REPO),
                            "env": [{"name": k, "value": v} for k, v in sorted(env.items())
                                    if not POD_OWN.fullmatch(k)],
                            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                "capabilities": {"drop": ["ALL"]}},
                            "resources": {"requests": {"cpu": "50m", "memory": "256Mi"},
                                          "limits": {"memory": self.memory}},
                            "volumeMounts": [{"name": "state", "mountPath": state_dir},
                                             {"name": "tmp", "mountPath": "/tmp"}],
                        }],
                        "volumes": [{"name": "state", "persistentVolumeClaim": {"claimName": self.volume}},
                                    {"name": "tmp", "emptyDir": {}}],
                    },
                },
            },
        }

    def wait(self, launched: Launched) -> int:
        """Look at the Job until it ends; its pod's exit status, or GONE if it went away."""
        while True:
            try:
                job = self._job(launched.job)
            except LaunchError as err:
                print(f"tasks: {err}; looking again")  # the API server, for a moment: not the burst's end
                time.sleep(self.poll)
                continue
            if job is None:
                return GONE
            status = job.get("status") or {}
            if status.get("succeeded") or status.get("failed"):
                return self._exit_status(launched.job, succeeded=bool(status.get("succeeded")))
            time.sleep(self.poll)

    def stop(self, launched: Launched) -> None:
        # Its pod gets SIGTERM, which burst.py passes on as SIGINT, and terminationGracePeriodSeconds.
        self._kubectl("delete", "job", launched.job, "--cascade=foreground", "--wait=false", "--ignore-not-found")

    def reattach(self, record: dict[str, Any]) -> Launched | None:
        """The Job a record left running, if it is still there (running or ended). A kubectl failure is raised,
        not taken for a Job that is gone: starting the burst again beside one still running would be worse."""
        name = str(record.get("job") or "")
        return Launched(job=name) if name and self._job(name) is not None else None

    def _job(self, name: str) -> dict[str, Any] | None:
        found = self._kubectl("get", "job", name, "-o", "json", "--ignore-not-found")
        return json.loads(found) if found else None

    def _exit_status(self, job: str, *, succeeded: bool) -> int:
        try:
            pods = json.loads(self._kubectl("get", "pods", "-l", f"job-name={job}", "-o", "json")).get("items", [])
        except (LaunchError, ValueError):
            pods = []
        for pod in pods:
            for status in (pod.get("status") or {}).get("containerStatuses") or []:
                if (terminated := (status.get("state") or {}).get("terminated")) is not None:
                    return int(terminated.get("exitCode", 1))
        return 0 if succeeded else 1  # the pod is gone (its deadline, or deleted): the Job's word for it

    def _kubectl(self, *args: str, stdin: str | None = None) -> str:
        argv = [self.cfg.kubectl, *(["--context", self.cfg.kube_context] if self.cfg.kube_context else []),
                "-n", self.namespace, *args]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, input=stdin, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise LaunchError(f"kubectl {' '.join(args[:3])}: {err}") from err
        if proc.returncode != 0:
            raise LaunchError(f"kubectl {' '.join(args[:3])} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()


def granted(names: list[str]) -> dict[str, str]:
    """The values of the credentials a burst is granted, those that have one."""
    return {name: value for name in names if (value := credentials.get(name))}
