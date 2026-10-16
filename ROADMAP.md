# Roadmap

Where this is going: replace OpenHands on the cluster with something of our own
that can run autonomous agents (today's `run`), interactive ones, and scheduled
jobs, with network access that is controlled and auditable. Local podman stays a
first-class path throughout; every phase lands there first.

## Goals

1. **See all network activity** from anything in a sandbox.
2. **Contain filesystem damage**, so an agent can be given free rein. Nothing
   done in a container or pod may affect its host.
3. Cheap defence against hostile code escaping the container. Deferred: gVisor
   is not planned to start.

UIs are LAN-only (WireGuard covers remote); nothing is exposed publicly.

## Components

| Component | Lives | Role |
|---|---|---|
| **Harness** | `harness/` | Run one task or one session in a target. The CLI stays the unit everything else launches. |
| **Gateway** | `gateway/`, own image | A sandbox's only network peer: policy, audit events, credential injection, inbound UI traffic. Imports nothing from the harness; configuration in, JSONL events out. Replaces `burst-egress-proxy`. |
| **Control plane** | a separate project, later | API, audit store, approvals UI, session routing. Launches `harness` rather than reimplementing it. |
| **Cluster network policy** | home-ops | Default-deny egress around sandboxes, routes to session UIs. |
| **Jobs** | their own repos, one per instance | Scheduled or triggered runs: a `job.toml` (harness config plus a `[job]` table) and a prompt. Run and rendered by the harness (phase 8). |

Not adding: a second backend abstraction (`ExecTarget` with `acquire` /
`release` / `attach_hint` is it, and `agent-sandbox` is already the k8s
implementation), or a YAML sandbox spec (the layered config covers it until a
control plane needs to store specs).

## Decisions

- **One gateway per sandbox** for now, as `--egress allowlist` does today:
  attribution is trivial. Revisit when the control plane exists.
- **Credentials either way.** Each credential is either injected by the gateway
  (the sandbox never sees it) or passed into the sandbox, as the `claude-code`
  engine does now. Gateway injection is the default we build towards; passing
  one in is never disallowed.
- **Selective TLS interception.** Intercept only where injection or path rules
  need it (GitHub, model APIs); elsewhere pass through and log the host. The
  sandbox image trusts the gateway's CA.
- **HTTP(S) only**; git over HTTPS. A self-hosted LLM endpoint on the LAN is an
  explicit plain-HTTP exception, and the rest of the LAN is denied by default.
- **Denials are fast.** A blocked request gets an immediate 403 with a JSON body
  and a request ID; connections are never held open waiting for approval. Agent
  instructions say not to retry-loop on it.
- **Three engines.** `strands` (loop on the driver), `claude-code` and
  `opencode` (loop in the target). opencode is for llama.cpp models and its web
  UI, and is built for sessions first.
- **Sessions are free-form.** A session is a target, a gateway, optionally a
  clone, and a UI. No branch setup, task document, or terminal state; a repo is
  optional. Pushes happen from inside the sandbox with gateway-injected
  credentials, so protected branches are guarded by token scope and GitHub
  branch protection, not `git_policy.py`.
- **Root filesystem.** Read-only root is an option, not the default. Root in the
  container (for `dnf install`) is an opt-in: on rootless podman now, in the
  cluster only once user-namespace pods (`hostUsers: false`) are verified there.
  Agents are told to prefer non-root installs.
- **Web terminal (zellij) is optional** per session, bound to localhost in the
  sandbox and reached through the gateway. Both UIs require authentication even
  on the LAN: a shell in a session can use that session's injected credentials.
- **A hostname per session**, not a `/s/<id>/` path prefix, to avoid subpath
  breakage and keep sessions' cookies apart. In the cluster that is one
  HTTPRoute per session on the existing Envoy Gateway.

## Phases

### 1. Gateway v0: observe

Done for podman/docker: `--egress log`, `gateway/`, events saved under
`state_dir` when the container is removed. Chosen for DNS: a sink that answers
NXDOMAIN and records the query, since nothing behind the proxy needs a real
answer and forwarding would open a way out that the allowlist does not have.

- `gateway/` with its own image; `burst-egress-proxy` moves there.
- `--egress log`: everything allowed, everything recorded, to learn what agents
  reach for before writing allowlists.
- Structured JSONL events (time, sandbox, verdict, host, port, bytes), written
  where they outlive the container.
- The gateway answers and logs the sandbox's DNS.

### 2. Traffic that ignores the proxy

Done for podman. Three approaches were run on rootless podman 5.8 with the
sandbox's capabilities dropped:

| Approach | Result |
|---|---|
| **Gateway as the network's default route** (`network create --route`), redirect rules in the gateway's namespace | Chosen. The sandbox needs nothing; every TCP connection is seen with the address it dialled; with the rules removed nothing gets out. Costs the gateway `NET_ADMIN` in its own namespace. |
| **Shared network namespace**, redirect by UID | Works, but fails open: without the rules the sandbox has the gateway's route out. UID matching also breaks once root-in-container is allowed. |
| **Staying explicit**, DNS sink only | What phase 1 had, and what docker still has (it cannot give a network a route). Bare-IP connections leave no trace. |

DNS steering goes with the chosen approach: the gateway answers allowed names
with its own address and resolves nothing for the sandbox, so names work for
software that ignores the proxy without opening a DNS channel out.

Still unseen: UDP other than DNS, and non-TCP protocols (dropped, no event).

For the cluster (phase 6), not yet tried: the same redirect needs an init
container with `NET_ADMIN` in the sandbox pod, which a warm-pool template may
not want. Start there with Calico default-deny plus a `Log` rule (open-source
Calico has no FQDN policy) and the gateway as the pod's resolver, and revisit.

### 3. Interception and credential injection

Done on podman/docker: `--egress-inject git,model`. The gateway makes a
per-sandbox CA and terminates TLS only for hosts it has a credential for.

- `git`: the token is set on requests for the repo; the harness then passes none
  of its own, which closes the token-in-argv exposure for those runs.
- `model` (engine=claude-code): the sandbox holds placeholders, swapped for the
  real credential at the API's host.

Left open: whether injection becomes the default whenever there is a gateway.
With `git` injected the agent's own `git push` authenticates, which it does not
today, so it stays opt-in until that is decided. Also unbuilt: connection reuse
to terminated hosts (each request is its own connection).

### 4. Sessions

Done on podman: `harness session start | stop | rm | list`, optional repo, two
volumes (workspace and home), opencode's web UI reached through the gateway
behind a per-session password, opencode configured from the harness's
llama.cpp / OpenAI-compatible model settings, and `host:port` allowlist entries
for a plain-HTTP model server.

Differs from the plan below: opencode is not an `--engine` (there is nothing
for it to do in a task yet); it is what a session runs.

Also done: the optional web terminal (`session start --terminal`). zellij's web
server only listens on loopback without a certificate, so socat relays to it
inside the sandbox and the gateway passes connections to that; its own login
token is the authentication.

Also done: `--idle` and `--lifetime` (the gateway counts and exits; the
sandbox follows), `--root` on rootless podman for tasks and sessions, and
opencode's Anthropic key at the gateway (`--egress-inject model`).

Phase 4 is complete on podman. Carried forward: sessions on anything but
podman, and TLS for the UIs (they are plain HTTP behind a password or token).

- `opencode` engine; llama.cpp via its OpenAI-compatible endpoint.
- `harness session`: start detached, list, reattach, stop. Optional repo.
- A named volume (a PVC in the cluster) for the workspace and UI state, so a
  crash means restart and reattach. Lifetime and idle reaping carried on
  container / claim labels, since no driver process outlives the command.
- opencode web and, optionally, zellij web, reached through the gateway.
- Root-in-container opt-in for podman.

### 5. Approvals

Done on podman/docker: `harness egress pending | approve | allow | revoke |
list`, an approvals file each gateway re-reads when it changes, scopes of once,
session, a duration, or permanent, and `--egress-webhook`.

Not done: surfacing a refusal as a `handoff_to_user` pause in `run`. For now a
finished task lists what it was refused, and the 403 tells the agent to carry on
or say it is waiting.

- Policy file the gateway reloads without a restart; `harness egress approve
  <request-id>` with a scope of once, session, TTL, or permanent.
- In `run`, a blocked host can surface as the existing `handoff_to_user` pause.
- Notifications behind a webhook, so ntfy or similar can be attached later.

### 6. Cluster

Decided (2026-10-13): **one shared gateway per sandbox namespace**, not a
sidecar. A sidecar shares the sandbox pod's network identity, so Calico cannot
tell it from the agent: if the gateway can reach the internet, so can the agent
(the fail-open case from phase 2). A shared gateway lets Calico allow sandbox
pods to reach only it, needing no extra privileges. It tells sandboxes apart by
pod IP; per-sandbox policy and credentials are pushed to it after a claim, the
way approvals already reach a running gateway, which keeps warm pools usable.
agent-sandbox stays the substrate. Open: whether a claimed sandbox's volume can
outlive a stop, which sessions need. home-ops #1083 (in-cluster task runs,
without a gateway) comes first.

- Gateway alongside each claimed sandbox; default-deny egress NetworkPolicy
  allowing only it (home-ops).
- HTTPRoute per session.
- Verify user-namespace pods before allowing root there.

### 7. Control plane (orchestrator)

Started (2026-10-13) as a proof of concept in this repo, `orchestrator/`, on
podman: a local web page that creates and manages sessions with every option a
session has, shows each gateway's events live, and approves refusals. It calls
the harness's session and egress code directly; it moves to its own repo when
the Kubernetes backend arrives. Sessions can now run Claude Code (in a browser
terminal) as well as opencode, and keep their own allowlists. OpenHands is not
used and is to be removed from home-ops once this covers the same ground.

Original plan:

API and dashboard over what the earlier phases emit: live event feed, pending
approvals, session list and launch, audit store (SQLite to start). A
mobile-friendly chat front end or a Slack bot belongs here if opencode's own UI
turns out not to be enough. A shared gateway is reconsidered at this point.

### 8. Jobs

Added 2026-10-16, from a design brief on the workloads to run (not checked in).
A job is an unattended, mostly read-only run with a narrow brief, on a
schedule or a trigger: a Renovate and repo health digest, Gmail triage, a
"where I left off" digest, a cluster watcher, PR reviews, and at work, Slack
digests and a `/catchup` command. Two instances, personal (cluster) and work
(podman or docker), share only the harness code and the job contract. Their
credentials, config and state are kept apart.

**Jobs live in their own repos.** One repo per instance, say `burst-jobs`
(personal) and one at work. Each job is a directory holding:

- `job.toml`: harness config, the same fields as `.harness.toml` (model,
  engine, image, egress allowlist, injection), plus a `[job]` table with
  `trigger`, `output`, `mode` and `secrets`;
- `prompt.md`: the brief;
- an optional `scripts/` folder, copied into the sandbox. Deterministic work
  runs from here, such as Gmail's rules-first pass, so the model only sees
  what is ambiguous.

The harness does not know any particular job. Adding one means adding a
directory, not changing harness code. Because a job spec is harness config
with one more table, this is not the separate YAML spec ruled out above.

**Contract:**

- **Trigger:** `schedule = "0 7 * * *"`, a webhook, or a Slack command. Cron
  is rendered (below). Event triggers arrive at the control plane.
- **Mode:** `report` (the default) or `act`, enforced by the gateway, not the
  prompt. In report mode, the job's API hosts are intercepted and only
  read methods are allowed (for Gmail, also a list of read endpoints). A
  hijacked report job therefore cannot archive, merge or post.
- **Secrets:** named, and injected by the gateway where possible: GitHub and
  Slack tokens, and Gmail OAuth with the `gmail.modify` scope. Gmail needs
  something new: the gateway refreshing an access token from a refresh token.
- **Output:** the job writes its report to a file. The driver posts it to
  Slack, stdout or similar, the same way it opens PRs today, so the sandbox
  needs no token that can post.
- **State:** a small SQLite file per job, on a volume mounted at `/state`
  (last run, processed IDs, pending plans). This is a podman volume locally
  and a PVC in the cluster, backed up the way home-ops backs up other PVCs.
- **Run log:** every run records its result and its gateway events, so a job
  that quietly stops working gets noticed (a "no successful run in N days"
  check).

**Approvals for actions** follow phase 5's rule: never wait in memory. A run
in act mode starts as a plan ("archive 212 messages from these 9 senders"),
which goes into the job's state and out to Slack. Approving it, by sender or
category, records the decision through the control plane. The next run, or
one the approval triggers, carries out only what was approved. A restart
loses nothing, because nothing is waiting.

**Steps:**

1. `harness job run <dir>` on podman: load the spec, run without a repo,
   collect the report, post it. First job: the Renovate and repo health
   digest. It is read-only and needs one token, so it exercises the whole
   path: trigger, container, model, Slack.
2. `harness job render <dir> --to systemd|cronjob`. Both outputs schedule the
   same driver command, so the renderer stays small. The CronJob runs the
   driver image, as home-ops #1083 does, and it starts the sandbox through
   agent-sandbox. Until phase 6's gateway exists, a cluster job's credentials
   are passed into the sandbox, so only read-only tokens are acceptable there.
3. State volumes, report mode enforced at the gateway, and the run log.
4. Gmail: report-only triage and an action-needed digest first. Then cleanup
   as a dry run with approvals, then with actions enabled. Every action gets a
   label (`bot/archived`, `bot/trashed`), and nothing is ever sent or drafted.
   On the cluster, this waits for phase 6's gateway.
5. Event triggers through the control plane: webhooks (a phone shortcut for
   capturing tasks), Slack commands and interactions (approvals,
   `/catchup`), and Forgejo label events (below).
6. The work instance: the same harness, its own jobs repo, on podman.

**PR reviews on Forgejo** become a job like the rest. Adding a label (say
`ai-review`) to a PR starts a review, in a sandbox that can only clone. The
driver posts the review through Forgejo's API, which needs a small forge layer
because PRs go through `gh` today. The trigger is a Forgejo webhook to the
control plane, or a `pull_request_target` workflow on a Forgejo Runner (none
is deployed yet). Labelling needs write access, so the label is the trust
gate. No PRs from forks until phase 6's gateway is in place.

**Not decided:**

- The personal instance's models: local on the cluster, or a cloud API with
  scoped keys. This decides whether Gmail triage can run entirely on the
  cluster. Routing is per job either way, via `job.toml`'s model fields.
- Whether a job reaches Gmail and Slack with `curl` plus scripts, or through
  MCP servers in the sandbox.

## Open

- How much workspace snapshotting or automatic WIP commits sessions need beyond
  a persistent volume.
- Whether podman can publish ports from an `--internal` network, or whether
  inbound UI traffic must always enter through the gateway (assumed above).
- zellij web's TLS and token requirements behind a proxy.
- Cluster Kubernetes, runtime and kernel versions against what
  `hostUsers: false` needs.
