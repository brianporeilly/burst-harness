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
  container (for `dnf install`) is an opt-in: on rootless podman, and in the
  cluster from a second pool whose template runs user-namespace pods
  (`hostUsers: false`; phase 6 step 4).
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

Phase 4 is complete on podman, and sessions run on docker too (2026-10-29: the
sandbox gets a fixed address on its network there as well, just no route).
Carried forward: TLS for the UIs (they are plain HTTP behind a password or
token).

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
agent-sandbox stays the substrate. home-ops #1083 (in-cluster task runs,
without a gateway) comes first.

Steps:

1. The gateway's shared mode: done (2026-10-21). With `EGRESS_CONTROL` set,
   one gateway serves many sandboxes, known by source address. A driver
   registers each claimed sandbox (allowlist, credentials, read-only and its
   exceptions, upstream CAs, approvals, a ttl) over a token-protected control
   API, reads its events back, and unregisters it. An unregistered or lapsed
   address is refused everything. TLS is terminated with a CA from files
   (cert-manager's), so sandboxes can trust it from their template and a
   restart breaks nothing but registrations, which drivers make again
   (or, with `EGRESS_STATE`, nothing: step 5).
   Single-sandbox gateways on podman are unchanged.
2. The driver: done (2026-10-21). With `gateway_control` set, a claimed
   sandbox's pod IP is registered with the settings a podman gateway would
   get (and a ttl ending at the claim's deadline), before anything runs in
   it; release unregisters it and saves its events where the run log reads
   them. Jobs run with `target = "agent-sandbox"` (`HARNESS_TARGET` turns a
   podman job into a cluster one), but cannot pass secrets into the sandbox:
   a warm-pool claim takes no environment. The sandbox image trusts a CA
   mounted into the pod (`BURST_GATEWAY_CA`), building its bundle at start.
3. home-ops: done (2026-10-21, home-ops #1224-#1227). The gateway
   (Deployment, Service, CA Certificate, control token) in
   `agent-sandbox-system`; Calico letting sandbox pods reach only the gateway's
   proxy port (no DNS), the gateway reach the internet and the hosts jobs name,
   and only the driver reach its control port; the proxy and CA in the
   SandboxTemplate, which also declares its own network policy (the
   controller's default allows all public egress, and Calico counts that as an
   allow); a cluster-watcher CronJob, whose state volume is ceph-block, as
   CephFS mounts fail here.
4. Sessions on the cluster, with an HTTPRoute per session: built (2026-10-21).
   `harness session` with `target = "agent-sandbox"` claims `session-NAME`
   from the pool (no deadline unless `--lifetime`), registers it with the
   gateway including the Host names of its UI and terminal, starts the UI over
   `kubectl exec`, and applies an HTTPRoute per name, owned by the claim, to
   the gateway's new `EGRESS_INBOUND_HTTP` port, which passes each connection
   to the sandbox holding that name. `stop` suspends the Sandbox
   (`operatingMode: Suspended`: the pod goes, the workspace volume stays),
   which answers the open question of whether a volume can outlive a stop;
   `start` resumes and registers it again. The agent's home is kept on the
   workspace. `gateway_control = "kubectl://NS/SERVICE:PORT"` reaches the
   control API by port-forward, for a driver outside the cluster.
   User-namespace pods checked (2026-10-21): Kubernetes 1.36, Flatcar's 6.12
   kernel and containerd 2.2 run `hostUsers: false` pods, root inside mapping
   to an unprivileged host uid, writing and chowning on an idmapped ceph-block
   volume. Root sessions (2026-10-23, checked live: uid 0 in the pod, mapped
   to an unprivileged range on the node, with only the template's
   capabilities): `--root` claims from `root_pool`, a second warm pool whose
   template has that; the claim's pool fixes which kind a session is.
   `--idle` (2026-10-23): the registration carries it, the gateway reports
   each sandbox's last activity, and `harness session reap`, run by a CronJob
   with the driver's rights, suspends what has been idle too long. The
   gateway is given no rights over pods.
5. Registrations survive a gateway restart: done (2026-10-23, checked live:
   a session's UI and allowlist held across `kubectl rollout restart`). With
   `EGRESS_STATE`, the shared gateway keeps each registration as it was sent
   (with its expiry and spent `once` approvals) in a file, rewritten on every
   change, and restores those that have not lapsed when it starts. The file
   holds the credentials registrations carry, so it is on a volume only the
   gateway mounts (a ceph-block PVC; the Deployment recreates rather than
   rolls, as an RWO volume is on one node at a time). Events are not kept.

Open: the control API is plain HTTP inside the cluster, and a registration
carries credentials, so it relies on Calico keeping everyone but the driver
off that port. TLS for it (the gateway already has a CA) if the pod network
is not trusted.

### 7. Control plane (orchestrator)

Started (2026-10-13) as a proof of concept in this repo, `orchestrator/`, on
podman: a local web page that creates and manages sessions with every option a
session has, shows each gateway's events live, and approves refusals. It calls
the harness's session and egress code directly; it moves to its own repo when
the Kubernetes backend arrives. Sessions can now run Claude Code (in a browser
terminal) as well as opencode, and keep their own allowlists. It replaced
OpenHands, removed from home-ops on 2026-10-25 (step 4).
Jobs (2026-10-20): a Jobs tab, given `--jobs DIR`, lists jobs and their last
outcome, runs one on demand, and shows each run's report, output and network
summary, from the run log the harness now keeps (`harness job runs`).
Scheduling lives here too (2026-10-25, phase 8 step 2).

On Kubernetes, in steps:

1. A cluster backend (2026-10-23): `ClusterBackend` drives
   `harness.cluster_session`, and reads each session's events and refusals
   from the shared gateway's control API, which now also takes approvals for a
   registered sandbox (`POST /sandboxes/NAME/approvals`) without the rest of its
   registration being sent again. Run from a laptop with the cluster settings.
2. Session records on the cluster (2026-10-23): a ConfigMap per session, named
   as its claim and owned by it (applied server-side, so no annotation copies
   the password), so the orchestrator and the CLI see the same sessions. Older
   local records are read, and moved at the next start. First a Secret; a
   ConfigMap since step 3, as the right to list a namespace's Secrets would
   include Flux's decryption key, which every namespace in home-ops holds.
3. The orchestrator in the cluster (2026-10-24): the harness image now carries it, and
   `--listen`, `--host` and `--user-header` let it sit behind a proxy that
   signs people in (and refuse anything that didn't come through it). In
   home-ops: a Deployment with a session's rights, an HTTPRoute behind
   Authentik's OIDC, and a network policy that admits only Envoy. Its
   sessions connect to Context7's MCP server (2026-10-25).
4. OpenHands removed from home-ops (2026-10-25), with its state, its
   runtime namespace and its Authentik application.

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
  something new: an access token from a refresh token. Done (2026-11-07) on
  the driver rather than the gateway: a secret with `token_url` is exchanged
  as the run starts, and the gateway adds the access token as any other
  (a run is shorter than its hour). `harness oauth` signs in once.
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
Done in the harness (2026-11-09): `mode = "act"` runs are read-only and
write `plan.json`; the plan is kept in the job's state, its items approved or
rejected (`harness job approve|reject`), and `harness job apply` runs the
job's `apply` command on the approved items, with no model and only
`apply_writes` let through. In the orchestrator: the Jobs tab and the API
decide, Slack posts a plan with buttons, and deciding the last item starts
the apply run. `examples/jobs/mail-sorter` is the first such job: the inbox
by sender, a plan made by its script from the model's choices, and an apply
that only archives, marks read or labels, labels what it changed
`bot/sorted`, and trashes nothing.

**Steps:**

1. `harness job run <dir>` on podman: load the spec, run without a repo,
   collect the report, post it. First job: the Renovate and repo health
   digest. It is read-only and needs one token, so it exercises the whole
   path: trigger, container, model, Slack. **Done (2026-10-16):** `harness
   job run|check`; engines strands (the default: loop on the driver, any
   provider, the key never in the sandbox), claude-code and opencode;
   outputs stdout, file and Slack; secrets added by the gateway at a host,
   or passed in; a failure notice when there is no report. Example:
   `examples/jobs/repo-digest` (the Renovate part only). Verified on podman
   with a stand-in llama.cpp server; not yet run with a real model.
2. Scheduling: in the orchestrator (2026-10-25). With `--schedule`, a job
   whose trigger is a cron schedule runs when it is due, in one timezone
   (`--timezone`), through summer time changes. The run log is the record, so
   a restart neither misses nor repeats a run; a time missed by more than
   `--catch-up` is skipped. Jobs can be paused from the Jobs tab, and with
   `--start-paused` a new job waits until someone resumes it. One scheduler
   for podman and the cluster, and the runs it starts are in the Jobs tab
   like any other; on the cluster it replaces the CronJobs. Before that
   (2026-10-20), deferred: not systemd; a cron in the orchestrator, launchd
   on macOS, or CronJobs in the cluster were the options. A real Slack bot (not just a webhook) also waits; it belongs with the
   control plane's triggers.
3. Report mode enforced at the gateway: done (2026-10-20). The gateway
   terminates all of a report job's TLS and refuses every request that writes
   (403 `read_only`), except git's fetch, the model endpoint for engines that
   call it from the sandbox, and a job's own `writes`. State volumes and the
   run log are still to come; the run log goes with the orchestrator's jobs
   view (below).
   Second example job: `examples/jobs/cluster-watcher` (2026-10-20), against
   the cluster's API server through the gateway: a ServiceAccount token
   added at `host:port`, the server verified against its own CA, report mode
   refusing anything but reads. Its script boils the cluster down to what is
   wrong before the model sees it.
4. Gmail: report-only triage and an action-needed digest first. Mail is read
   by a local model (decided 2026-11-07), so it stays on the network. Then cleanup
   as a dry run with approvals, then with actions enabled. Every action gets a
   label (`bot/archived`, `bot/trashed`), and nothing is ever sent or drafted.
   On the cluster, this waits for phase 6's gateway.
   Done (2026-11-09 to 11-11): `examples/jobs/daily-brief` (today's calendar
   and the mail that may need you, read by a local model) and
   `examples/jobs/mail-sorter` (act mode, above). Beside them,
   `examples/jobs/price-watch`; `[job] memory = true` keeps notes between
   runs, and `quiet` runs send nothing when there is nothing to say.
5. Event triggers through the control plane: webhooks (a phone shortcut for
   capturing tasks), Slack commands and interactions (approvals,
   `/catchup`), and Forgejo label events (below).
   Partly done. Slack interactions: a plan's buttons (2026-11-10) and Accept
   for a job repo's change (2026-11-15). Webhooks (2026-11-15,
   orchestrator/hooks.py): GitHub and Forgejo post to `/hooks/NAME`, signed
   with `<NAME>_WEBHOOK_SECRET`; a merged PR is seen at once (and with
   `auto_continue` starts the task's next round), and a push to a repo of
   jobs syncs it. A delivery is only a hint: the orchestrator looks for
   itself, and polling goes on beside it. On the cluster Forgejo reaches a
   port that answers `/hooks/` alone (home-ops #1489); GitHub's needs a route
   from outside, not added yet. Still to come: a webhook that starts a job or
   a task, Slack commands, and label events.
6. The work instance: the same harness, its own jobs repo, on podman.
7. Jobs from git repos: done (2026-11-11). A repo of jobs, set in Settings or
   `ORCHESTRATOR_JOB_REPOS`, is fetched every few minutes (or on a push's
   webhook) and its jobs listed beside the `--jobs` directories, each run
   copied from one commit. Since anyone who can push to the repo could
   otherwise give a job any secret, write rule or network, a commit that
   changes a job's access waits for a person's Accept (2026-11-14), and the
   job runs as last accepted meanwhile; a change to only its prompt, scripts
   or schedule goes straight through. The listed Slack users are told, with an
   Accept button when the message can show the whole change (2026-11-15).

**PR reviews on Forgejo** become a job like the rest. Adding a label (say
`ai-review`) to a PR starts a review, in a sandbox that can only clone. The
driver posts the review through Forgejo's API, which needs a small forge layer
because PRs go through `gh` today (the forge layer is in: tasks open PRs on
Forgejo, 2026-11-12, and webhooks reach the orchestrator, 2026-11-15; the
review job and the label trigger are not built). The trigger is a Forgejo
webhook to the control plane, or a `pull_request_target` workflow on a Forgejo Runner (none
is deployed yet). Labelling needs write access, so the label is the trust
gate. No PRs from forks until phase 6's gateway is in place.

**Not decided:**

- The personal instance's models: local on the cluster, or a cloud API with
  scoped keys. This decides whether Gmail triage can run entirely on the
  cluster. Routing is per job either way, via `job.toml`'s model fields.
- Whether a job reaches Gmail and Slack with `curl` plus scripts, or through
  MCP servers. Connecting to remote MCP servers is in (2026-10-24, below); this
  is now a question of whether good hosted servers exist for those, with keys
  the gateway can add.

**MCP servers** (2026-10-24): the agent connects to remote ones (`mcp =
["context7=https://mcp.context7.com/mcp"]`), in sessions and jobs, on every
engine: opencode's config, Claude Code's `--mcp-config`, and Strands'
`MCPClient` from the driver. The sandbox engines reach them through the
gateway, which allowlists the host, lets a report-only job POST to the
server's URL, and adds a key from the driver's `<NAME>_API_KEY` as a bearer
token. The first use is Context7 in sessions, for current library docs
instead of web searches. Running MCP servers ourselves (stdio ones, inside the
sandbox) is not planned.

### 9. Tasks from anywhere

Added 2026-10-28. Choose a repo, give a prompt, and walk away: the work goes on
on a machine that stays up (the cluster, or a server reached over SSH) until
there is a PR, with nobody watching a terminal. Sessions stay for working
alongside the agent; tasks are for handing work off. Started from the
orchestrator's page, its API, and later Slack.

**Steps:**

1. A Tasks tab and API in the orchestrator: done (2026-10-28). A task is a repo
   and a prompt, run in bursts of `harness run` as subprocesses, with the
   orchestrator's credentials and target. A burst ends in a PR, a pause with
   questions, an abandon or a failure; continuing (with a reply, or without)
   is another burst from the pushed branch, so nothing waits in memory. Tasks
   queue beyond a limit (default 1: llama-server crashes with two clients at
   once), and the queue is in the records, so it survives a restart; a burst
   a restart cut short is started again, up to twice. A burst that fails or
   is stopped with work not yet pushed keeps its sandbox, and the next burst
   carries on in it. The base branch is the repo's own unless one is given,
   and asking for it checks the orchestrator can read the repo.
   Checked on podman with a public repo (no model key, so each burst stopped
   at the model): the clone, the branch, a failed burst keeping its sandbox,
   continuing in it, Stop, and Remove releasing it.
   On the cluster, this needs a git token in the orchestrator's environment
   (sessions use one only through the gateway) and egress from its pod to
   GitHub, since the driver opens the PR.
2. Callers other than the browser. API tokens: done (2026-10-29). The
   orchestrator makes them (the Settings tab), keeps only a hash, and a token
   reaches the Tasks API alone; tasks it starts are "you via NAME". Without a
   signing-in proxy, `--listen` beyond this machine serves the page locally
   and the Tasks API to token holders. On the cluster, a token needs a route
   to /api/tasks that skips the OIDC sign-in (home-ops; done 2026-10-30). Slack: done (2026-10-30),
   orchestrator/slack.py, Socket Mode, tasks from a mention or a direct
   message, their bursts told in its thread, replies continuing them; the
   app from examples/slack-app.yaml. Slack first on the
   personal workspace, in
   Socket Mode, so the orchestrator needs no public URL (a laptop or a work
   network will do): `/task owner/repo do the thing`, with replies in a
   thread as the task starts, pauses with a question (answered in the thread),
   and ends with its PR.
3. Packaging, for the work machine (macOS with OrbStack) and a server: a
   `pyproject.toml` with `harness` and `orchestrator` commands, managed with
   uv (a lockfile, `uv sync`/`uv run`, uv in the images) in place of pip and
   the requirements files: done (2026-10-29). Docker as the local runtime
   (`HARNESS_TARGET=docker`) for tasks, jobs and sessions, with the
   orchestrator run natively on the Mac: done (2026-10-29), tested against a
   real dockerd but not yet on a Mac. No compose file: a containerized
   orchestrator would need docker's socket (root on the docker host) and the
   host's loopback, where sessions' UIs are published. arm64 images, for
   Apple silicon (docker would not pull amd64-only ones there): each image is
   built for amd64 and arm64 on runners of each architecture, then the two are
   put under one tag (2026-10-30).
Credentials (2026-10-28): each run is handed only what it is granted (a task:
git, model, gateway and MCP keys; a job: what its job.toml names), and
everything reads credentials through `harness/credentials.py`, whose overlay
is where per-user credentials go. Settings (2026-10-30): credentials set in
the page, write-only, kept encrypted (`ORCHESTRATOR_SECRETS_KEY`) and used in
place of the environment's, for the orchestrator as a whole. Not yet: storing them per user (Kubernetes
Secrets on a cluster, the Keychain or a file locally), signing in with GitHub
so a task acts as whoever started it, and a GitHub App instead of a personal
token on the cluster. Those wait for a second user.

4. On a cluster, each burst as a Kubernetes Job of the driver image
   instead of a subprocess of the orchestrator, so its memory is the burst's
   and an orchestrator rollout does not cut it short. Designed (2026-11-05):
   docs/burst-jobs.md. Live on the cluster (2026-11-06), with a run to the
   end, Stop, and a rollout mid-burst checked; jobs' runs on it too
   (2026-11-06).
5. More than one model per run (2026-10-30): named profiles (`[agents.NAME]`),
   a lead that delegates pieces of work to them, and a router that picks the
   lead for a new task or job run (`harness/agents.py`), with example
   scenarios to benchmark against each other. Tasks without a repo
   (2026-11-01): a question, a summary or some research, answered by whichever
   model the router picks (`harness ask`, and a task with no repo in the
   orchestrator, and a Slack message with no repo), run as a one-off job in
   report mode, with follow-ups given the earlier answers. A profile's role as
   a delegate (2026-11-02): its own `prompt`, and `read_only`, which takes the
   file editor away and tells the lead of any file the delegate changed
   anyway. A profile whose provider cannot load (its SDK not installed) stops
   the run at the start (2026-11-03). Measured and left alone: a delegate on
   the lead's one-slot llama-server pushes out the lead's prompt cache, but in
   team-vs-solo benchmarks the lead still read 96-99% of its tokens from cache
   and prompt processing rose by only a few points of a run; generation in the
   delegates and the extra fix rounds take the time. Two slots with the lead
   pinned to one, or saving and restoring its slot, would save about 5% of a
   team run. Not yet: Claude Code as a
   profile (it runs in the target, not as a Strands model), and delegates
   working in parallel (each would need its own worktree).
   Strands' own `ModelRouter` switches models per call and fails over; per
   task was picked here so a task's prompt cache and snapshot stay with one
   model. Its failover (local down, use Claude) may still be worth adding.

Next, in this order (2026-11-04):

6. Model settings for tasks: done (2026-11-04). Effort, thinking and the
   longest reply as fields where a provider has them (Anthropic's effort and
   thinking; llama.cpp's `enable_thinking`; an OpenAI-compatible endpoint's
   reasoning effort), and the extra request parameters (`model_params`) as
   JSON, with examples for the provider chosen. The llama.cpp and
   OpenAI-compatible fields write into the JSON, so what is sent is what is
   shown. For a llama-server, the orchestrator reads its `/props` (a
   description, not a model call) to fill in the context window and say
   whether its chat template can switch thinking. Kept with the task for
   every burst, and taken by `POST /api/tasks` with a token too. Not yet: from
   Slack, and for Claude Code tasks.
7. Versions and releases: built (2026-11-04). On each merge to main,
   python-semantic-release works out the next version from the squashed
   commit, the PR's title (`feat` minor, `fix` and `perf` patch, `!` or
   `BREAKING CHANGE` major from 1.0 on; the other types release nothing),
   commits it to `pyproject.toml`, `uv.lock` and `CHANGELOG.md`, tags
   `vX.Y.Z`, makes the GitHub release, and builds the images for it as
   `X.Y.Z` (a tag pushed with the workflow's own token starts no workflow,
   so release.yml calls images.yml itself). PR titles are checked on each PR;
   AGENTS.md (CLAUDE.md links to it) says how to write them. Tests moved to a
   ci.yml of their own, with uv's cache. It pushes with a GitHub App's token
   when the repo has one (`RELEASE_APP_ID`, `RELEASE_APP_KEY`), which a
   protected main can let past its rules; until then, with the workflow's
   own token. home-ops pins `X.Y.Z@sha256:...`, the release and its digest.
8. Claude as the lead, handing work to Strands profiles: built (2026-11-04).
   With engine=claude-code and `delegate_to`, headless runs go through the
   Claude Agent SDK, whose in-process MCP server offers Claude Code a
   `delegate` tool. Its calls travel over `claude`'s own stdin and stdout,
   through the exec that runs it, rather than a route through the gateway as
   first planned: nothing listens in the sandbox, and no new credentials go
   in. The driver runs each delegate as a Strands lead's (the same function).
   The SDK is built from its source package, since its wheels carry a 125 MB
   Claude Code binary the harness never runs. `chat` has no delegate tool.
   Checked on podman: Haiku leading in the sandbox, one delegation to a
   profile on a fake llama.cpp server, its report back, and a pause; and
   again (2026-11-05) with a real llama-server profile as the delegate.
   Profiles and the default `delegate_to` are set in the orchestrator's
   Settings, and a task picks its own delegates (2026-11-06; README
   "Profiles"); its bursts are given them in their environment, so a burst
   run as a Job needs no config file. After it, maybe the other
   way round (Claude Code as a profile a Strands lead
   delegates to), which would put Claude's token in the sandbox for the run.

Since then (2026-11-12 to 11-16):

9. Tasks that go on for days: done (2026-11-12 to 11-14). Rounds: once a
   task's PR is merged, Continue starts a new round on a fresh branch, and
   with `auto_continue` it starts by itself (seen at once with a webhook). A
   refused push is `push_failed`, opens no PR and keeps the sandbox; a kept
   sandbox's claim is renewed when a burst reattaches, and replaced when it is
   gone.
10. A task's own network: done (2026-11-12). Each task can be permissive, or
    the allowlist with hosts of its own, and what the gateway refused it is
    listed in the task, to allow for the task, for a burst, or for good. A
    build cache (`/cache`) is shared by bursts. The sandbox image has Rust and
    Bevy's tooling; Claude Code tasks run anything without asking, in their
    sandbox, and can leave Claude's attribution out of commits.
11. Runners: done (2026-11-14). Hosts of your own over SSH, as a plain host or
    a remote podman or docker, set in Settings with their SSH key and
    known_hosts, picked per task (or a default). A run on one is given only that
    runner's key. Not yet: sessions on runners, and on the cluster the network
    policy for port 22 to a runner's host (none is added yet).
12. Resources: done (2026-11-14, 11-15). A run killed for memory says so, with
    its limit; the agent is told its sandbox's memory and CPUs, read from the
    cgroup, and to keep builds within them (fewer parallel jobs), rather than
    a cap on them.
13. Warm pools kept on their templates: built (2026-11-15). agent-sandbox does
    not replace a pool's idle sandboxes when the template changes, so after an
    image bump a claim got the old image. The orchestrator replaces those out
    of date every 5 minutes (`ORCHESTRATOR_RECYCLE`), deleting only idle ones,
    with preconditions so a claim is never deleted from under it. Live from
    0.33.1 (a naming bug kept it off on the cluster in 0.31).

14. Slack for tasks started in the page or the API: done (2026-11-16, 0.34.0).
    Ticking "Tell me in Slack" means that when a burst ends, the listed users
    get a direct message whose thread continues the task, as a reply would on
    a task started from Slack.

Next (2026-11-18):

- Phase 10's first steps: run metrics, and the repo's `AGENTS.md` for the
  Strands agent (#31, #32); then budgets per burst. Done, with the rest of
  phase 10 (below).
- Falling back from the local model to Claude when the llama-server is down:
  done (2026-11-19, 0.44.0), set per task in Settings (0.45.0).
- Model settings from Slack.
- Delegates working in parallel, and other delegation improvements.
- Sessions on runners; refusals listed for local podman tasks.
- Checked on the cluster: a Forgejo task end to end, and a second Bevy burst
  using `/cache`.

### 10. Reliable runs, and knowing a change helped

Added 2026-11-18, from a look at harness-engineering material:
learn-harness-engineering, 12-Factor Agents, agents.md, Spec Kit, Deep Agents,
mini-swe-agent, and Harbor. Most of what they recommend is here already: a
committed task document, bursts that pause and resume, asking the user through
a tool, delegates with contexts of their own, offloaded tool output, and
approvals at the gateway. What they add is checking the work before it is
called done, limits on a run, and measuring whether a change to the harness
helped. The rule for that measurement: keep the model fixed and change one
thing at a time.

Measuring belongs to `local-llm-benchmark`. It already runs its tasks through
the harness, and owns tasks, grading, scoring, the matrix of model × profile ×
harness commit, repeats, and reports. The harness reports accurate facts about
each run and judges nothing, so there is one set of numbers.

**Steps:**

1. Run metrics: built (2026-11-18, #31). `HARNESS_RESULT` and `HARNESS_JOB`
   carry `metrics`:
   - wall time, time waiting on the model, and time running tools;
   - model calls, tool calls and errors;
   - the lead's tokens, and the largest prompt;
   - cut-off replies, window drops and retries;
   - an approximate cost;
   - fix-ups and questions to the user.

   Delegates get the same `metrics`. `config` names the `--config` layers and
   hashes the settings that shape the agent's work, so A:B runs can't be
   compared under the wrong label. The fields follow what the benchmark
   already records, so it can stop parsing the log. The Tasks tab shows each
   burst's time, calls and cost, and a total for the task.
2. The repo's instructions for agents: built (2026-11-18, #32). The Strands
   agent and its delegates are given the repo's `AGENTS.md` (or `CLAUDE.md`)
   below the harness's own rules. It can be turned off, and what was read is
   listed in `context_files`. Once both are merged, its settings join the
   `config` hash.
3. Budgets per burst: built (2026-11-18, #34). `max_model_calls`, `max_cost_usd` and
   `max_burst_minutes` are checked before each model call. Reaching one
   pauses for review, with the limit reached as the next step, so the work is
   never stranded. This stops a runaway burst spending credits overnight, and
   a stuck local burst holding the one llama.cpp slot.
4. Noticing a loop: built (2026-11-18, #36). The same tool call failing the same way three times tells
   the model to change its approach; five times pauses for review. Small
   local models are the ones that loop.
5. Checked before a PR: built (2026-11-18). A repo-settable `check_command`
   (its tests, say) runs in `finish_with_pr` before anything is pushed, on
   both engines. A failure goes back to the model with the end of its output,
   up to `max_fixup_attempts`; then it is told to pause, and finishing anyway
   pauses it. `metrics.checks` counts runs and failures. Acceptance criteria
   in the task document, filled from the prompt, are left for later.
6. A setup step before the model: built (2026-11-18). A repo-settable
   `setup_command` runs after the branch is made, before every burst (a new
   sandbox needs it again). Its output (dependencies installed, tests failing
   before any change) goes into the first prompt of a task or round, so the
   model doesn't spend turns finding out.
7. A review before the PR: built (2026-11-18). `review_with = "NAME"` runs
   that profile, read-only, on the branch's diff (new files included) and the
   task document once the check passes. Its findings go back to the lead once,
   then into the PR's body; NO FINDINGS lets the first finish through. Either
   engine can lead. A local model reviewing costs nothing.
8. A run's transcript as data: built (2026-11-18). One JSON per burst holds
   every message of the lead and its delegates, the tool and model calls with
   when each started and how long it took, and the metrics. It is recorded as
   the burst runs, so it has what the window dropped, and Claude Code's runs
   too; the benchmark's `transcript.json` was the session snapshot, which has
   neither. A format Harbor reads is still worth a look.
9. Comparing harness changes. What stays here: built (2026-11-19,
   `tests/test_smoke.py`). A tiny task on the fake llama server sets up,
   delegates, edits, passes its check and review, and pushes; every key of
   HARNESS_RESULT and its `metrics` is checked to be there, none null. The
   same burst then replays from its transcript (`fake_llama.from_transcript`,
   or `python -m tests.fake_llama TRANSCRIPT.json` for a live run) to the
   same end and the same tool calls. Everything that scores or ranks
   is the benchmark's. It runs a variant per `--config` layer against a
   baseline with the model fixed, and reports the before and after for a PR
   that changes how the harness behaves. A job (a weekly run of a few tasks on
   the local model, its table posted to Slack) could do it unattended.

10. The repo carries the context (2026-11-20). From a long local round on a
    27B model, which repeated the same recap for 35 minutes: a next round
    restored round one's whole conversation, and when the window cut it back
    it kept round one's prompt pinned, so the model kept reconciling a stale
    goal with the work in front of it. The task document and git are the
    record, as in get-shit-done without its token cost. In order:
    1. Every burst starts from the repo: built (2026-11-20). A fresh
       conversation, given the task document, the branch's commits and
       anything uncommitted; an answer comes with its question.
       `resume_from = "snapshot"`, or a task's "Carry each burst's
       conversation into the next", keeps the old way.
    2. A note where the window was cut: built (2026-11-20). How many messages
       went, and for a task's lead the same document and git state, since the
       copy read earlier may be stale. `metrics.window_cuts`, and the
       transcript's `cuts`.
    3. Repeated replies noticed: built (2026-11-20). Replies whose opening
       content words overlap two of the six before, twice close together,
       are told it is settled and to take the next step (`metrics.repeats`,
       `repeat_notice`). Fired in 11 of 350 earlier runs, nearly all loops.
    4. The document kept current: the preamble asks for milestones in Done
       and no restated situation; a nudge after many tool calls without an
       update; a burst that ends without updating it is sent back once.
    5. Clone a task in the orchestrator: a new task from an old one, its
       prompt and settings changed, starting from the repo alone.
    Measured in local-llm-benchmark: riftwardens' second round, before and
    after, the model fixed.

Left out: Spec Kit's whole workflow (only acceptance criteria, step 5),
approving each tool call (the gateway and the sandbox are the boundary), a
bash-only loop, and benchmarks at SWE-bench's scale.

## Open

- How much workspace snapshotting or automatic WIP commits sessions need beyond
  a persistent volume.
- Whether podman can publish ports from an `--internal` network, or whether
  inbound UI traffic must always enter through the gateway (assumed above).
- zellij web's TLS and token requirements behind a proxy.
- opencode's projects and sessions. `session start` opens the UI on an
  opencode session in the workspace, created through opencode's API, which
  saves adding the directory by hand. Found (opencode 1.18, by its API): a
  project is a git repo's first commit -- its id is that commit's hash, so a
  directory is a project of its own only once its repo has a commit. Anything
  else, an empty workspace or a `git init` with nothing committed, is filed
  under the "global" project. But the web UI's sidebar is none of that: it is
  a list the browser keeps, filled only by adding a project in the UI. Neither
  the server's projects nor a link to a session add to it (checked by hand), so
  nothing the harness does from outside fills it. What it can do is what it
  does: print a link that opens the session, in the right directory.
  Still open: how a session per task, or per branch, should look once the
  orchestrator starts them (a project's records carry a list of
  "sandboxes", not yet looked into).
- Signing in once, instead of to each session's opencode password and zellij
  token: on the cluster, done (2026-10-25, `session_sign_in`). Envoy Gateway
  puts Authentik's sign-in on the session routes with one SecurityPolicy that
  selects them by label (one provider for every session host, with a regex
  redirect URI). It then adds each UI's own login with an HTTPRouteFilter
  (`credentialInjection`) per route, reading a Secret the harness creates:
  opencode's password as basic auth, and for the terminal a zellij session
  cookie the harness logs in for. The UIs keep their logins, which keep one
  session from another. A live test (2026-10-25, with basic auth standing in
  for Authentik) checked that Envoy adds the credential after its sign-in
  check, that a forged header is replaced, and that opencode's event stream
  and zellij's websockets work through it. On podman, sessions are on
  localhost and the local orchestrator has no login, so they keep their
  passwords. Not decided: limiting each session to whoever started it, which
  matters once more than one person uses the cluster.
