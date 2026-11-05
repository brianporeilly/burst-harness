# Bursts as Kubernetes Jobs

A design, not yet built (2026-11-05). ROADMAP.md, phase 9, step 4.

## Why

On a cluster, each burst of a task is a `python -m harness run` subprocess
of the orchestrator, in its pod. That ties the burst to the pod:

- **A rollout cuts it short.** The orchestrator's Deployment is `Recreate`
  with a 30s grace, so every new image or setting stops the bursts running.
  `recover()` queues them again (up to twice), and the new burst starts from
  the pushed branch and the Strands snapshot. What was under way is lost: the
  turn in progress, a Claude Code session and its delegates, and the model's
  prompt cache, which llama-server must fill again. Two rollouts in two days
  (0.2.0 and the image pins) would each have done it.
- **One burst's memory is everyone's.** The pod's limit (1Gi) covers the web
  page, the Slack bot, the scheduler and every burst. A burst that grows
  (a big context, a runaway tool's output) takes all of them down.
- **Running more than one task at a time** is bounded by that one pod. The
  limit is 1 for llama-server's sake, but Claude-led tasks don't use it.

As Jobs, a burst is a pod of its own: its own memory limit, its own logs, and
it goes on while the orchestrator restarts.

## What stays the same

- `harness run` is still the unit: a Job runs the same argv, with the same
  environment, as the subprocess does now.
- The task records, the queue, the limit, bursts' states and Slack's thread
  are the orchestrator's, as now.
- podman, docker and a Mac keep subprocesses. Jobs are for
  `HARNESS_TARGET=agent-sandbox`, where the sandbox is already a pod.
- Sessions, and the release of a kept sandbox (a short `harness release`),
  stay in the orchestrator.

## Decisions

### 1. State: a shared volume

The burst needs the harness's state between bursts, not only its result:
`HARNESS_STATE_DIR` holds the Strands snapshot (`SnapshotSessionManager`,
how a continued task remembers), the router's choice (`SLUG.agent.json`), a
question's runs and `report.md`, and the approved egress. So logs alone are
not enough, and the state must be readable from any node.

- `HARNESS_STATE_DIR` moves to a new **ReadWriteMany** volume
  (`ceph-filesystem`, already in the cluster), mounted by the orchestrator and
  every burst.
- What is the orchestrator's alone (the encrypted credentials, API tokens,
  task records) stays on its own ReadWriteOnce volume, which bursts do not
  mount. A burst can then never read another task's credentials, even sealed.
- One burst per task at a time is the orchestrator's rule already, so no two
  pods write the same snapshot.

Moving the existing state is a one-off copy (a Job with both volumes) when
the change is deployed.

Not chosen: pinning bursts to the orchestrator's node to share its RWO volume
(gives back the isolation), or having the burst fetch and return a tarball of
its state (a burst killed outright loses everything since it started).

### 2. Output and the result: a file on the shared volume

Each burst's output goes on appending to the task's output file, now on the
shared volume, so the page's tail and `_result()` (the last `HARNESS_RESULT`
line) work unchanged, and survive an orchestrator restart.

A small wrapper, `python -m orchestrator.burst OUTPUT -- ARGV...`, runs the
harness, copies each line to the file and to stdout (so `kubectl logs` shows
it too), and exits with the harness's status. The subprocess path uses the
same wrapper, so there is one way a burst's output is written.

The exit status comes from the Job's pod (`containerStatuses[].state.terminated.exitCode`).

### 3. Credentials: fetched once, never in etcd

A burst is granted what `credentials.for_task()` gives it now (git, the
model's keys, the gateway's, MCP keys). Some of those come from the Settings
tab, decrypted only in the orchestrator's memory.

Putting them in a Secret per burst is simplest, but this cluster's API server
does not encrypt Secrets at rest (no `--encryption-provider-config`), and
Settings exists to keep person-entered credentials out of the cluster's
records. So instead:

- The orchestrator makes a one-time token for the burst, and puts only that
  in the Job (an env var, valid for a few minutes and one use).
- The wrapper exchanges it on start, at `POST /internal/bursts/ID/env` on the
  orchestrator's Service, for the granted credentials, and puts them in the
  harness's environment. Nothing is written to disk.
- Burst pods may reach the orchestrator's port 8700: an egress rule in the
  agent-runner policy (which denies private addresses otherwise) and an
  ingress rule in the orchestrator's. The token is what opens the path.

The non-secret environment (`HARNESS_*`, the task's model settings) goes in
the Job's spec as it goes in `Popen(env=...)` now: `credentials.scrubbed()`
plus `_model_env()`.

### 4. Identity and network

- Bursts run as a new ServiceAccount, `burst-task`, bound to what claiming
  and driving a sandbox needs: the part of the orchestrator's Role in
  `agent-sandbox-system` that `harness run` uses (sandboxclaims, sandboxes,
  pods, pods/exec, and what the gateway's registration needs), without what
  only sessions use (routes, route filters, security policies). Which rules
  those are is checked against a run, not guessed.
- The orchestrator gets a Role in `agent-runner`: Jobs (create, get, list,
  watch, delete), pods (get, list, delete) and pods/log (get).
- Burst pods carry `app.kubernetes.io/name: burst-harness`, so the existing
  agent-runner egress policy (the API server, llama-server, the gateway, and
  443 to the internet for GitHub and Anthropic)
  covers them as it covers the orchestrator, with
  `app.kubernetes.io/component: burst`.

### 5. The Job

- **Image:** the orchestrator's own (read from its pod), so one pin in
  home-ops moves both. `ORCHESTRATOR_BURST_IMAGE` overrides it. A burst
  started before a rollout keeps the old image until it ends.
- **Name:** `burst-SLUG-N` (N, the burst's number): creating it twice fails,
  so a burst can't be started twice by a restart racing a dispatch.
- `backoffLimit: 0` (the orchestrator decides about restarts, as now),
  `ttlSecondsAfterFinished` of a day, and `activeDeadlineSeconds` as a backstop
  (`ORCHESTRATOR_BURST_DEADLINE`, default 8h).
- **Resources:** requests and a memory limit from `ORCHESTRATOR_BURST_MEMORY`
  (default 2Gi), with `terminationGracePeriodSeconds` = `CANCEL_GRACE` + 10.
- Read-only root, non-root, `HOME=/tmp`, as the orchestrator.

### 6. Stop

Stop deletes the burst's pod with a grace of `CANCEL_GRACE`. Kubernetes sends
SIGTERM; the wrapper passes it on to the harness as SIGINT, which is what
Ctrl-C and today's Stop send, so the harness releases its sandbox, or keeps
one with work not yet pushed, without changes of its own. Past the grace,
Kubernetes kills it, as today's timer does.

### 7. Restarts: reattach

A running burst's record keeps its Job's name instead of a pid. On start,
`recover()` looks at each running record:

- its Job is running: follow it again (nothing is queued);
- its Job finished while the orchestrator was down: read its status and
  output, and end the burst as `_wait()` does;
- its Job is gone, or its pod was evicted or its node lost: what a restart
  does today (queued again, up to `RESTARTS`).

Following a Job is a poll of `kubectl get job` every few seconds, in the
thread `_wait()` runs in now; no watch to keep open.

## Steps

1. The wrapper (`orchestrator/burst.py`), used by today's subprocesses: no
   change in behaviour, and the SIGTERM path tested locally.
2. A launcher in `tasks.py` with two kinds, subprocess and Job: start, follow,
   stop, reattach. Records keep a `launch` (kind and name) in place of `pid`.
   Tested against a fake `kubectl`, as the agent-sandbox tests do.
3. The credential exchange: one-time tokens, the endpoint, the wrapper's side.
4. home-ops: the RWX volume and the one-off copy, `burst-task` and its
   bindings, the orchestrator's new Role, the ingress rule, and
   `ORCHESTRATOR_BURSTS=job`.
5. On the cluster: a task stopped at the model (no key), then one on Haiku;
   Stop; and an orchestrator rollout while a burst runs, which it should
   survive.
6. Then questions (tasks with no repo) and scheduled jobs, on the same launcher.

## Open

- The burst's memory default: 2Gi is a guess. Today's bursts have not been
  measured on their own.
- Whether the limit (one task at a time) should become one per provider, now
  that Claude-led tasks no longer share a pod with llama-server ones.
- Whether the orchestrator's Deployment can then lose `Recreate`: its own
  volume is still RWO, so probably not, but a rollout no longer costs a burst.
