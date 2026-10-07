# Burst Harness

A contained Strands task runner. Point it at a repo, give it a task, and it works
on a correct branch, records what it did in a committed document, and stops in a
defined state. Work in bursts; a fresh session picks up from the document instead
of re-reading the codebase.

## Usage

```bash
python -m harness --repo /path/to/repo start my-slug --task "What to do"
python -m harness --repo /path/to/repo resume my-slug              # headless burst
python -m harness --repo /path/to/repo run my-slug --task "..."    # start or resume, whichever applies
python -m harness --repo /path/to/repo chat my-slug                # interactive, starts if new
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

### Local containers (`--target podman`)

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
root in the container is your own unprivileged user outside it. What is
installed lasts as long as the container. For a session, `--root` is fixed when
it is created, since its volumes' files are owned accordingly. A container with uncommitted work and no terminal state is
kept; `harness list` shows it and `harness --target podman --docker-container
NAME release` removes it.

**Optional gateway** (`--egress log` or `--egress allowlist`; default `open`).
The container is attached only to its own `--internal` network — no route out —
and a gateway sidecar (`--gateway-image`, built from `gateway/`) is the only way
out. Its proxy variables and its resolver point at the sidecar. On podman the
sidecar is also the network's default route, so software that ignores the proxy
variables lands there all the same.

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

## Sessions

A task runs and ends; a session stays. `harness session` starts a sandbox behind
a gateway with opencode's web UI, for working with an agent by conversation. No
branch, no task document, no terminal state: what happens in it is up to you.

```bash
python -m harness --repo-url https://github.com/me/scratch.git session start my-box
python -m harness session list
python -m harness session stop my-box      # containers go; the workspace stays
python -m harness session start my-box     # ...and comes back where it was
python -m harness session rm my-box        # delete the workspace too
```

`start` prints the UI's address and a password (user `opencode`), and prints them
again if the session is already running. `--repo-url` is optional: without it the
workspace starts empty. podman only, for now.

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
  a llama-server on your LAN included. With the default `anthropic` provider and
  `ANTHROPIC_API_KEY` on the driver, opencode is pointed at `model_id` and given
  the key — or, with `--egress-inject model`, a placeholder that the gateway
  makes real, so the key is never in the session. For anything else, pass
  `--opencode-config my-opencode.json` (used as is; allow its hosts with
  `--egress-allow`), or set a provider up in the UI.
- An allowlist entry may name a port — `--egress-allow llm.lan:8080,...` — to
  allow a host on that port instead of 443.

Not there yet: anything but podman.

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
model_id = "qwen3-coder-30b"          # a label: llama-server serves whatever it loaded
context_window_limit = 65536          # = llama-server --ctx-size; the default 1M
                                      #   would stop context management ever firing
model_params = '{"max_tokens": 8192}' # llama.cpp has no top-level max_tokens
model_timeout = 900                   # seconds; LlamaCppModel's own default is 30s,
                                      #   far shorter than a long prefill
```

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
| `burst-harness-gateway` | `gateway/Containerfile` | The egress gateway sidecar. Standalone: nothing from the harness. |

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
podman run --rm -v "$PWD":/src:Z -w /src --entrypoint sh burst-harness:dev \
  -c 'pip install -q --user -r requirements-dev.txt && python -m pytest -q tests'
```

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
- `agent-sandbox` has been run live through claim, contract check, clone, branch,
  list, and release against agent-sandbox v1.0.4. A full model-driven burst
  through push and PR against a live cluster has not been run yet.
