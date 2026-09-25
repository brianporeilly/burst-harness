# Burst Harness

A contained Strands task runner. Point it at a repo, give it a task, and it works
on a correct branch, records what it did in a committed document, and stops in a
defined state. Work in bursts; a fresh session picks up from the document instead
of re-reading the codebase.

## Usage

```bash
python -m harness --repo /path/to/repo start my-slug --task "What to do"
python -m harness --repo /path/to/repo resume my-slug              # headless burst
python -m harness --repo /path/to/repo chat my-slug                # interactive
```

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
  conversation resume) and offloaded tool results.

Resuming on the same machine restores the conversation. A fresh clone anywhere
works from the committed document alone.

## Configuration

Defaults → `.harness.toml` in the repo → `HARNESS_*` environment variables →
CLI flags, in increasing order of specificity. Every field of `HarnessConfig` is
settable from all three; unknown keys and unparseable values are errors, so
typos surface rather than being silently ignored. `HarnessConfig.env_var_names()`
lists them all.

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
| `docker` | `--docker-container` | Work in a container; harness stays outside. |
| `kubectl` | `--pod`, `--namespace`, `--container`, `--kube-context` | Work in a pod; harness stays outside. |
| `sandbox` | `--warmpool` or `--claim`, `--sandbox-connection` | kubernetes-sigs/agent-sandbox, over its runtime API. |

```bash
python -m harness --target kubectl --pod agent-0 --namespace agents \
  --repo /workspace/repo chat my-slug
```

For the remote targets `--repo` is the path **inside** the container or pod, and
the agent's tools become `sandbox_shell` / `sandbox_file_editor`, bound to it. The
conversation snapshot stays harness-side under `state_dir` — a pod is disposable
and the snapshot has to outlive it, so a fresh pod plus the old snapshot resumes
cleanly.

Running the harness outside while executing inside also keeps the model credentials
on the driver: the sandbox never holds them.

### agent-sandbox (`--target sandbox`)

agent-sandbox does **not** expect `kubectl exec`. Each sandbox runs `sandboxd`
(port 8888) behind the Sandbox Router, driven by `k8s-agent-sandbox`
(`pip install k8s-agent-sandbox`). Under Kata or gVisor, exec and port-forward may
be unavailable entirely, so the runtime API is the portable path.

```bash
python -m harness --target sandbox --warmpool python-sandbox-warmpool \
  --repo /workspace/repo start my-slug --task "..."
python -m harness --target sandbox --claim <claim-name> \
  --repo /workspace/repo resume my-slug
```

`--warmpool` claims a fresh sandbox; `--claim` attaches to an existing
SandboxClaim, which is how a resumed burst lands in the same sandbox as the last
one. If your runtime *does* permit exec, `agent_sandbox.pod_name_for_claim()`
returns the backing pod so you can use the simpler `kubectl` target instead.

Two things worth knowing about warm pools. A pooled sandbox is created from a
SandboxTemplate *before* any task exists, so its command is fixed and the task
cannot be baked into it — driving it from outside is the only option, which is what
this target does. And passing `env` at claim time forces a cold start, defeating
the pool, so the harness does not pass any.

Commands go through `commands.run()`, which is not streaming: output arrives when
a command finishes, not as it is produced. The SDK's own 60s default timeout is
raised to 900s (`HARNESS_COMMAND_TIMEOUT`) because test suites exceed it.

## Providers

`--provider` / `HARNESS_PROVIDER`: `anthropic` (default), `openai` (any
OpenAI-compatible endpoint, including llama-server's `/v1`), `bedrock`,
`llamacpp`, `ollama`, `gemini`. `--base-url` overrides the endpoint; optional
providers report the missing extra to install rather than raising an import error.

For an Anthropic-compatible proxy that wants a short-lived bearer token rather than
an API key, set `HARNESS_TOKEN_COMMAND` (stdout is the token, re-run to refresh)
or `HARNESS_TOKEN_FILE` (re-read to refresh), with `--base-url` pointing at it.
JWT expiry is read so the token refreshes before it lapses.

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

```bash
docker build -t burst-harness:dev .
docker run --rm \
  -v /path/to/repo:/workspace/repo -v harness-state:/state \
  -e ANTHROPIC_API_KEY \
  -e HARNESS_PR_DRAFT=true \
  burst-harness:dev --repo /workspace/repo start my-slug --task "..."
```

The CLI is the entrypoint; the subcommand is the `cmd`. Everything else is an
environment variable, so nothing needs baking into the image.

### Keep model and effort stable across a task's bursts

Changing either invalidates the prompt cache. Measured on the same resume: with
`effort` matching the previous burst, 96% of input came from cache ($0.003); with
it changed, zero cache hits ($0.030) — 10x for one env var.

## Caveats

- **No isolation.** Commands run on the host with full privileges. The harness's
  own virtualenv is hidden from the agent's shell (a `pip install` would
  otherwise land in it), but the target repo still needs its own environment.
  Pass `inherit_venv=True` to `RepoEnvironment` to opt out.
- Guardrails are a guardrail, not a sandbox: they parse shell commands and can be
  circumvented by an agent that sets out to. They exist to stop drift, not attack.
- **Changing the tool set, model, or effort invalidates the prompt cache** for an
  existing session, because caching is a prefix match. Keep them stable across a
  task's bursts.
- The `kubectl` target is verified only as far as argv construction and kubectl's
  own parsing; it has not been run against a live cluster.
- The `sandbox` target's own logic is verified end-to-end against a stub matching
  the SDK's interface, backed by a real container. The SDK's HTTP transport to
  `sandboxd` has not been exercised.
