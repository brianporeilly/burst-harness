# Burst Harness

A contained Strands task runner. Point it at a repo, give it a task, and it works
on a correct branch, records what it did in a committed document, and stops in a
defined state. Work in bursts; a fresh session picks up from the document instead
of re-reading the codebase.

## Install

With [uv](https://docs.astral.sh/uv/), from a checkout:

```bash
uv sync                    # Python 3.13 and exactly what uv.lock says, in .venv
uv sync --extra openai     # also the SDK for provider = "openai" (and --extra gemini)
uv run harness --help      # or: source .venv/bin/activate; harness --help
uv run orchestrator        # http://127.0.0.1:8700/
```

Or as tools on your PATH, from anywhere: `uv tool install --editable .`. Below,
`python -m harness` and `harness` are the same thing (likewise the
orchestrator).

### On a Mac, with docker (OrbStack or Docker Desktop)

The harness and the orchestrator run on the Mac itself; sandboxes and their
gateways are docker containers.

```bash
brew install uv gh                # git comes with the Xcode command line tools
uv tool install --editable .      # harness and orchestrator on your PATH
export HARNESS_TARGET=docker      # or target = "docker" in ~/.config/burst-harness/config.toml
orchestrator                      # tasks, jobs and sessions, all in docker
```

What is different from podman:

- **No transparent redirect.** A sandbox's only way out is its gateway's proxy.
  Software that ignores `HTTPS_PROXY` gets nowhere, instead of being caught and
  recorded by the gateway.
- **No `--root`.** Root in a docker container is root on docker's VM, so the
  harness refuses it.
The orchestrator is not packaged as a container. It would need docker's socket,
which is root on the docker host. It also checks sessions' UIs on the host's
loopback, which a container doesn't share.

## Usage

```bash
python -m harness --repo /path/to/repo start my-slug --task "What to do"
python -m harness --repo /path/to/repo resume my-slug              # headless burst
python -m harness --repo /path/to/repo run my-slug --task "..."    # start or resume, whichever applies
python -m harness --repo /path/to/repo chat my-slug                # interactive, starts if new
python -m harness --target podman ask my-q --task "A question"     # no repo: ends in an answer
python -m harness list                                             # claims and Jobs in flight
python -m harness session start my-box                             # a long-lived sandbox with a web UI
```

`run` is idempotent — it starts the task if its branch does not exist yet and
resumes it otherwise — so a CronJob or a button can fire the same command every
time. Exit status: `0` for any terminal state (a PR, a pause, an abandon), `3` when
a headless run stopped on a question for the user, `1` otherwise. The last line of
output is `HARNESS_RESULT {json}` with the slug, branch, state, PR URL, and claim.

Useful flags: `--draft-pr`, `--no-push`, `--model`, `--effort`, `--base-branch`,
`--offload-max-tokens`.

**Questions: tasks with no repo.** `ask SLUG --task QUESTION` is for work
that needs no checkout: a question, a summary, some research. It runs as a
one-off [job](#jobs) does: a fresh sandbox behind a gateway in report mode, so
its network only reads; the model routed afresh unless the run names one (see
[Other agents](#other-agents-routing-and-delegation---router---delegate-to---agent));
and it ends with `finish_report`, whose report is the answer. The answer is
printed, and kept with the run in `asks/SLUG/runs/` under the state directory.
`ask SLUG --message FOLLOW-UP` asks a follow-up: the run is given the earlier
questions and answers (the newest that fit in 60,000 characters) and the
follow-up. Nothing else carries over, because each run's sandbox goes when it
ends. `ask SLUG` alone, after a run that gave no answer, asks the same
question again. Exit status `0` when it was answered; the last line is
`HARNESS_JOB {json}`.

## What is enforced, not prompted

- **The branch.** `start` runs `git fetch` and branches from `<remote>/<base>`
  itself, before the model runs. There is no path by which it branches from a
  stale local ref.
- **Guardrails.** `git_policy.py` refuses commits on protected branches, branch
  creation, force-pushes to protected branches, and `reset --hard` on them.
  Denials return to the model as correctable errors — through a Strands
  intervention, or as a Claude Code `PreToolUse` hook (the same file, uploaded).
- **No stranded work.** A run that ends with a dirty tree and no terminal state
  is re-prompted (up to `max_fixup_attempts`) to commit or pause.

## Terminal states

| Tool | Use when | Result |
|---|---|---|
| `finish_with_pr` | the work is reviewable | commit, push, PR |
| `pause_for_review` | mid-flight, or a decision is needed | commit, push, no PR |
| `abandon` | it should not continue | commit, push, reason recorded |

`handoff_to_user` asks a question mid-task; the run pauses and resumes with the
answer.

## State

- **`docs/tasks/<slug>.md`** — committed. Goal, constraints, done, decisions,
  open questions, next step. **Size-capped**: an oversized write is rejected, not
  truncated, so the handoff document cannot grow into the token problem it exists
  to solve.
- **`~/.burst-harness/<repo>/`** — not in the repo. Strands snapshots (verbatim
  conversation resume) and offloaded tool results. `<repo>` comes from the
  remote (`github.com_owner_repo`), so every sandbox's `/workspace/repo` does not
  collapse into one directory, and a fresh clone of the same repo finds its
  snapshots again.

Resuming with the same state directory restores the conversation. A fresh clone
anywhere works from the committed document alone — which is what makes a sandbox
disposable between bursts.

## Configuration

Defaults → `~/.config/burst-harness/config.toml` → `.harness.toml` in the repo →
`HARNESS_*` environment variables → CLI flags, in increasing order of
specificity. Unknown keys and unparseable values are errors, so typos surface
rather than being silently ignored. `HarnessConfig.env_var_names()` lists them all.

The repo's `.harness.toml` is read from the checkout — after cloning, for remote
targets — and may only set how work on that repo is done (`REPO_SETTABLE` in
`config.py`: model, effort, branches, PR style, doc limits). Driver settings —
provider, `base_url`, token sources, target, `kubectl`, `state_dir` — are refused
there: whoever controls a repo must not be able to run commands on, or redirect
credentials from, the machine driving it. Those go in the user file:

```toml
# ~/.config/burst-harness/config.toml
target = "agent-sandbox"
namespace = "agent-sandbox-system"
pool = "coding-agent"
root_pool = "coding-agent-root"                 # for --root: a hostUsers: false template
```

A repo's own file:

```toml
pr_draft = true
base_branch = "develop"
branch_prefix = "agent/"
protected_branches = ["main", "master", "develop"]
effort = "medium"
doc_max_bytes = 4096
```

## Where work happens

`--target` decides the filesystem the agent and the harness both operate on. This
is one setting, not two, on purpose: if the agent edited files in a pod while
`git commit` ran on your laptop, the harness would commit an unchanged local
directory and the real work would never be recorded.

| Target | Flags | Use |
|---|---|---|
| `local` | — | Work on this machine. |
| `podman` / `docker` | `--image`, `--egress`, or `--docker-container` | Start a fresh container for the run and remove it after, or attach to a running one. |
| `kubectl` | `--pod`, `--namespace`, `--container`, `--kube-context` | Work in an existing pod. |
| `agent-sandbox` | `--pool` or `--claim`, `--repo-url`, `--claim-ttl`, `--keep-claim` | Claim a kubernetes-sigs/agent-sandbox sandbox, work in it, release it. |

`--repo-url` clones into the target first (to `--repo`, default
`/workspace/repo` for remote targets); without it the target must already hold a
checkout. For the remote targets `--repo` is the path **inside** the container or
pod, and the agent's tools become `sandbox_shell` / `sandbox_file_editor`, bound
to it. The conversation snapshot stays harness-side under `state_dir` — a pod is
disposable and the snapshot has to outlive it.

### Local containers (`--target podman` or `docker`)

```bash
python -m harness --target podman --repo-url https://github.com/me/scratch.git \
  run my-slug --task "..."
```

Starts a container from `--image` (default
`ghcr.io/brianporeilly/burst-harness-sandbox:main`) with no host mounts, no
capabilities, and `no-new-privileges`, clones into it, works, and removes it —
the local twin of an agent-sandbox claim. `container_args` adds `run` flags
(`--memory 4g`, ...).

`--root` runs the container as root so the agent can `dnf install` what a task
needs, with only the capabilities that takes (changing file ownership and
switching user; nothing over the network stack or the system) and
`no-new-privileges` still set. It is refused unless podman is rootless, where
root in the container is your own unprivileged user outside it (for sessions
on a cluster, see `root_pool` below). What is
installed lasts as long as the container. For a session, `--root` is fixed when
it is created, since its volumes' files are owned accordingly. A container with uncommitted work, or commits not
pushed, and no terminal state is kept; `harness list` shows it and `harness --target podman --docker-container
NAME release` removes it.

**Optional gateway** (`--egress log` or `--egress allowlist`; default `open`).
The container is attached only to its own `--internal` network — no route out —
and a gateway sidecar (`--gateway-image`, built from `gateway/`) is the only way
out. Its proxy variables and its resolver point at the sidecar. On podman the
sidecar is also the network's default route, so software that ignores the proxy
variables lands there all the same. On docker the container has no route at
all, so such software fails.

| `--egress` | Passes | Use |
|---|---|---|
| `open` | everything, unrecorded | the container's normal network |
| `log` | every TCP connection | see what an agent reaches for before writing an allowlist |
| `allowlist` | TLS to listed hosts on port 443 | hold it to that list |

In `allowlist` the default list is `api.anthropic.com`, GitHub, PyPI, and npm;
`--egress-allow a.com,*.b.com` replaces it, and the repo's host (and
`ANTHROPIC_BASE_URL`'s) are always added. A refusal is an immediate 403 with a
JSON body naming the host, where the protocol allows one. Repos must be cloned
over HTTP(S) in either mode.

Every decision is a JSON event on the sidecar's stdout — `podman logs
<name>-egress` while it runs — and the events are saved to
`<state_dir>/egress/<name>.jsonl` when the container is removed:

```json
{"ts": "2026-10-07T05:23:10Z", "sandbox": "burst-my-slug-b94222", "kind": "connect", "verdict": "allow", "id": "a449cc-3", "peer": "10.89.1.2", "host": "github.com", "port": 443}
{"ts": "2026-10-07T05:23:10Z", "sandbox": "burst-my-slug-b94222", "kind": "connect", "verdict": "close", "id": "a449cc-3", "bytes_up": 1911, "bytes_down": 587427, "ms": 469, "peer": "10.89.1.2", "host": "github.com", "port": 443}
{"ts": "2026-10-07T05:23:12Z", "sandbox": "burst-my-slug-b94222", "kind": "dns", "verdict": "deny", "id": "a449cc-5", "peer": "10.89.1.2", "host": "example.net", "qtype": "A", "detail": "no DNS in the sandbox; this lookup did not go through the proxy"}
```

`kind` is `connect` (an HTTPS tunnel through the proxy: the host is seen, the
contents are not), `http` (plain HTTP, with method and path; the query string is
not recorded), `tls` or `tcp` (see below), or `dns`.

**Software that ignores the proxy** (podman). The sandbox's network routes
everything to the gateway, which redirects it into itself; the sandbox needs no
privileges for this. Such a connection is recorded with `"via": "redirect"`,
identified by its TLS server name or HTTP `Host`, and held to the same policy:

- The gateway is the sandbox's only resolver, and resolves nothing for it. A name
  the policy could allow is answered with the gateway's own address; any other
  does not resolve. So there is no DNS channel out, and every lookup is a `dns`
  event — software that honours the proxy never makes one.
- The gateway connects to the *name* the client gave, never to the address it
  dialled. Claiming an allowed name while dialling some other address reaches
  the real host of that name (the address is recorded as `dialled`).
- A connection to a bare IP address is `tcp`, or `http`/`tls` if it speaks one;
  `log` lets it through, `allowlist` refuses it unless the address is listed.
- UDP other than DNS, and anything that is not TCP, goes nowhere and leaves no
  event.

If the redirect rules are missing, nothing is forwarded: the sandbox fails
closed. The sidecar holds `NET_ADMIN` for those rules, in its own network
namespace; they are installed from outside by `burst-gateway-netinit`, and the
gateway process itself runs unprivileged. On docker there is no way to give the
network a route, so software that ignores the proxy simply has no route: its
lookups are recorded and refused, and a connection to a bare IP leaves no trace.

**Approving what was refused** (`allowlist`). A refusal is immediate — nothing
is held open — and carries an id, in its event and in the 403 the agent sees,
which tells it not to retry and how you can approve it.

```bash
python -m harness egress pending                  # what running gateways have refused
python -m harness egress approve a1b2c3-7         # let that destination through
python -m harness egress approve a1b2c3-7 --scope once | session | 2h | permanent
python -m harness egress allow llm.lan:8080       # for good, without an id
python -m harness egress revoke llm.lan:8080
python -m harness egress list                     # what is allowed for good
```

An approval takes effect on the next request, with nothing restarted, and only
ever adds to the allowlist. `once` is one connection; `session` (the default)
lasts until that gateway stops — for a `harness session`, until you next stop
it; a duration expires; `permanent` is also kept in
`<state_dir>/egress/approved.json`, which every allowlist includes from then on.
When a task's container is removed, the hosts it was refused are listed, since
by then there is no gateway left to approve them on.

`--egress-webhook URL` (or `egress_webhook` in your config) has the gateway POST
each refusal to a URL as JSON — the event, plus the command that approves it. A
destination is reported once every few minutes however often it is retried.

**Credentials the container never holds** (`--egress-inject git,model`, with
`log` or `allowlist`). The gateway terminates TLS to the host a credential is
for and adds it there.

- `git`: the driver's git token, on requests for the repo —
  `/<owner>/<repo>.git/...` and nothing else on the host. `git clone`, `fetch`
  and `push` in the container then just work, the harness's and the agent's
  alike, and the token is in neither the container's environment, its git
  config, nor any command line there.
- `model` (with `--engine claude-code`): `ANTHROPIC_API_KEY`,
  `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_AUTH_TOKEN`. The container is given a
  placeholder for each one set on the driver; on requests to the API's host
  (`api.anthropic.com`, or `ANTHROPIC_BASE_URL`'s) the gateway swaps the real
  credential in for whichever placeholder Claude Code sent, in the header it
  belongs in and nowhere else. A request carrying some other credential is left
  alone.

How it is done:

- The gateway makes a CA when it starts; its key stays in the gateway. The
  harness has the container trust the certificate (`burst-sandbox-trust`, run as
  root from outside) and points `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`,
  `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO` and `NODE_EXTRA_CA_CERTS` at it.
- Only hosts with a credential to add are terminated. Everything else is
  tunnelled untouched, exactly as without `--egress-inject`.
- Each request to a terminated host is a `request` event: method, path, status,
  and the *names* of the headers added.
- A request whose `Host` header names a different host is refused, and a path
  with `..` or encoded separators gets no credential.

This changes what the agent can do: its own `git push` now authenticates, where
without injection only the harness's terminal-state tools could push. The
protected-branch guardrails still apply to what the agent runs, but they are a
guardrail; what bounds a push is the token's scope and the repo's branch
protection.

`log` is for observing, not containing: through the proxy the sandbox can reach
anything the host can, your LAN included — the same reach as `open`.

### The driver holds every secret

The harness process is the *driver*; the target is compute. Model credentials
never leave the driver. Git credentials don't either — or, with
`--egress-inject git` (above), go no further than the gateway. Otherwise: set `HARNESS_GIT_TOKEN`
(or `GH_TOKEN` / `GITHUB_TOKEN`) on the driver and the harness passes it only on
its own clone, fetch, and push (`git -c http.extraHeader=...`, never written to
the repo's config, and scrubbed from any error the model sees). Pull requests are
opened from the driver with `gh pr create --repo`. The target needs no token and
no `gh`; the agent's own `git push` simply has nothing to authenticate with, and
pushes happen through the terminal-state tools.

One residual exposure without `--egress-inject git`: the token is in the argv of
the harness's git command while it runs inside the target, so a process the
agent left running in the background could read it from `/proc`. Scope the token
to the repos the harness works on.

### agent-sandbox (`--target agent-sandbox`)

```bash
python -m harness --target agent-sandbox --namespace agent-sandbox-system \
  --pool coding-agent --repo-url https://github.com/me/scratch.git \
  run my-slug --task "..."
```

Each run:

1. creates a SandboxClaim against the pool, labelled `burst-harness/task=<slug>`
   and annotated with the repo and branch, with a hard deadline
   (`lifecycle.shutdownTime` = now + `--claim-ttl`, default 4h, and
   `shutdownPolicy: Delete`) so a claim whose driver died is reaped by the
   controller rather than lingering with its PVC;
2. waits for it to be Ready and resolves its pod;
3. checks the image provides the sandbox contract (see below);
4. clones, branches or syncs, and runs the burst over `kubectl exec`;
5. deletes the claim — unless the tree is dirty with no terminal state, in which
   case it keeps it and prints `--claim <name>` to resume in the same sandbox.

Nothing is passed into the claim that forces a cold start (`env`,
`additionalPodMetadata`), so claims are served straight from the warm pool.

Exec rather than agent-sandbox's router/`sandboxd` runtime API: exec works under
runc and gVisor alike, needs no daemon in the image, and adds no client SDK.
The driver needs `kubectl` and, per namespace, permission to create/get/list/watch/
delete `sandboxclaims` and to `create` `pods/exec`. In a pod, kubectl uses the
in-cluster ServiceAccount on its own.

**The sandbox contract.** A POSIX `sh`, `git`, and coreutils (`base64`, `mkdir`,
`dirname`, `ls`, `rm`, `cat`, `env`), a writable parent of `--repo`, and a process
that keeps the pod up. `images/sandbox/Containerfile` is a Fedora image meeting
it, plus Python, Node, and a C toolchain for tasks. No credentials, no `gh`.
`--egress-inject` additionally needs `burst-sandbox-trust` (see that script for
what an image of your own has to provide).

**Through the cluster's gateway.** With `gateway_control` set (the URL of a
shared gateway's control API, `EGRESS_CONTROL`; its token in
`HARNESS_GATEWAY_TOKEN` or `gateway_token_file`), each claimed sandbox is
registered with the gateway before anything runs in it: the pod's IP, with what
a podman gateway would be given -- the allowlist (`--egress allowlist`; `open`
and `log` are both recorded), `--egress-inject` credentials, read-only for a
report job, upstream CAs -- and a ttl ending at the claim's deadline. Release
unregisters it and saves its events to `$HARNESS_STATE_DIR/egress/<claim>.jsonl`.
The gateway refuses any address not registered, so Calico must let sandbox pods
reach only its proxy port (and no DNS). The pod template sets `HTTPS_PROXY` and
`HTTP_PROXY` to the gateway, mounts its CA and points `BURST_GATEWAY_CA` at it,
and points `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE` and
`GIT_SSL_CAINFO` at `/tmp/burst-harness/ca-bundle.crt` (`NODE_EXTRA_CA_CERTS` at
the CA itself), which `burst-sandbox-init` writes at start-up from the system's
CAs and that one. Nothing per claim, so the warm pool still serves claims.

## Sessions

A task runs and ends; a session stays. `harness session` starts a sandbox behind
a gateway with an agent you reach from the browser — opencode's web UI, or
(`--agent claude-code`) Claude Code in a terminal in the browser. No branch, no
task document, no terminal state: what happens in it is up to you.

```bash
python -m harness --repo-url https://github.com/me/scratch.git session start my-box
python -m harness session list
python -m harness session stop my-box      # containers go; the workspace stays
python -m harness session start my-box     # ...and comes back where it was
python -m harness session rm my-box        # delete the workspace too
```

`start` prints the UI's address and a password (user `opencode`), and prints them
again if the session is already running. `--repo-url` is optional: without it the
workspace starts empty. On podman, on docker (`--target docker`, with the differences
above), or on a cluster (below).

The address opens on an opencode session in the workspace (the repo's checkout,
with `--repo-url`): the latest one there, or one `start` creates, titled after
the session. Creating it calls no model. Without that, opencode's UI starts
with no project and you add the directory yourself. If opencode will not
answer, `start` says so and prints the plain address.

- **What persists** is two volumes: `/workspace` and the agent's home (opencode's
  conversations and settings). Stopping, or a crash, loses neither; `start`
  brings up new containers on the same volumes, on the same port with the same
  password. Anything installed outside those two directories does not persist.
- **How you reach it.** The sandbox publishes nothing: it has no way in or out but
  its gateway. The gateway listens on the host and passes connections in, each an
  `inbound` event. It listens on `127.0.0.1` unless you say otherwise:
  `--bind 0.0.0.0` (or a LAN address) to reach it from other machines, `--port`
  to choose the port. The connection is plain HTTP behind a password; keep it to
  a network you trust.
- **A terminal in the browser** (`session start my-box --terminal`): zellij's web
  client, on its own port, behind a login token that `start` prints. Optional
  because it is a shell in the sandbox — whoever can log in can do whatever the
  agent can, including use credentials the gateway adds. A session remembers the
  choice; `--no-terminal` turns it off. The token is replaced on every start.
  The link opens straight onto a zellij session (`shell`) already in the
  workspace, rather than zellij's session picker.
- **Stopping by itself** (`--idle 2h`, `--lifetime 1d`). Nothing of the harness
  keeps running after `start`, so the gateway keeps count: it sees everything
  that goes in or out, and exits after that long with no new activity (or that
  long after starting). The sandbox notices within about a minute and exits
  too. The result is the same as `session stop`: `list` shows it stopped,
  `start` brings it back, and the session remembers the setting. A browser tab
  left open on the UI may count as activity for as long as it keeps making
  requests.
- **Egress** is `--egress log` unless you ask for `allowlist`; a session always
  has a gateway, since that is how it is reached. `--egress-inject git` works as
  it does for tasks, and is how the session pushes: there are no terminal-state
  tools here, and the protected-branch guardrails do not apply. Scope the token
  and protect your branches accordingly.
- **The model.** With `provider = "llamacpp"` (or `"openai"`) and `base_url`
  set, the harness writes opencode's config from your model settings, and in
  `allowlist` mode lets the session reach that one host and port — plain HTTP to
  a llama-server on your LAN included. Leave `model_id` unset and opencode is
  given the name of the model the server lists, rather than the harness's
  default Claude model. With the default `anthropic` provider and
  `ANTHROPIC_API_KEY` on the driver, opencode is pointed at `model_id` and given
  the key — or, with `--egress-inject model`, a placeholder that the gateway
  makes real, so the key is never in the session. For anything else, pass
  `--opencode-config my-opencode.json` (used as is; allow its hosts with
  `--egress-allow`), or set a provider up in the UI. An opencode session may
  also reach `models.opencode.ai`, where opencode gets its catalog of models.
- **Claude Code** (`--agent claude-code`): the session's UI is a zellij terminal
  opened on Claude Code, behind a login token. It gets `ANTHROPIC_API_KEY` /
  `CLAUDE_CODE_OAUTH_TOKEN` from the driver — or placeholders, with
  `--egress-inject model` — and with neither you can log in from the terminal.
- **Its own allowlist** (`--allow a.com,llm.lan:8080`): kept with the session
  and added to the defaults each time it starts. Approving a refusal with
  `--scope permanent` adds to this list, not to every sandbox's.
- An allowlist entry may name a port — `--egress-allow llm.lan:8080,...` — to
  allow a host on that port instead of 443.

### On a cluster (`--target agent-sandbox`)

The same commands, on a sandbox claimed from a warm pool and reached through the
cluster's shared gateway. Settings (in your config file, or `HARNESS_*`):

```toml
target = "agent-sandbox"
namespace = "agent-sandbox-system"
pool = "coding-agent"
root_pool = "coding-agent-root"                 # for --root: a hostUsers: false template
# From outside the cluster: a port-forward to the control API, for as long as the
# command runs. In a pod, its Service URL instead.
gateway_control = "kubectl://agent-sandbox-system/burst-gateway:8081"
session_domain = "internal.example.com"          # a wildcard DNS name and certificate
session_route_parent = "network/envoy-internal/https"   # NAMESPACE/GATEWAY/LISTENER
session_route_backend = "burst-gateway:8080"     # the gateway's EGRESS_INBOUND_HTTP (the default)
# On each route. An uptime checker that probes every route would count as the
# session's activity, and --idle would never stop it; this one opts out.
session_route_annotations = ["gatus.home-operations.com/enabled=false"]
# The cluster signs people in on the routes (see "Signed in by the cluster" below).
session_sign_in = true
```

with the control token in `HARNESS_GATEWAY_TOKEN` (`kubectl get secret -n
agent-sandbox-system burst-gateway-control -o jsonpath='{.data.token}' | base64 -d`).

- **A session is a SandboxClaim**, `session-NAME`, labelled
  `burst-harness/session`. It has no deadline unless `--lifetime` gives one
  when it is first claimed (then the controller deletes it).
- **Stop suspends it**: the Sandbox's `operatingMode` becomes `Suspended`, so
  the pod is deleted and the workspace volume kept. `start` sets it back to
  `Running`, and a new pod mounts the same volume. The agent's home is kept at
  `/workspace/.home`, because the pool's template mounts nothing else. `rm`
  deletes the claim, and with it the workspace and the routes.
- **How you reach it**: `https://session-NAME.<session_domain>/`, plus
  `session-NAME-term.` for opencode's terminal. One HTTPRoute per name, owned
  by the claim, sends it to the gateway, which passes each connection to the
  sandbox whose registration holds that name. Every connection is an `inbound`
  event, as on podman. A stopped session's names get a 404.
- **Its record is on the cluster**: a ConfigMap named as the claim and owned by
  it, holding the UI password, host names and settings. A ConfigMap, so that
  reading records doesn't mean reading the namespace's Secrets (in home-ops,
  every namespace holds Flux's decryption key). Any driver of the
  namespace sees the same sessions (the CLI on one machine, the orchestrator on
  another), and `rm` takes the record with the claim. A session started before
  this is read from the local file and moved when it next starts.
- **Signed in by the cluster** (`session_sign_in = true`): for a cluster where
  an Envoy Gateway SecurityPolicy in the namespace signs people in (OIDC, for
  instance) on every HTTPRoute labelled `app.kubernetes.io/managed-by:
  burst-harness`. The UIs keep their own logins, which keep one session from
  another, but you never type them. On each start the harness has Envoy add
  them after its sign-in, through an HTTPRouteFilter (`credentialInjection`)
  per route that reads a Secret owned by the claim. opencode's password goes in
  as basic auth. For the terminal, the harness logs in to zellij's web server
  with the token and passes on the session cookie it gets back, which lasts
  four weeks. The injected values replace whatever the browser sent. The
  orchestrator and `start` then show no password or token. With no such policy
  the UIs would be open to anyone who can reach the ingress, so a session
  won't start without one. The driver creates these Secrets but never reads or
  changes them, so each start makes new ones, and they go when the claim goes.
  Its RBAC needs `create` on `secrets`, `get`/`create`/`patch` on
  `httproutefilters` and `list` on `securitypolicies`.
- **Registered on every start**, running or not, since the pod's address
  changes when it resumes. A gateway with `EGRESS_STATE` (a file on a volume
  of its own) keeps registrations across its restarts; one without forgets
  them, and then `start` is what gives a running session its network back.
- **The model** has to be reachable from the gateway, which the cluster's
  network policy decides, as well as allowed in the registration.
- **`--idle 2h`** goes in the session's registration. The gateway cannot stop
  pods, so it only reports how long each sandbox has done nothing (anything but
  a connection closing counts as something, as on podman), and `harness
  session reap` suspends each session quiet for longer than its `--idle`, as
  `stop` would. Run it every few minutes from something with the driver's
  rights, a CronJob for instance; `start` brings a reaped session back. After a
  gateway restart, idle time is counted from the restart.
- **`--root`** claims from `root_pool` instead of `pool`: a warm pool whose
  template runs pods as root in a user namespace (`hostUsers: false`), so root
  in the sandbox is an unprivileged uid on the node. Without `root_pool` it is
  refused. The claim's pool is which kind of session it is, and starting it
  the other way is refused, as on podman. `dnf install` also needs the
  distribution's mirrors allowed (`--allow`).
- **Not here:** `--port`, `--bind` and `--terminal-port`, which mean nothing on
  a cluster.

The driver needs `kubectl` with permission to create, get, list and patch
`sandboxclaims` and `sandboxes` (and delete `sandboxclaims`: `rm`), to create and
get `pods/exec` (kubectl execs over a WebSocket, a GET), to get, list and
watch `pods` (`stop` waits for the pod to go), to apply `httproutes`, and to
get, list, create and patch `configmaps` (each session's record), all in the
namespace. `session reap` needs only the sandboxclaims, sandboxes and
pods parts, and the gateway's control API. Port-forwarding to the gateway also
needs `get` on `services` and `create` on `pods/portforward` there.

## Orchestrator

A web page for sessions, on this machine: create one with the options above
(agent, repository, allowlist or allow-all-and-record, the session's own
allowlist, credentials at the gateway, model, root, terminal, idle and lifetime),
open its UI, watch its gateway's events live, and approve what it refused —
once, for this run, for two hours, or always.

```bash
uv run orchestrator               # http://127.0.0.1:8700/ (or python -m orchestrator in the venv)
```

It drives the same `harness session` and `harness egress` code, so sessions it
makes are ordinary sessions; it remembers what each was created with, so
starting one again means the same thing. Credentials it can hand on come from
the Settings tab, or else its own environment, as for the harness; the page
shows which are there.

**Settings.** The Settings tab sets credentials in the page instead of the
orchestrator's environment: a git token, `ANTHROPIC_API_KEY`,
`CLAUDE_CODE_OAUTH_TOKEN`, your MCP servers' and jobs' keys, or any name that
looks like a credential. A value set there is used instead of the environment
variable of the same name by everything started afterwards. Clearing it goes
back to the environment's value.

- Values are write-only. The page and `/api/settings/credentials` show where
  each value comes from and who set it when, never the value itself. API
  tokens can't reach it.
- They're stored encrypted (Fernet) in `credentials.enc` in the
  orchestrator's state directory (`ORCHESTRATOR_STATE_DIR`, by default the
  harness's state directory), mode 600. The key is `ORCHESTRATOR_SECRETS_KEY`, or else
  `secrets.key`, which the orchestrator makes beside it. Setting the variable
  keeps the key away from the data; make one with `python -c "from
  cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- A key that doesn't open an existing `credentials.enc` stops the orchestrator
  from starting. It won't run without the credentials or write over them.

**Profiles.** The Settings tab also defines the profiles a task's lead may
hand work to (see "Other agents" below): a name, a model (provider, base URL,
model id and context window, which "Read the server" fills in from a
llama-server), what it's good at, and as a delegate its extra instructions
and whether it is read-only. They're added to the profiles in the
orchestrator's config file, and replace one of the same name. Settings also
picks the delegates for new tasks; unset, they are the config file's
`delegate_to`.

- The task form lists the profiles as "Delegate to", preset to that default.
  A task keeps the names it was made with, so continuing it hands work to the
  same profiles (as they are defined by then). `POST /api/tasks` takes
  `delegate_to` as a list of names; `[]` is no one.
- A burst is given the profiles and its task's delegates in its environment
  (`HARNESS_AGENTS`, as JSON, and `HARNESS_DELEGATE_TO`), so a burst run as a
  Job needs no config file of the orchestrator's.
- They're kept in `profiles.json` in the orchestrator's state directory. They
  aren't secret: a provider's key stays in the credentials. The page can't set
  `token_command` or `token_file`, which run a command or read a file on the
  driver. `/api/settings/profiles` is the API, which API tokens can't reach.

**What each run is handed.** A task's burst and a job's run are subprocesses
(or, with `--bursts job`, Kubernetes Jobs), and each gets the orchestrator's environment with every credential taken out
and only its own put back (`harness/credentials.py`):

- a task: the git token, the model's keys, the cluster gateway's token, and
  its MCP servers' keys;
- a job: the model's keys, the gateway's token, its MCP servers' keys, the
  secrets its `job.toml` names, and Slack's only if it posts there;
- either: what the provider plugins it may use declare (see Provider plugins).

So a task never sees a job's Slack token or the cluster watcher's
`KUBE_TOKEN`. A credential is anything named like one (`*_TOKEN`, `*_KEY`,
`*_SECRET`, `*_PASSWORD`, ...) or listed in that file, except the harness's
own settings. `ORCHESTRATOR_PASS_ENV` (comma-separated names) passes more to
every run, such as the key of a provider plugin that doesn't declare it. Everything that needs a credential
reads it through one function, which per-user credentials will feed later
without the callers changing.

It shows jobs too (see Jobs, below), given where to find them:

```bash
python -m orchestrator --jobs ~/Developer/code/strands-harness-jobs
```

`--jobs` is a job's directory or one of them (repeatable). The Jobs tab lists
each job with its last outcome and what its secrets need from the orchestrator's
environment, runs one on demand (`harness job run`, as a task's burst is run: a
subprocess, or a Kubernetes Job with `--bursts job`), and shows every run: its report, its output, and
the hosts its gateway let through or refused. Runs started from the command
line are there too.

**Schedules.** With `--schedule` (or `ORCHESTRATOR_SCHEDULE=1`), a job whose
`[job] trigger` is a cron schedule (`"0 7 * * 1-5"`) is also run when it's due:

```bash
python -m orchestrator --jobs ~/jobs --schedule --timezone America/Anchorage --catch-up 1h
```

- Times are wall-clock times in `--timezone` (`ORCHESTRATOR_TIMEZONE`; by
  default this machine's), through summer time changes. A time the change
  skips doesn't happen that day, and one it repeats happens once.
- The run log is the only record: a job is run when it's due unless a run of
  it has started since. A restart therefore neither misses a run nor repeats
  one. A time missed by up to `--catch-up` (`ORCHESTRATOR_CATCH_UP`, default
  `1h`) runs late, once. One missed for longer (the machine asleep, the
  orchestrator down) is skipped. A laptop can set a longer `--catch-up`.
- A job still running when it is due again isn't started twice.
- **Pause and resume** a job's schedule from its page in the Jobs tab, which
  shows when it runs next. The pause is kept beside the job's runs
  (`jobs/NAME/schedule.json` in the state directory), not in `job.toml`.
  With `--start-paused` (`ORCHESTRATOR_START_PAUSED=1`), a job starts paused
  and runs on its schedule only once someone resumes it.
- A run's record says what started it: `"trigger": "schedule"` with
  `scheduled_for`, or `"manual"`.

**Tasks.** The Tasks tab (or `POST /api/tasks`) takes a repo and a prompt and
works on it until there is a PR, without anyone watching. With no repo, the
task is a question (see "Questions" under Usage), answered rather than turned
into a PR:

```bash
curl -X POST http://127.0.0.1:8700/api/tasks -H 'X-Orchestrator: 1' -H 'Content-Type: application/json' \
  -d '{"repo": "you/proj", "prompt": "Fix the flaky test in tests/test_api.py"}'
```

- `repo` is `owner/repo` on GitHub, or a repo's https URL. Optional fields:
  `slug` (the branch is `harness/SLUG`; by default from the prompt's first
  words), `engine` (`strands` or `claude-code`), `base_branch` (by
  default the repo's own, which the orchestrator reads with its git token, so
  a repo it cannot read is refused right away), and `draft`.
- The model: `provider`, `model`, `base_url` and `context_window`. A task can
  use any provider the harness has: the built-ins and installed provider
  plugins, listed as `task_providers` in `GET /api/info`. A session's
  opencode takes only `anthropic`, `llamacpp` and `openai`. Empty means the
  orchestrator's own. A different provider from the orchestrator's starts from
  that provider's defaults, so a cluster's llama.cpp URL does not follow a
  task to Anthropic. `llamacpp` and `openai` then need a `base_url`. Claude
  Code is always Anthropic's, with the orchestrator's Claude token or
  Anthropic key. Each task shows what it uses (`uses`: provider, model and
  URL, the defaults filled in).
- Its model settings, all optional, kept for every burst of the task:
  - `effort` (`low` to `max`) and `thinking` (`true` or `false`, adaptive
    thinking) are Anthropic's.
  - `max_tokens` caps one reply, on any provider.
  - `model_params` is a JSON object merged into every request last, for
    what the others don't cover. On llama.cpp that is
    `{"chat_template_kwargs": {"enable_thinking": false}}` for a template
    that reads it (Qwen's, GLM's and others'), and sampling settings such as
    `temperature`. On an OpenAI-compatible endpoint it is
    `{"reasoning_effort": "low"}`.

  In the Tasks form, Model settings shows the fields that apply to the
  provider chosen. The llama.cpp thinking switch and the OpenAI-compatible
  effort are written into the JSON, so what is sent is what is shown.
  **Read the server** asks a llama-server for `/props`, which is its
  description and not a model call. It fills in the context window (per
  slot) and says whether its chat template can switch thinking. That is `GET
  /api/probe?base_url=...`, from the page only. A task given its model or
  any of these settings isn't routed. Claude Code tasks take none of them;
  Claude Code chooses for itself.
- Each burst is `harness run SLUG --task PROMPT --repo-url URL` as a
  subprocess, with the orchestrator's environment: its git token and model
  key, its target, and the task's model settings on top. On a cluster that's a claimed sandbox
  (`--task-ttl`, default `4h`, is how long the claim lives); here it's a fresh
  podman container behind a gateway, never this machine itself.
- A burst ends in a PR, a pause (the work pushed, with what it needs from you
  in the task document on the branch), an abandon, `max_tokens` (the model's
  replies kept hitting the output limit, see llama.cpp below), or a failure. **Continue**
  (`POST /api/tasks/SLUG/continue`, with an optional `{"message": ...}`) is
  another burst, `harness run SLUG --message ...`, from the pushed branch.
  Continuing a task with a PR adds to the PR.
- A burst that fails or is stopped with work not yet pushed keeps its
  sandbox. The next burst carries on in it, and the orchestrator removes it
  once a burst pushes or gives up, or the task is removed.
- A task with no `repo` is a question: each burst is `harness ask SLUG
  --task PROMPT`, and it ends `answered`, with the answer kept with the burst
  (`bursts[].answer` in `GET /api/tasks/SLUG`), or `failed`. Continue with a
  message asks a follow-up. Continue without one, after a burst that gave no
  answer, asks the question again. Follow-ups stay with the profile the router
  picked for the first. `base_branch` and `draft` don't apply. Remove deletes
  its answers too.
- `--task-limit` (`ORCHESTRATOR_TASK_LIMIT`, default 1) tasks run at once; the
  rest wait their turn. The queue is in the tasks' records (`tasks/SLUG/` in
  the orchestrator's state directory), so it survives a restart, and a burst a restart cut
  short is started again, up to twice.
- **Stop** sends the burst Ctrl-C, so the harness cleans up after itself;
  **Remove** forgets the task (not its branch or PR). `--no-tasks` turns tasks
  off.
- On a cluster, `--bursts job` (`ORCHESTRATOR_BURSTS=job`) runs each burst,
  and each job's run (scheduled or not), as a Kubernetes Job of its own
  instead of a subprocess (docs/burst-jobs.md). It has its own memory, and it
  goes on while the orchestrator restarts: a restarted orchestrator follows
  the Jobs still running instead of starting them again. A job's directory is
  copied into its run's, on the shared volume, and run from there, so the pod
  needs none of the orchestrator's mounts for it. Paths a `job.toml` gives
  outside its directory aren't copied. The Job is made in the orchestrator's namespace, from its own
  image. It is given no credentials, only a token good once for fetching
  them from the orchestrator as it starts. Stop deletes the Job, and its pod
  gets 30 seconds to clean up. Settings:
  - `ORCHESTRATOR_BURST_VOLUME` (required): the PersistentVolumeClaim
    (ReadWriteMany) that the state directory is on. Each burst's pod mounts
    it at the same path.
  - `ORCHESTRATOR_STATE_DIR`: keep it off that volume, so bursts can't read
    the orchestrator's own files: `credentials.enc`, `secrets.key`,
    `tokens.json`, and the task and session records. The bursts' output
    stays in `tasks/SLUG/` in the state directory, where they write it.
  - `ORCHESTRATOR_BURST_ACCOUNT`: the bursts' service account, which claims
    and drives sandboxes (default `burst-task`).
  - `ORCHESTRATOR_BURST_MEMORY`: the memory limit (default `2Gi`).
  - `ORCHESTRATOR_BURST_DEADLINE`: the longest a burst can run (default `8h`).
  - `ORCHESTRATOR_BURST_IMAGE`, `ORCHESTRATOR_BURST_NAMESPACE`,
    `ORCHESTRATOR_BURST_HANDOFF_URL`: by default, the orchestrator's own image,
    namespace and pod address.

**API tokens.** For scripts, other machines and bots, make a token in the Settings
tab (`POST /api/tokens` with `{"name": ...}`). It is shown once, and the
orchestrator keeps only its SHA-256 (`tokens.json` in the orchestrator's
state directory, mode 600). Revoke it there.

```bash
curl -H "Authorization: Bearer orch_..." -H 'Content-Type: application/json' \
  http://orchestrator:8700/api/tasks -d '{"repo": "you/proj", "prompt": "..."}'
```

- A token reaches `/api/tasks` and below, and nothing else: not sessions, jobs,
  the page, or tokens, which only someone signed in (or on this machine) can
  make or revoke. A task it starts shows `created_by` as "you via NAME".
- With a token, requests skip the Host check and the `X-Orchestrator` header.
  Both of those stop a web page from using your browser's way in, and a
  browser never sends a token by itself.
- `--listen 0.0.0.0` without `--user-header` serves the page to this machine
  only, and the Tasks API to any machine with a token. That's plain HTTP, so
  use it on a network you trust, or put TLS in front. An SSH tunnel is still
  the way to reach the page from elsewhere.
- On a cluster behind a signing-in proxy, the proxy sends anyone it hasn't
  signed in to log in first, so a token request never arrives. A route for
  `/api/tasks` that skips the sign-in (and lets the token do the checking) is
  that cluster's own configuration.

**Slack.** Tasks can be started from Slack, and the bot reports how they go in
the message's thread. It uses Socket Mode: the orchestrator connects out to
Slack, so nothing has to reach it.

1. Create the app from [`examples/slack-app.yaml`](examples/slack-app.yaml) at
   api.slack.com/apps (Create New App, From a manifest). The file says where
   to find its two tokens.
2. Set `SLACK_APP_TOKEN` (xapp-...) and `SLACK_BOT_TOKEN` (xoxb-...) in the
   Settings tab, or in the environment. The orchestrator connects within 30
   seconds, with no restart, and Settings shows whether it is connected.
3. List who may use it, by Slack member ID: `--slack-user U012ABC`
   (repeatable), or `ORCHESTRATOR_SLACK_USERS`. Anyone else who messages the
   bot is told their own ID and nothing more, which is also how you find
   yours.

Then mention it in a channel it's in, or message it directly:

- `@burst you/proj fix the flaky test in test_api.py` starts a task. The repo
  is owner/repo or its https URL.
- `@burst what are the tradeoffs of SQLite's WAL mode?`, anything without a
  repo first, is a question (see "Questions" under Usage). The answer is posted
  in its thread, and a reply there asks a follow-up.
- `@burst status` lists the latest tasks.

In the task's thread you get each burst's start and how it ended:
- a PR;
- a pause, with its summary, next step and questions;
- the questions it stopped on;
- why it gave up;
- or a failure, with the end of its output.

Reply in the thread to continue the task. In a channel, mention the bot in the
thread; in a direct message, just reply. `status`, `cancel` and `continue`
(another burst, with no reply) also work there.

A task started from Slack is the same as one from the Tasks tab, with
`created_by` set to `slack:<member ID>`.

With `target = "agent-sandbox"` and a cluster session's settings (On a cluster,
above), its sessions are on that cluster instead: claimed, suspended and
removed as `harness session` does there, with events and refusals read from
the shared gateway's control API (polled every 2 seconds) and approvals added
to a session's registration there. An approval lasts until the session is next
started; `always` also adds it to the session's own allowlist, for its next
start. A stopped session's refusals are gone with its registration.

It has no login of its own. By default it answers only to `127.0.0.1` /
`localhost`, and refuses changes that lack a header a page on another site
cannot send.

In a cluster it runs from the harness image (`python -m orchestrator` in place
of the entrypoint) behind a proxy that signs people in, such as Envoy Gateway's
OIDC:

```bash
python -m orchestrator --listen 0.0.0.0 --host orchestrator.example.com --user-header Remote-User
```

`--host` adds the name it is reached by, and `--user-header` names the header
the proxy sets to who signed in. A request without it is refused, and the page
shows who you are. The header proves nothing if anything but the proxy can
reach the pod, so `--listen` beyond this machine needs `--user-header`, and the
pod needs a network policy that admits only the proxy. Its ServiceAccount needs
what a cluster session's CLI does (On a cluster, above), and `/state` should be
a volume: that's where it keeps what each session was created with.

## Jobs

A job is a run with a narrow brief that ends in a report, not a pull request:
a digest of Renovate PRs, a cluster summary. It is a directory, meant to live
in a repo of jobs of your own:

```
my-job/
  job.toml     harness settings (any of the above) and a [job] table
  prompt.md    the brief
  scripts/     optional; copied into the sandbox at /workspace/scripts
```

```bash
harness job check examples/jobs/repo-digest   # what a run would use, what is missing
GITHUB_TOKEN=... ANTHROPIC_API_KEY=... harness job run examples/jobs/repo-digest
```

A run is a fresh podman (or docker) container behind a gateway, with no repo,
and an agent working in `/workspace` from the prompt. In a cluster it is a
claimed agent-sandbox instead (`target = "agent-sandbox"`, `pool`, and
`gateway_control`; see above), with the same gateway settings registered for
it. A claim from a warm pool cannot be given environment variables, so there
every secret needs a `host` for the gateway to add it at. `engine` picks it:

- `strands` (the default): the harness's own loop on the driver, its shell and
  file tools running in the sandbox, ending when the model calls
  `finish_report`. Any provider above (Anthropic, OpenAI-compatible,
  llama.cpp, ...); the model is called from the driver, so its key never
  enters the sandbox and its endpoint needs no allowlist entry.
- `claude-code`: Claude Code headless in the sandbox, which can use a
  subscription's token. It is passed `--model <model_id>`, unless
  `claude_args` has a `--model` of its own. Its model calls go through the gateway (use
  `egress_inject = ["model"]` to keep the key there).
- `opencode`: `opencode run` in the sandbox, with a session's model settings
  (Anthropic, or `llamacpp`/`openai` with `base_url`, which must be on the
  allowlist). Permissions are set to allow its shell and edits: nobody is there
  to answer a prompt.

The last two write `/workspace/report.md`. The harness takes the report and
sends it to each of `[job] output`: `stdout`, `file:PATH` (strftime, so `%Y-%m-%d` works),
`slack:CHANNEL` (chat.postMessage, `SLACK_BOT_TOKEN`) or `slack` (an incoming
webhook, `SLACK_WEBHOOK_URL`). Slack is posted to from the driver, so the
sandbox never holds a token that can post. A run that ends without a report
sends a short notice instead, and exits 1.

`[job.secrets.NAME]` names a credential from the driver's environment
(`env`). With `host`, the gateway adds it to every request to that host (as
`header`, default `Authorization`, with `value`, default `Bearer {}`) and the
sandbox never sees it; `sandbox_env` then gives the sandbox a placeholder, for
tools that insist on a token. Without `host`, it is passed into the sandbox as
`sandbox_env`; either way it is in the container's environment, set by name so
the value is not on a command line. Secrets' hosts are added to the allowlist.
A secret's `host` can carry a port (`host = "10.0.0.5:6443"`), and `ca_file`
names the server's own CA when it is not a public one, for the gateway to
verify it with -- a Kubernetes API server, say. The gateway chooses its
certificate by the host it allowed, so an IP address works.

**OAuth (Gmail, Google Calendar, ...).** With `token_url`, `env` holds a
refresh token. As the run starts, the driver exchanges it there, with the
client's id and secret (`client_id_env`, `client_secret_env`), for an access
token, and that is what the gateway adds. Secrets naming the same refresh
token share one exchange, so a job can read Gmail and Calendar on one sign-in:

```toml
[job.secrets.mail]
env = "GOOGLE_REFRESH_TOKEN"
host = "gmail.googleapis.com"
token_url = "https://oauth2.googleapis.com/token"
client_id_env = "GOOGLE_OAUTH_CLIENT_ID"
client_secret_env = "GOOGLE_OAUTH_CLIENT_SECRET"

[job.secrets.calendar]
env = "GOOGLE_REFRESH_TOKEN"
host = "www.googleapis.com"
token_url = "https://oauth2.googleapis.com/token"
client_id_env = "GOOGLE_OAUTH_CLIENT_ID"
client_secret_env = "GOOGLE_OAUTH_CLIENT_SECRET"
```

An access token lasts about an hour, which a run must finish within. The
refresh token comes from signing in once, on a machine with a browser:

```bash
uv run harness oauth ~/Downloads/client_secret_....json --out refresh-token.txt
```

Over ssh, with no browser on that machine, add `--paste`: open the link it
prints in a browser anywhere, sign in, and paste back the address the browser
ends on (a 127.0.0.1 page that does not load; its code is what is exchanged).

The client is a Google Cloud OAuth client of the "Desktop app" kind, in a
project with the Gmail and Calendar APIs on. The command asks to read Gmail
and Calendar (`--scope` for others), writes the refresh token to the file
(readable by you alone; it never prints it), and you set it, with the
client's id and secret, in the orchestrator's Settings. While the consent
screen is in "Testing", Google expires its refresh tokens after 7 days:
publish it ("In production"; unverified is fine for your own account) to
keep one.

Examples: `examples/jobs/repo-digest` (Renovate PRs, GitHub),
`examples/jobs/cluster-watcher` (what needs attention in a cluster, with a
read-only ServiceAccount from its `rbac.yaml`), and
`examples/jobs/daily-brief` (today's Google Calendar and the Gmail that may
need you, on one sign-in, for a model of your own to read).

Jobs default to `target = "podman"`, `engine = "strands"`, `egress =
"allowlist"`. A job's settings sit between your config file and `HARNESS_*`.
Headless Claude Code runs only the tools it is allowed (`claude_args =
"--allowedTools ..."`, or `claude_settings`). `[job] trigger` (cron or
`manual`) is read by the orchestrator's scheduler, not by `run`; `mode = "act"` is
refused until approvals exist. See ROADMAP.md, phase 8.

`mode = "report"` is enforced by the gateway, not by the prompt. It terminates
all of the sandbox's TLS and lets through only requests that read (GET, HEAD,
OPTIONS); anything else gets a 403 (`"error": "read_only"`), however the
sandbox connects. A job that reads a hostile issue or email still cannot
merge, post or delete. The exceptions are what the run needs -- git's fetch,
and the model endpoint for claude-code and opencode, which call it from the
sandbox -- plus any a job lists:

```toml
[job]
# APIs that read with a POST. GraphQL's mutations are POSTs too, so list one
# only for a token that cannot write.
writes = [{host = "api.github.com", method = "POST", path = "/graphql"}]
```

`harness job check` shows what is allowed.

## Engines

`--engine` picks the agent loop; everything around it — target, clone, branch,
commit, push, PR, release — is the same.

| Engine | Loop runs | Model |
|---|---|---|
| `strands` (default) | on the driver, tools exec into the target | any provider below |
| `claude-code` | inside the target, as `claude` | Anthropic API, or a subscription token |

### Claude Code (`--engine claude-code`)

- **Headless** (`start`, `resume`, `run`): `claude -p` with stream-json output,
  summarised as it goes. Claude ends by writing `.harness/outcome.json` (finish,
  pause, or abandon — the shapes are in its instructions); the harness applies it
  exactly as the Strands terminal tools would, and asks again (up to
  `max_fixup_attempts`) if it is missing or invalid. A failing `claude` (not logged
  in, API down) ends the run rather than being retried.
- **Interactive** (`chat`): `claude` in a tmux session attached to your terminal.
  Detach with `Ctrl-b d` and it keeps running (the target is kept); `chat` again
  reattaches. When the session ends, its outcome is applied, or uncommitted work
  is paused.
- **Permissions are Claude Code's own.** Headless runs use
  `--permission-mode` (default `acceptEdits`: file edits allowed); what else Claude
  may run comes from the settings you supply. The harness never passes a
  permission-bypass flag or answers a prompt for you.
- **Credentials.** `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`),
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_BASE_URL` are copied
  from the driver's environment into a 0600 file in the target for the run and
  removed after. Unlike the Strands engine the loop runs in the target, so the
  credential is there while it runs — unless you pass `--egress-inject model`
  (see the gateway, above), which leaves only placeholders there. The git token
  never is.

**Your Claude Code config.** The sandbox image composes `~/.claude.json` and
`~/.claude/settings.json` at start (`burst-sandbox-init`), deep-merging layers,
lowest first: what is already there → image defaults → a mounted
`/config/claude/{claude.json,settings.json}` (read-only is fine) →
`CLAUDE_JSON` / `CLAUDE_SETTINGS_JSON` env → files the harness uploads from
`--claude-json` / `--claude-settings`. Objects merge key by key, so a file only
needs the keys it changes:

```jsonc
// my-settings.json → --claude-settings my-settings.json
{ "permissions": { "allow": ["Bash(npm test:*)", "Bash(pytest:*)", "Bash(make:*)"] } }
```

The image default sets `hasCompletedOnboarding`: without it, interactive `claude`
runs first-run onboarding even with `CLAUDE_CODE_OAUTH_TOKEN` set, which looks like
the token being ignored. On the `local` target none of this applies — Claude runs
as you, with your own `~/.claude`.

### MCP servers (`--mcp NAME=URL`)

The agent can connect to remote MCP servers over HTTPS, such as Context7's
library docs. Every engine and session gets them, each in its own way:

```toml
# ~/.config/burst-harness/config.toml, a job.toml, or HARNESS_MCP (comma-separated)
mcp = ["context7=https://mcp.context7.com/mcp"]
```

- **opencode** (sessions and jobs): the `mcp` section of the `opencode.json` the
  harness writes (not one you pass with `--opencode-config`).
- **Claude Code:** `--mcp-config` on every run, with the servers' tools allowed
  (`--allowedTools mcp__NAME`), because a headless run has nobody to approve
  them. A Claude Code session gets them in `~/.claude.json`, and asks as usual.
- **strands:** Strands' `MCPClient`, from the driver, the same place the model
  is called from. A server that is down leaves its tools out instead of
  ending the run.

opencode and Claude Code connect from the sandbox, so each server's host is
added to the allowlist. A report-only job may send POSTs (MCP's requests) to
the server's URL and nowhere else on that host. **Keys:** a key in the driver's
`<NAME>_API_KEY` (`CONTEXT7_API_KEY`) is sent as `Authorization: Bearer`. For
the sandbox engines the gateway adds it, so the sandbox never holds it. Without
a gateway (`--egress open`), the server is used without the key. The
orchestrator's form lists its own servers, which you can change for each
session, and shows which keys it holds.

With `--engine strands` only, a server on the driver's loopback may be plain
`http://` (`127.0.0.1`, `::1` or `localhost`), such as a local test server:
`planner=http://127.0.0.1:8931/mcp`. MCPClient reaches it from the driver,
with no gateway in between. The sandbox engines need `https://`, because the
sandbox reaches the server through the gateway.

### Other agents: routing and delegation (`--router`, `--delegate-to`, `--agent`)

Name your models once, as `[agents.NAME]` profiles in a config file (yours, a
`--config` file, or a job.toml; never a repo's `.harness.toml`), or in the
orchestrator's Settings tab ("Profiles", above). Each profile
takes the model settings (`provider`, `base_url`, `model_id`,
`context_window_limit`, `effort`, `model_params`...) and a `use_for` line
saying what it is good at. A profile is a whole model: what it leaves out is
the default, not the top-level setting, so a hosted profile never picks up a
local `base_url`.

```toml
[agents.local-coder]
provider = "llamacpp"
base_url = "http://typhon:8080"
model_id = "tiel-coder"
context_window_limit = 262144
use_for = "Coding in one repository. Free and private, slower than Claude."

[agents.claude-quick]
provider = "anthropic"
model_id = "claude-haiku-5-5"
use_for = "Fast, cheap hosted work: reading code to answer a question, small edits."
```

There are three ways to use them, and they combine:

- **`agent = "NAME"`** (`--agent`): the run uses that profile instead of the
  top-level model.
- **`delegate_to = [...]`** (`--delegate-to a,b`): the lead gets a `delegate`
  tool and hands pieces of work to those profiles. Each piece runs as a fresh
  agent with the same sandbox, tools and git guardrails, but no task tools, so
  it doesn't commit. Only its final report comes back, so the lead's context
  stays small. Delegates run one at a time, because they share the checkout,
  and never delegate further. A profile's own `delegate_to` applies when it
  leads.

  With `--engine claude-code`, Claude Code is the lead, in headless runs
  (`run`, `start`, `resume`, jobs) but not `chat`. The harness offers it the
  `delegate` tool as an MCP server in the driver (the Claude Agent SDK's
  in-process server). Its calls come back over `claude`'s own stdin and
  stdout, through the same `podman exec` or `kubectl exec` that runs it.
  Nothing listens in the sandbox, and no new credential goes in. Each delegate
  runs on the driver as a Strands lead's does: the profile's model is called
  from there, and its tools run in the sandbox. Claude Code commits as usual,
  and the burst report adds each delegate's token use to Claude Code's own.

  Two profile settings shape a delegate whatever the lead's brief says.
  `prompt` is added to its instructions, so a role keeps its stance.
  `read_only = true` takes away its file editor and tells it to change
  nothing, for a reviewer or a tester that should report rather than quietly
  fix. It keeps its shell, to run tests, which can still write files. So the
  harness compares the checkout before and after, and if the delegate changed
  anything, the lead's report ends with the files it changed. Nothing is
  undone. The comparison needs a git checkout, so a job's working directory
  is not checked.

  ```toml
  [agents.reviewer]
  provider = "anthropic"
  model_id = "claude-haiku-5-5"
  use_for = "Reviewing a change before it is finished."
  prompt = "Review adversarially: find bugs, missed requirements and claims the work doesn't back up. Report them; don't fix anything."
  read_only = true
  ```
- **`router = "NAME"`** (`--router`): the front door. Before a new task's
  first burst, or before each job run, that profile reads the prompt and picks
  who does the work from `route_to` (all other profiles when empty). Jobs are
  routed too, so non-coding work (reports, summaries, triage) can go to a small
  local model and hard work to Claude. A task keeps its choice, so later bursts
  use the same model and its prompt cache. A run that names its model
  (`--provider`, `--model`, `--base-url`, `--agent`, or a task given a model in
  the orchestrator) isn't routed. Neither is `--engine claude-code`. The router
  gets one try; when it is down or names no profile, the top-level model does
  the work. It is told it never does a task itself, and gets the roster, then
  the task in `<task>` tags, then the question: a small model given the task
  alone tends to do it. A reply cut off at `max_tokens` still routes when it
  named a profile first. Turn a router's thinking off (for llama.cpp,
  `model_params = { chat_template_kwargs = { enable_thinking = false } }`):
  it is much faster, and in benchmarks a small router was no less accurate.
  Left on, a router can think for thousands of tokens, so its `max_tokens`
  has to allow for that.

`examples/agents/` has profiles and scenarios to layer over them:
`front-door`, `strong-lead` (Claude leads, local models do the bulk),
`small-lead` (the small model leads) and `local-only`. `--config` is
repeatable, and later files win:

```sh
harness --config examples/agents/profiles.toml --config examples/agents/front-door.toml \
  --repo-url https://github.com/me/proj.git --target podman start fix-flake --task "..."
```

Before a run starts, and before any sandbox, the harness checks that every
profile the run may call can load its provider here: the lead (`--agent`, or
the router and what it picks from) and whoever a possible lead delegates to.
A hosted profile on `provider = "openai"` needs the `openai` extra, and
without it the run stops with that profile's name and the missing package.
Otherwise every delegation to it would fail and the lead would carry on
alone. Profiles that are only defined aren't checked. With `--engine
claude-code`, only the profiles it delegates to are checked, since it is never
routed.

`HARNESS_CONFIG` is the same list (os.pathsep-separated), and `HARNESS_AGENTS`
takes the profiles as JSON. That is how the orchestrator's bursts get them:
set either in its environment, along with `HARNESS_ROUTER`. The burst report
gives each delegate's token use separately. `HARNESS_RESULT` (and a job's
`HARNESS_JOB`) gives the model that led, the profile (`agent`) and the
delegations, and the Tasks tab shows the routed profile, so scenarios can be
compared.

## Providers

`--provider` / `HARNESS_PROVIDER`: `anthropic` (default), `openai` (any
OpenAI-compatible endpoint, including llama-server's `/v1`), `bedrock`,
`llamacpp`, `ollama`, `gemini`. `--base-url` overrides the endpoint; optional
providers report the missing extra to install rather than raising an import error.

For an Anthropic-compatible proxy that wants a short-lived bearer token rather than
an API key, set `HARNESS_TOKEN_COMMAND` (stdout is the token, re-run to refresh)
or `HARNESS_TOKEN_FILE` (re-read to refresh), with `--base-url` pointing at it.
JWT expiry is read so the token refreshes before it lapses.

### llama.cpp

`llamacpp` talks to llama-server's `/v1/chat/completions`; point `base_url` at the
server root (no `/v1`). Effort, thinking, and prompt caching are Anthropic
features and are not sent. Four settings matter:

```toml
# ~/.config/burst-harness/config.toml  (or HARNESS_* env vars of the same names)
provider = "llamacpp"
base_url = "http://llm.example.lan:8080"
model_id = "qwen3-coder-30b"          # a label: llama-server serves whatever it loaded;
                                      #   unset, the name the server lists is sent and shown
context_window_limit = 65536          # = llama-server --ctx-size; the default 1M
                                      #   would stop context management ever firing
model_params = '{"max_tokens": 8192}' # one reply's cap; by default max_tokens,
                                      #   at most half the window, is sent here
model_timeout = 900                   # seconds; LlamaCppModel's own default is 30s,
                                      #   far shorter than a long prefill
```

`model_timeout` is the longest wait for the *next* byte, not for the whole
response: a reply that keeps streaming is never cut, and one that goes silent
that long (a huge prefill, or reasoning the server does not stream) is. A
timed-out call is tried again `model_retries` times (default 2; 0 gives up at
once). A server that goes away -- the reply cut off, connections refused, then
503 while it loads the model again, which is what a crashed llama-server under
`--restart=always` looks like -- is backed off and retried for up to about eight
minutes (4s, 8s, 16s ... 240s), as are rate limits. Each retry is logged.

**Replies are capped and a cut-off one is recovered from.** Strands has no
`max_tokens` setting for llama.cpp or OpenAI-compatible servers. So the harness
sends `max_tokens` in the request as a param, the smaller of `max_tokens` and
half of `context_window_limit`, unless `model_params` already sets
`max_tokens`, `max_completion_tokens` or `n_predict`. Without a cap, a model
that loops in its reasoning keeps going until the server's own limit, if there
is one. When a reply is cut off at the limit, it is dropped from the
conversation (a model shown its own cut-off reasoning tends to carry on with
it), and the model is told why and asked to take the next step with a tool
call. This happens up to twice in a row; the count starts again after a reply
that finishes. A third cut-off in a row stops the run: `HARNESS_RESULT` state
`max_tokens`, exit status 1, with any work that was not pushed kept in the
sandbox. For a job, it is a run with no report.

Every key in `model_params` reaches llama-server, including those Strands does
not know (it forwards only its own list): `chat_template_kwargs`
(`{"enable_thinking": false}`), `n_predict`, `reasoning_format`, `dry_multiplier`
and the rest. Those are named once in the log when first sent. In a TOML file,
`model_params` may be a table rather than a JSON string:
`model_params = { chat_template_kwargs = { enable_thinking = false } }`.

Keep `window_size` (messages kept, default 200) large. Prompt caching is a
prefix match: whenever old messages are cut, the next call re-reads everything
after the cut. So the harness cuts back to half the window at once, not one
message per call, and the prefix holds until the next cut. With the old
default of 40, cut one message at a time, re-reading the prompt took 55-78% of
the wall time on long local runs. With a large window it took 15-17%, and the
cache served up to 97% of input. The same applies to Anthropic's prompt cache,
where re-read input is billed at the uncached rate. `context_window_limit` still
bounds the size: past 70% of it, the conversation is halved the same way.

The server must run with `--jinja` for tool calls, and the model has to be able to
close a multi-step tool loop — small instruct models often cannot.

### Provider plugins

Anything more involved — an OAuth grant, a vendor CLI, a custom client — is a
plugin rather than a change here. A plugin is a callable taking `HarnessConfig`
and returning a Strands `Model`. Select it by `module:function`, or install a
package that registers it under the `burst_harness.providers` entry-point group
and select it by name:

```toml
[project.entry-points."burst_harness.providers"]
myproxy = "myproxy_harness:build"
```

```python
from strands.models.anthropic import AnthropicModel
from harness.models import model_kwargs

def build(cfg):
    return AnthropicModel(
        client_args={"base_url": "https://proxy.example/anthropic", "credentials": my_token},
        **model_kwargs(cfg, AnthropicModel, messages_api=True),
    )
```

`model_kwargs` gives a plugin the same schema fitting, effort/thinking, and prompt
caching as the built-ins. Built-in names win over plugin names.

A plugin can say more about itself with optional attributes on the callable.
A plain function needs none:

- `build.credentials = ("EXAMPLE_CLIENT_ID", "EXAMPLE_CLIENT_SECRET")`: the
  credentials it reads, through `harness.credentials.get`. The orchestrator
  hands each run only the credentials it's granted. These are granted to a
  task or job that may use the provider: its own provider, or any profile's
  (a router, a delegation or `--agent` may pick one). That includes a task
  given the provider in the form while the orchestrator's default is another.
  They're listed in the Settings tab, which can store them even when the name
  doesn't look secret.
- `build.target_endpoint = lambda cfg: TargetEndpoint(base_url=..., token=...)`
  (`harness.models.TargetEndpoint`): where Claude Code sends its model calls
  when it runs in the target (`--engine claude-code` on a container or a
  claimed sandbox). `token` has the shape of an Anthropic `credentials=`
  provider: it returns an `AccessToken`, whose `expires_at` says when to ask
  again. `env` holds extra settings for the target, and nothing secret.
  - Claude Code then gets the endpoint as `ANTHROPIC_BASE_URL` and none of the
    driver's Anthropic settings. That matters because a developer's shell
    often has its own.
  - The token is in a 0600 file that Claude Code reads with `apiKeyHelper`,
    every five minutes and after a 401. While Claude Code runs, the driver
    asks the plugin for the token again at half its remaining life, and at
    least once a minute, and rewrites the file when it changes. So an
    hour-long token lasts a four-hour burst. The plugin's `token` must be
    safe to call from another thread.
  - The gateway lets the sandbox reach the endpoint, and a job's report mode
    lets it POST there.
  - In the orchestrator, a Claude Code task keeps such a default provider
    instead of switching to Anthropic.
  - Not yet: `egress_inject = model` with it (refused), and a session's
    opencode.

Provider config schemas differ, and Strands only *warns* about keys a provider
doesn't accept before ignoring them. So the harness fits kwargs to each provider's
own schema: `effort` and adaptive thinking are Anthropic Messages API features and
are only sent to Messages-API providers; Bedrock takes `temperature` at the
top level while llama.cpp takes it under `params`. `HARNESS_MODEL_PARAMS` is a raw
JSON escape hatch for anything not modelled, merged last and routed to the right
place. Anything a provider cannot accept is logged, not silently dropped.

## Containers

Three images, all published to GHCR by `.github/workflows/images.yml`:

| Image | Built from | Role |
|---|---|---|
| `burst-harness` | `Dockerfile` | The driver: harness, `git`, `gh`, `kubectl`. |
| `burst-harness-sandbox` | `images/sandbox/Containerfile` | What a SandboxTemplate runs. |
| `burst-harness-gateway` | `gateway/Containerfile` | The egress gateway: a sidecar per sandbox on podman, or one shared by a namespace's sandboxes in a cluster (`EGRESS_CONTROL`; see the top of `gateway/burst_gateway.py`). Standalone: nothing from the harness. |

Each is tagged `sha-<commit>` and `main` on every merge to main, and `X.Y.Z` on
a release (`.github/workflows/release.yml`; see AGENTS.md for how a version is
worked out). Pin a release as `:X.Y.Z@sha256:...`.

Locally with podman, the simplest mode makes the container itself the sandbox
(target `local` inside it; your key is in the container, which is fine on your own
machine):

```bash
podman build -t burst-harness:dev .
podman run --rm -it --userns=keep-id \
  -v /path/to/repo:/workspace/repo:Z -v harness-state:/state \
  -e ANTHROPIC_API_KEY -e GH_TOKEN \
  burst-harness:dev --repo /workspace/repo chat my-slug
```

In a cluster, run the driver image as a Job with a PVC at `/state` and
`--target agent-sandbox`. The CLI is the entrypoint; the subcommand is the `cmd`.
Everything else is an environment variable, so nothing needs baking into the image.

## Tests

```bash
uv run pytest -q -n 8      # the dev group (pytest, pytest-xdist) comes with uv sync
```

Dependencies are pinned in `pyproject.toml` and locked in `uv.lock`; change
them with `uv add`/`uv lock --upgrade-package NAME`, and commit the lock. The
image installs from the lock too (`uv sync --frozen`), with the `openai` extra.

### Keep model and effort stable across a task's bursts

Changing either invalidates the prompt cache. Measured on the same resume: with
`effort` matching the previous burst, 96% of input came from cache ($0.003); with
it changed, zero cache hits ($0.030) — 10x for one env var.

## Caveats

- **The `local` target runs on the host with full privileges**; the others are only
  as isolated as the container or pod they exec into. The harness's
  own virtualenv is hidden from the agent's shell (a `pip install` would
  otherwise land in it), but the target repo still needs its own environment.
  Pass `inherit_venv=True` to `RepoEnvironment` to opt out.
- Guardrails are a guardrail, not a sandbox: they parse shell commands and can be
  circumvented by an agent that sets out to. They exist to stop drift, not attack.
- **Changing the tool set, model, or effort invalidates the prompt cache** for an
  existing session, because caching is a prefix match. Keep them stable across a
  task's bursts.
- **A small `window_size` defeats prompt caching**: each cut changes the prefix.
  The harness cuts in chunks (back to half the window), but a window of a few
  dozen messages still cuts often on a long run.
- `agent-sandbox` has been run live through claim, contract check, clone, branch,
  list, and release against agent-sandbox v1.0.4. A full model-driven burst
  through push and PR against a live cluster has not been run yet.
