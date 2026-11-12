# CHANGELOG

Releases before v0.21.0 were made in the project's earlier, private repository;
their entries are kept without links.

<!-- version list -->

## v0.26.0 (2026-11-12)

### Features

- **sandbox**: Rust and Bevy tooling in the sandbox image, docs.rs allowed, longer command timeouts
  ([#10](https://github.com/brianporeilly/burst-harness/pull/10),
  [`10a8e9a`](https://github.com/brianporeilly/burst-harness/commit/10a8e9a5da3b86f175c5624bd03da064bef17414))

- **tasks**: A task's own network, permissive or with hosts added, its refusals approved from the
  page, and a build cache ([#11](https://github.com/brianporeilly/burst-harness/pull/11),
  [`d75517c`](https://github.com/brianporeilly/burst-harness/commit/d75517cf7e9986b198738adac7a931bc12e23d28))


## v0.25.0 (2026-11-12)

### Features

- **git**: Forgejo repos beside GitHub, each forge's token to its own host
  ([#9](https://github.com/brianporeilly/burst-harness/pull/9),
  [`dc2750e`](https://github.com/brianporeilly/burst-harness/commit/dc2750e215d0b6cb083880c340eb6ca89ac743b0))


## v0.24.0 (2026-11-12)

### Features

- **harness**: Strands' read, write and edit tools instead of the deprecated file_editor
  ([#8](https://github.com/brianporeilly/burst-harness/pull/8),
  [`7878d2c`](https://github.com/brianporeilly/burst-harness/commit/7878d2c0bc9487ba08ef804480a70dda59a1fbba))


## v0.23.3 (2026-11-12)

### Bug Fixes

- **deps**: Strands-agents 1.64.0, so mcp is 2.3.0 with its security fixes
  ([#7](https://github.com/brianporeilly/burst-harness/pull/7),
  [`9d1f605`](https://github.com/brianporeilly/burst-harness/commit/9d1f605ff50023cf885ee0b3fc5bccf07e606ebf))


## v0.23.2 (2026-11-12)

### Bug Fixes

- **orchestrator**: Read the server is a POST behind the change header
  ([#6](https://github.com/brianporeilly/burst-harness/pull/6),
  [`2d51a8d`](https://github.com/brianporeilly/burst-harness/commit/2d51a8deeec69b4f11fd04543b6267e143cdb416))


## v0.23.1 (2026-11-12)

### Bug Fixes

- **orchestrator**: A queued task says what it waits for, a task slot or its model server
  ([#5](https://github.com/brianporeilly/burst-harness/pull/5),
  [`d3abe5c`](https://github.com/brianporeilly/burst-harness/commit/d3abe5c62605d01225a975d29cb88c258ea12285))

### Chores

- **deps-dev**: Bump pytest from 8.4.2 to 9.0.3
  ([#4](https://github.com/brianporeilly/burst-harness/pull/4),
  [`45113de`](https://github.com/brianporeilly/burst-harness/commit/45113de5c68b69f41750c5fda42c495b6700c257))


## v0.23.0 (2026-11-12)

### Features

- **mcp**: Basic auth for MCP servers, and keys sent by the engine when there is no gateway
  ([#3](https://github.com/brianporeilly/burst-harness/pull/3),
  [`207f2fc`](https://github.com/brianporeilly/burst-harness/commit/207f2fc3e47a4ce874e638261192a9a985ea0b9c))


## v0.22.0 (2026-11-12)

### Documentation

- **changelog**: Entries from the earlier private repo lose their links
  ([#1](https://github.com/brianporeilly/burst-harness/pull/1),
  [`c4172c5`](https://github.com/brianporeilly/burst-harness/commit/c4172c51e95efe273521bededd4f73e2df0e6926))

### Features

- **tasks**: Turn off Claude Code's attribution in commits and PRs, for a run or every task
  ([#2](https://github.com/brianporeilly/burst-harness/pull/2),
  [`61f81f4`](https://github.com/brianporeilly/burst-harness/commit/61f81f4686c2b50897caffb3e73c87a2ea13b146))


## v0.21.0 (2026-11-12)

### Features

- **tasks**: Rounds, so a merged PR's task carries on with Continue on a fresh branch
  ([`cceac56`](https://github.com/brianporeilly/burst-harness/commit/cceac568370022a418676c0affc984fbd21641cc))


## v0.20.0 (2026-11-12)

### Features

- **sandbox**: Rust in the sandbox image, crates.io allowed, Claude Code telemetry off

- **tasks**: Claude Code tasks run anything without asking, in their sandbox


## v0.19.1 (2026-11-12)

### Bug Fixes

- **orchestrator**: No "null" text in the task, job and run views


## v0.19.0 (2026-11-12)

### Features

- **orchestrator**: Jobs from git repos, set in Settings or ORCHESTRATOR_JOB_REPOS


## v0.18.1 (2026-11-12)

### Bug Fixes

- **jobs**: Price-watch leads with the price to buy one, with shipping

- **jobs**: The mail sorter's apply says each item's count once


## v0.18.0 (2026-11-12)

### Features

- **jobs**: Quiet runs that send nothing when there is nothing to send, and a price-watch example


## v0.17.0 (2026-11-11)

### Features

- **orchestrator**: MCP servers saved in Settings, chosen per session and per task


## v0.16.0 (2026-11-11)

### Features

- **jobs**: A run in act mode is shown what was decided on its earlier plans

- **orchestrator**: A run that names its model waits while its llama.cpp server serves another


## v0.15.0 (2026-11-11)

### Features

- **jobs**: Mail-sorter, an inbox tidied only as far as a person approves


## v0.14.0 (2026-11-11)

### Features

- **orchestrator**: Decide plans in the Jobs tab, the API and Slack, and apply once decided


## v0.13.0 (2026-11-10)

### Features

- **jobs**: Act mode, plans a person approves and an apply command that does only that


## v0.12.0 (2026-11-10)

### Features

- **jobs**: Memory between runs, [job] memory = true


## v0.11.0 (2026-11-10)

### Features

- **orchestrator**: Llama.cpp servers take turns, one run at a time and only once idle


## v0.10.0 (2026-11-09)

### Features

- **orchestrator**: /version, and the page and log say the release and commit running


## v0.9.1 (2026-11-09)

### Bug Fixes

- **orchestrator**: Settings keeps what a job names as a credential, such as an OAuth client id


## v0.9.0 (2026-11-09)

### Features

- **jobs**: A daily-brief example job, today's calendar and the mail that may need you


## v0.8.0 (2026-11-09)

### Features

- **jobs**: Harness oauth --paste, to sign in over ssh with a browser elsewhere


## v0.7.0 (2026-11-09)

### Features

- **jobs**: OAuth refresh-token secrets, and harness oauth to sign in once


## v0.6.0 (2026-11-07)

### Features

- **orchestrator**: Run jobs' runs on the burst launcher, as Kubernetes Jobs on a cluster


## v0.5.0 (2026-11-06)

### Features

- **orchestrator**: Profiles and delegate_to in Settings, and per task


## v0.4.2 (2026-11-06)

### Bug Fixes

- **harness**: A stopped run says Stopped. rather than ending in a traceback


## v0.4.1 (2026-11-06)

### Bug Fixes

- **tasks**: Stop a burst run as a Job mid-tool, as Stop does on podman


## v0.4.0 (2026-11-05)

### Features

- **orchestrator**: Keep its own records apart from the harness's state


## v0.3.0 (2026-11-05)

### Documentation

- **orchestrator**: Design for running each burst as a Kubernetes Job

- **roadmap**: Profiles and delegate_to cannot yet be set per task or in Settings

### Features

- **tasks**: On a cluster, run each burst as a Kubernetes Job

### Refactoring

- **tasks**: Run each burst through orchestrator.burst


## v0.2.0 (2026-11-05)

### Features

- **claude-code**: Claude Code leads and delegates to Strands profiles


## v0.1.2 (2026-11-05)

### Bug Fixes

- **agent**: A SIGINT stops a run at once, not when Strands' loop returns


## v0.1.1 (2026-11-05)

### Bug Fixes

- **tools**: A terminal state ends the run, however often the model calls it


## v0.1.0 (2026-11-05)

- Initial Release
