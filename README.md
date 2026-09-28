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
- **Guardrails.** A `BeforeToolCallEvent` intervention refuses commits on
  protected branches, branch creation, force-pushes to protected branches, and
  `reset --hard` on them. Denials return to the model as correctable errors.
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
| `docker` | `--docker-container` | Work in a running container. For podman, symlink `docker` to `podman`. |
| `kubectl` | `--pod`, `--namespace`, `--container`, `--kube-context` | Work in an existing pod. |
| `agent-sandbox` | `--pool` or `--claim`, `--repo-url`, `--claim-ttl`, `--keep-claim` | Claim a kubernetes-sigs/agent-sandbox sandbox, work in it, release it. |

`--repo-url` clones into the target first (to `--repo`, default
`/workspace/repo` for remote targets); without it the target must already hold a
checkout. For the remote targets `--repo` is the path **inside** the container or
pod, and the agent's tools become `sandbox_shell` / `sandbox_file_editor`, bound
to it. The conversation snapshot stays harness-side under `state_dir` — a pod is
disposable and the snapshot has to outlive it.

### The driver holds every secret

The harness process is the *driver*; the target is compute. Model credentials
never leave the driver. Git credentials don't either: set `HARNESS_GIT_TOKEN`
(or `GH_TOKEN` / `GITHUB_TOKEN`) on the driver and the harness passes it only on
its own clone, fetch, and push (`git -c http.extraHeader=...`, never written to
the repo's config, and scrubbed from any error the model sees). Pull requests are
opened from the driver with `gh pr create --repo`. The target needs no token and
no `gh`; the agent's own `git push` simply has nothing to authenticate with, and
pushes happen through the terminal-state tools.

One residual exposure: the token is in the argv of the harness's git command
while it runs inside the target, so a process the agent left running in the
background could read it from `/proc`. Scope the token to the repos the harness
works on.

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

Two images, both published to GHCR by `.github/workflows/images.yml`:

| Image | Built from | Role |
|---|---|---|
| `burst-harness` | `Dockerfile` | The driver: harness, `git`, `gh`, `kubectl`. |
| `burst-harness-sandbox` | `images/sandbox/Containerfile` | What a SandboxTemplate runs. |

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
