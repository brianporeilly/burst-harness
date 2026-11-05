# Contributing to burst-harness

For people and coding agents alike. README.md says what the harness and the
orchestrator do; ROADMAP.md says where they are going.

## Changes go in through PRs

- Work on a branch from `main` and open a PR: nothing is pushed to `main`
  directly. PRs are squash-merged.
- The PR's title becomes the commit on `main`, and releases are worked out from
  those commits, so the title is a [Conventional
  Commit](https://www.conventionalcommits.org/): `type(scope): summary`, checked
  on each PR (`.github/workflows/pr-title.yml`). Write commit messages on the
  branch the same way.
  - `feat`: something new; a minor release.
  - `fix`, `perf`: a patch release.
  - `docs`, `test`, `refactor`, `build`, `ci`, `chore`, `style`, `revert`: no
    release of their own.
  - `!` after the type (`feat(tasks)!: ...`), or a `BREAKING CHANGE:` footer:
    a major release once the version is 1.0 or more (below it, a minor).
  - The scope is the part changed: `models`, `tasks`, `slack`, `gateway`,
    `orchestrator`, `ci`...
- Don't change the version by hand: the release does (pyproject.toml's
  `[tool.semantic_release]`, `.github/workflows/release.yml`).
- Nothing unfinished is committed: no placeholders, TODO stubs or made-up
  values standing in for real ones.

## Checks

```sh
uv sync --extra openai
uv run pytest -q -n 8          # what CI runs (ci.yml), with -n 4
uvx ruff@0.15.0 check          # lint
```

A change comes with tests for what it changes. Tests make no calls to a real
model: use the fakes in `tests/` (`tests/fake_provider.py`), a local fake
server, or an unreachable URL.

## Provider plugins

Code, docs, tests, commit messages and PR text name no company, gateway host,
or a plugin's environment variables: use generic names (`example-gateway`,
`ai.example.com`, `EXAMPLE_*`).
