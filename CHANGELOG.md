# CHANGELOG

Releases before v0.21.0 were made in the project's earlier, private repository;
their entries are kept without links.

<!-- version list -->

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
