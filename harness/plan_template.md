# <Project> — Plan

<!--
The layout burst-harness reads (docs/project-layout.md). The headings `## Roadmap`, `### M<n> — Title`, and the
labels `Build:`, `Accept:` and `Checkpoint:` are fixed: the harness parses them. Everything else is the project's own.
-->

## Summary

What this is, for whom, and why, in a paragraph. What success looks like.

## Scope

What is in. What is out (out-of-scope ideas go in BACKLOG.md). Scope is closed: what isn't written here isn't built.

## Constraints

Platforms, versions to pin, dependencies allowed or not, performance and size budgets, licences, anything the
agent must not do.

## Design

The architecture and the decisions that shape it: components, data, the main flows, what is deterministic and
tested where. Decisions taken here go in DECISIONS.md as D-001, D-002, ... with why and what else was considered.

## Delivery

- **CI:** what runs on every PR (format, lint, tests, build), and the one command that runs it all locally
  (`just check`, `make check`, ...). That command is the harness's `check_command`.
- **Setup:** the command that gets a fresh checkout ready (dependencies), the harness's `setup_command`.
- **Releases:** how a version is cut and published (tags, a changelog, where builds go).
- **Docs:** what is documented where (README, docs/).

## Roadmap

Milestones in order, each small enough for one PR a person can review. M0 is always the bootstrap: the workspace,
CI running the check command, the release pipeline, and a first thing that runs.

### M0 — Bootstrap
Build: the workspace and toolchain, CI running the check command on every PR, the release pipeline, and the
smallest thing that runs end to end.
Accept:
- M0.1 The check command passes locally and in CI on a clean clone.
- M0.2 <the smallest thing> runs, shown by a test.
- M0.3 A tagged release builds and publishes <the artifact>.
Checkpoint: <a question for the person reviewing, about what they can only see or feel by trying it>

### M1 — <Title>
Build: what this milestone makes.
Accept:
- M1.1 <a criterion something can check: a test that passes, a command and its output, a number within a budget>
- M1.2 <...>

<!--
Accept items: numbered M<n>.<k>, each one checkable, so the agent can tick it with evidence. Write them as outcomes
("A save round-trips through disk, shown by a test"), not tasks ("write the save code"). A person's judgement
(is it fun, is it readable on a phone) goes in Checkpoint, which never blocks.
Changing the plan later: items are struck through (`- ~~M1.2 ...~~`), never deleted, so their ids are never reused;
new items take the next number; a milestone split in two becomes M1a and M1b, keeping its items' ids.
-->
