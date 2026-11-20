# Projects on rails: the plan, its progress, and the tools that keep them

Status: agreed design, 2026-11-20. ROADMAP.md phase 10, step 10 (items 4 to 8) is built from it.

A project should run as headless as it can. You drop a foundation document (a PRD, an ADR, a brief from a
chat) into a repo, or write a prompt. A setup round turns it into a plan with milestones and acceptance
criteria, and opens a PR. Merging that PR approves the plan. From then on each round works through the
next milestone and ends in a PR. You review and test the PR, then do one of three things:

- merge it, and the next round starts (auto-continue);
- comment and press Continue, and the next burst adjusts the same PR;
- merge it with a message that steers the next round.

The repo carries everything the next session needs: the plan, where the work is, what was decided, and why.
The harness gives the agent tools that write those files in a fixed format, and checks them at the points
that matter. So the agent fills in fields instead of laying out documents, and a reviewer reads the same
shape every time.

graveyard-orbit already works like this by hand: its `PLAN.md`, `PROGRESS.md` and `DECISIONS.md`. This
design makes that layout the standard. The harness can then read it, write it and check it, and it
replaces the harness's own task document (`docs/tasks/<slug>.md`) in repos that have a plan.

## Why

- **One record, not two.** In graveyard-orbit the task document repeats `PROGRESS.md`. Its goal is the round's
  prompt, and its next step paraphrases the plan. Two records drift apart. A stale record pinned in context
  caused the riftwardens loop: the model spent 35 minutes reconciling the wrong goal with the work in front of it.
- **Updating the record has to be cheap.** `update_task_doc` replaces whole sections, so recording one finished
  item means sending all of Done again. Small models skip it or drop items, and `pause_for_review` replaces
  Done with its summary. A tool that adds one entry gets used.
- **Done needs a definition.** Without acceptance criteria the agent can't weigh documentation against finishing,
  and a reviewer can't check either.
- **Context is precious.** graveyard-orbit's agent reads a 674-line plan at every session. It needs the current
  milestone, the rules, and where things stand.

## The files

At the repo root, so an agent sees them first. The names and headings are fixed, because the harness parses
them. Their wording is the project's own.

| File | Who writes it | What it holds |
|---|---|---|
| `PLAN.md` | setup round; then `amend_plan` | The brief: goal, scope, constraints, design, and the roadmap of milestones. The contract. It changes rarely, and only on purpose. |
| `PROGRESS.md` | the harness's tools | Where the work is: the current milestone, each criterion's tick and evidence, blockers, measurements, and a dated log of sessions. |
| `DECISIONS.md` | `record_decision`, `amend_plan` | Append-only, numbered `D-001`, `D-002`, … Choices, deviations and plan changes, each with why and the alternatives. |
| `BACKLOG.md` | `add_backlog` | Ideas and deferred bugs that are out of scope. Never built without a person saying so. |
| `FEEDBACK.md` | you | Optional steering that lasts: it overrides tunables and reorders the current milestone. Agents read it and never write it. |
| `AGENTS.md` | setup round | The repo's own rules for agents (build, test, style). The harness already gives it to the agent (`repo_instructions`). It points at the files above. |

### PLAN.md

```markdown
# <Project> — Plan

(Anything: summary, pillars, design, architecture, CI and release plan, …)

## Roadmap

### M0 — Bootstrap
Build: what this milestone makes, in a paragraph or a list.
Accept:
- M0.1 `just check` passes in CI on a clean clone.
- M0.2 A tagged release builds and publishes the web build.
Checkpoint: Does the title screen load on your phone? (optional; questions for the reviewer)

### M1 — Simulation core
...
```

Parsed:

- **Milestones** are the `###` headings under `## Roadmap`, `M<n>` or `M<n><letter>` then a title.
- **Accept** items are list items with ids `M<n>.<k>`. Each has to be checkable: a test, a command, or
  something to look at. The ids stay put when the plan changes: a new item gets the next number, a removed
  one is struck through, not renumbered.
- **Checkpoint** questions are for the reviewer and never block.

`harness plan check` validates the layout:

- every milestone has at least one Accept item;
- the ids are unique;
- `PROGRESS.md` names a current milestone that exists.

The setup round runs it as its check. `finish_with_pr` runs it after `amend_plan`.

### PROGRESS.md

```markdown
# Progress

Current: M3 — Readouts and feel

## Milestones

- [x] M2 — Flight toy (merged #3)
- [ ] M3 — Readouts and feel
  - [x] M3.1 Focus-body hysteresis tested — `cargo test -p sim focus` (2026-11-21)
  - [ ] M3.2 Each marker's detection tested
- [ ] M4 — Objects and hazards

## Blockers

- CI has never run: the agent cannot see Actions. (since 2026-11-14)

## Measured

| Date | What | Value | Target |
|---|---|---|---|

## Log

### 2026-11-21 14:03Z — M3 · harness/graveyard-orbit-3a48-3
- what was done, one outcome a line
Next: ...
```

The harness writes this file through its tools, so its shape holds. A person may edit it; the parser
tolerates hand edits that keep the headings.

### DECISIONS.md

```markdown
## D-008 — Pe/Ap markers computed in sim, not render (M3, 2026-11-21)

- **What:** ...
- **Why:** ...
- **Alternatives:** ...
```

Plan changes are decisions too, with `Plan change:` before the title. They also record what PLAN.md said before.

## The tools

They replace `read_task_doc` and `update_task_doc` in a repo with a plan. Each writes its own format, so
the agent passes fields and never lays out a file.

| Tool | Does |
|---|---|
| `current_milestone()` | The current milestone's Build, Accept (each item ticked or not, with evidence) and Checkpoint; open blockers; `FEEDBACK.md` if present; the last log entry. What a session needs, not the whole plan. |
| `read_plan(section)` | One section of `PLAN.md` by heading, for the design or rules behind an item. |
| `tick_criterion(id, evidence)` | Ticks an Accept item, with what shows it is met (a test's name, a command and its result). Refused for an unknown id; refused without evidence. |
| `record_progress(done, next, blocker, resolved, measured)` | Adds to this session's log entry (one per burst, made on first use), a blocker, a blocker resolved, or a row of measurements. Any one field, or several. |
| `record_decision(title, what, why, alternatives)` | Adds the next `D-NNN`. |
| `amend_plan(milestone, change, why, kind)` | Edits `PLAN.md` (an item added, split, reworded or struck; a milestone added, split, moved or dropped; the goal) and records a plan-change decision with the old text. `kind` is `item` or `roadmap`. |
| `add_backlog(idea)` | Adds to `BACKLOG.md`. For anything outside the plan. |

The terminal tools stay, and use these files:

- `pause_for_review(summary, next_step)` adds to the session's log entry. It no longer replaces anything.
  In a repo without a plan it adds to the task document's Done (the old behaviour replaced Done, a bug).
- `finish_with_pr(title, body)`:
  - **Checks the milestone.** If Accept items are still unticked, the first call is refused with the list:
    meet them, or say for each why not. The second call goes through, and the reasons go in the PR.
  - **Writes the PR body** from the record: what was met and how it was shown, what wasn't and why, plan
    changes (first, marked for review), this round's decisions, the Checkpoint questions as the
    reviewer's test list, and whether any docs changed. The agent's own body follows.
  - **Advances the milestone.** When every item is ticked, the PR closes the milestone: once it is merged,
    the next round's `current_milestone` is the next one.

`abandon` is unchanged.

A milestone usually takes one round, and several bursts if needed. A milestone too large for one PR is
split with `amend_plan` (M3 → M3a, M3b), a plan change the reviewer sees.

## Changing the plan

Discoveries and spikes change plans. `amend_plan` is how, and the change is always visible:

- **Item changes** (an item added, split or reworded within a milestone) are recorded and listed in the PR.
- **Roadmap changes** (a milestone added, dropped, split or moved; the goal or scope changed) are also listed
  first in the PR and the Slack message, under "Plan changes: review these". After a PR like that is merged,
  auto-continue does not start the next round by itself. You press Continue, which confirms the new direction.
- A spike is a milestone item like any other. Its result is a decision, and the plan is amended to match.

## The session

The harness says how to work. The plan holds what to build. The harness's protocol, given in its system
prompt in a repo with a plan:

1. You start from `current_milestone()`, which is already in your first prompt: the milestone, its criteria,
   blockers, feedback, and the last log entry. `PLAN.md` is the contract; read other sections of it only
   when an item needs them.
2. If `FEEDBACK.md` or the burst's message changes the work, do that first.
3. Take the first unticked item. Build it with its tests. When it is met, `tick_criterion` with the evidence,
   then commit.
4. Record as you go, tersely: a finished step or a measurement with `record_progress`, a choice with
   `record_decision`. The next session has only these files and git.
5. Anything outside the plan goes to `add_backlog`, not into code. If the plan is wrong, `amend_plan` and say why.
6. Keep the docs current (below).
7. End with `finish_with_pr` once the milestone is met, or `pause_for_review` with where it stands.

At the start of a burst and at a window cut, the "where you are" block (handover.py) gives
`current_milestone()` and git, instead of the task document.

## Checks and nudges

- **Sent back once** when a burst ends having recorded nothing (no tick, progress, decision or plan change):
  "record where this stands before you stop." A pause records its summary anyway; this catches the burst
  that worked for an hour and only paused.
- **A nudge** after `record_nudge_calls` tool calls (default 40, 0 for off) with nothing recorded.
- **The review** (`review_with`) is also asked: does each ticked criterion's evidence hold in the diff, does the
  record match the work, and is new behaviour documented?
- **Metrics:** `criteria` `{ticked, total}` for the milestone, and `records` `{progress, decisions,
  plan_changes, backlog}`. The benchmark can then see how well each model balances recording with building.

## Documentation

This is steering, not rails. `docs_style` picks the guidance in the prompt:

- `diataxis` (the default): tutorials, how-to guides, reference and explanation under `docs/`, and a README
  that says what the project is and how to start;
- `none`;
- a path to a file in the repo describing the project's own style.

The protocol asks that a change to behaviour comes with its docs in the same PR. The checks are soft:

- the PR body says whether docs changed;
- the review asks about them;
- one reminder at finish when the diff changes what a user sees (a command, a setting, an API) and no doc or
  README changed. It is not a refusal.

There are no documentation tools.

## Setting up a project

A setup round turns a foundation document or a prompt into the layout. It is a task option ("Set up this repo as
a project") and `harness run --setup`. It is never inferred, so a one-off task in a repo with a stray brief is
not turned into a project.

1. **Input:** the foundation document (a path, or one found under `docs/` named like `*foundation*`, `*prd*`,
   `*brief*`), the prompt, or both. It may be any shape: a chat's PRD is fine.
2. **Output:** `PLAN.md` in the layout above, with M0 as bootstrap (workspace, CI pipeline, release plan),
   and each milestone with checkable Accept items and, where a person is needed, Checkpoint questions. Also:
   - `PROGRESS.md`, with M0 current;
   - `DECISIONS.md`, with the foundation's decisions as `D-001…`;
   - `BACKLOG.md`, with what the foundation left out of scope;
   - `AGENTS.md` (or an added section), with build, test and style, pointing at the files above;
   - `.harness.toml`, with `check_command` and `setup_command`.
3. **The foundation document** is kept in place, with a banner: "Superseded by PLAN.md on <date>; kept as
   the original brief." The protocol leaves it out of sessions. It is the record of what was first intended,
   for a plan change to be weighed against.
4. **The check** is `harness plan check`.
5. **It always ends in a PR**, which is the plan for review. Merging it approves the plan; auto-continue then
   starts M0.

`docs/plan-template.md` (in this repo) is the same layout as a prompt you can paste. A foundation document
written in a chat elsewhere then comes out in this shape from the start, and the setup round has little to do.

## You as the reviewer

- **Merge:** the next round starts at the next milestone (auto-continue), unless the PR changed the roadmap.
- **Comment on the PR and press Continue:** the next burst's first prompt has the PR's review comments and
  unresolved threads (from the forge's API), and the burst adds to the same PR.
- **Merge with a message:** the next round gets your steering as its message.
- **`FEEDBACK.md`:** steering that lasts across rounds.
- **The task's card and the Slack message at a PR** show the milestone, criteria ticked out of the total, plan
  changes, and the Checkpoint questions.

## Repos without a plan

One-off tasks keep the task document (`docs/tasks/<slug>.md`), `read_task_doc` and `update_task_doc`, with the
pause fix. `record_progress` and `record_decision` add to its Done and Decisions there too, so the cheaper tools
work everywhere.

## Migrating

- **graveyard-orbit:**
  - its milestones' Accept prose becomes `M<n>.<k>` items;
  - `PROGRESS.md` gets `Current:` and the criterion lines;
  - `PLAYTEST.md` stays its own, since Checkpoint questions now reach the PR;
  - the task documents under `docs/tasks/` go.

  One PR in that repo, made by hand or by a setup round run on it.
- **riftwardens:** a setup round from its roadmap.

## Order of work

Each is one PR, with its tests, README and ROADMAP:

1. **The layout:** `harness/project.py` parses `PLAN.md` and `PROGRESS.md`, `harness plan check`, and a repo
   counts as a project when it has a valid plan. `record_progress`, `record_decision` and `add_backlog`; the
   pause fix; the "where you are" block from `current_milestone()`; the task document retired in a project.
2. **Milestones:**
   - `current_milestone`, `read_plan`, `tick_criterion`, `amend_plan`;
   - `finish_with_pr`'s criteria check and the PR body it writes;
   - the milestone advancing on merge;
   - `metrics.criteria` and `metrics.records`.
3. **Checks and steering:** the sent-back-once and the nudge; the protocol and `docs_style` in the prompt; the
   docs reminder; the review's brief.
4. **Setup round:** `--setup`, the task option, the foundation banner, and `docs/plan-template.md`.
5. **The review loop:**
   - PR review comments into the next burst;
   - roadmap changes holding auto-continue;
   - milestone progress on the task card and in Slack.
6. **Cloning a task** in the orchestrator (ROADMAP).
7. **Migrations** of graveyard-orbit and riftwardens, in their repos.

## Open

- Claude Code tasks get the layout as instructions: they tick items and amend the plan by editing the files in
  their format, and the harness sees a plan change from git against the base, as it does for any hand edit.
  The tools through the harness's MCP server (as `delegate` reaches it) are left until instructions prove not
  enough.
- Whether `tick_criterion` should run the evidence's command itself, when it is one. Probably not at first: the
  check and the review cover it.
