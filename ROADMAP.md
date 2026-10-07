# Roadmap

Where this is going: replace OpenHands on the cluster with something of our own
that can run both autonomous agents (today's `run`) and interactive ones, with
network access that is controlled and auditable. Local podman stays a
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

- `gateway/` with its own image; `burst-egress-proxy` moves there.
- `--egress log`: everything allowed, everything recorded, to learn what agents
  reach for before writing allowlists.
- Structured JSONL events (time, sandbox, verdict, host, port, bytes), written
  where they outlive the container.
- The gateway answers and logs the sandbox's DNS.

### 2. Spike: traffic that ignores the proxy

Today it is blocked (no route, no DNS) but not logged. Compare, on rootless
podman with capabilities dropped:

- **DNS steering**: the gateway answers every lookup with its own address and
  routes by SNI / Host. Logs anything that resolves a name.
- **Shared network namespace** with a redirect rule, sidecar-style.
- **Staying explicit**, with the DNS log as the only trace.

In the cluster, Calico (open source: no FQDN policy) can log denied packets by
IP and port with a `Log` rule beside default-deny. Pick one approach per
platform and record it here.

### 3. Interception and credential injection

- Gateway CA, trusted by the sandbox image (system store, plus the Node, Python
  and git settings).
- GitHub token injected for the session's repos; then model API credentials for
  in-target engines.
- The harness's own clone, fetch and push go through the gateway, closing the
  token-in-argv exposure noted in the README.

### 4. Sessions

- `opencode` engine; llama.cpp via its OpenAI-compatible endpoint.
- `harness session`: start detached, list, reattach, stop. Optional repo.
- A named volume (a PVC in the cluster) for the workspace and UI state, so a
  crash means restart and reattach. Lifetime and idle reaping carried on
  container / claim labels, since no driver process outlives the command.
- opencode web and, optionally, zellij web, reached through the gateway.
- Root-in-container opt-in for podman.

### 5. Approvals

- Policy file the gateway reloads without a restart; `harness egress approve
  <request-id>` with a scope of once, session, TTL, or permanent.
- In `run`, a blocked host can surface as the existing `handoff_to_user` pause.
- Notifications behind a webhook, so ntfy or similar can be attached later.

### 6. Cluster

- Gateway alongside each claimed sandbox; default-deny egress NetworkPolicy
  allowing only it (home-ops).
- HTTPRoute per session.
- Verify user-namespace pods before allowing root there.

### 7. Control plane (separate project)

API and dashboard over what the earlier phases emit: live event feed, pending
approvals, session list and launch, audit store (SQLite to start). A
mobile-friendly chat front end or a Slack bot belongs here if opencode's own UI
turns out not to be enough. A shared gateway is reconsidered at this point.

## Open

- How much workspace snapshotting or automatic WIP commits sessions need beyond
  a persistent volume.
- Whether podman can publish ports from an `--internal` network, or whether
  inbound UI traffic must always enter through the gateway (assumed above).
- zellij web's TLS and token requirements behind a proxy.
- Cluster Kubernetes, runtime and kernel versions against what
  `hostUsers: false` needs.
