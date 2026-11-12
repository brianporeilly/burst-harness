# CHANGELOG

<!-- version list -->

## v0.18.1 (2026-11-12)

### Bug Fixes

- **jobs**: Price-watch leads with the price to buy one, with shipping
  ([#100](https://github.com/brianporeilly/strands-harness/pull/100),
  [`4399031`](https://github.com/brianporeilly/strands-harness/commit/4399031bfbe3e830ac36ca9b115b4061624c1bc5))

- **jobs**: The mail sorter's apply says each item's count once
  ([#99](https://github.com/brianporeilly/strands-harness/pull/99),
  [`ddee27e`](https://github.com/brianporeilly/strands-harness/commit/ddee27e4ade2a88ce0ae42fcd75a2bed3e330e21))


## v0.18.0 (2026-11-12)

### Features

- **jobs**: Quiet runs that send nothing when there is nothing to send, and a price-watch example
  ([#98](https://github.com/brianporeilly/strands-harness/pull/98),
  [`2bbeb01`](https://github.com/brianporeilly/strands-harness/commit/2bbeb013dee80e66bc7f8de8d888b4b34b56ec76))


## v0.17.0 (2026-11-11)

### Features

- **orchestrator**: MCP servers saved in Settings, chosen per session and per task
  ([#97](https://github.com/brianporeilly/strands-harness/pull/97),
  [`be73fb4`](https://github.com/brianporeilly/strands-harness/commit/be73fb4e2593dcb9a5a10141361a9c7efd747443))


## v0.16.0 (2026-11-11)

### Features

- **jobs**: A run in act mode is shown what was decided on its earlier plans
  ([#96](https://github.com/brianporeilly/strands-harness/pull/96),
  [`5497916`](https://github.com/brianporeilly/strands-harness/commit/5497916160cb4c8b8789172b05c6f081dba75bb0))

- **orchestrator**: A run that names its model waits while its llama.cpp server serves another
  ([#95](https://github.com/brianporeilly/strands-harness/pull/95),
  [`68f4520`](https://github.com/brianporeilly/strands-harness/commit/68f4520877d21c15a398f69bea2e86599fd45194))


## v0.15.0 (2026-11-11)

### Features

- **jobs**: Mail-sorter, an inbox tidied only as far as a person approves
  ([#94](https://github.com/brianporeilly/strands-harness/pull/94),
  [`1716586`](https://github.com/brianporeilly/strands-harness/commit/17165861d3b33811de10bf214029050b83f266cd))


## v0.14.0 (2026-11-11)

### Features

- **orchestrator**: Decide plans in the Jobs tab, the API and Slack, and apply once decided
  ([#93](https://github.com/brianporeilly/strands-harness/pull/93),
  [`8d0c146`](https://github.com/brianporeilly/strands-harness/commit/8d0c14687cff99e0094b6d800e07b4ad7458e2e6))


## v0.13.0 (2026-11-10)

### Features

- **jobs**: Act mode, plans a person approves and an apply command that does only that
  ([#92](https://github.com/brianporeilly/strands-harness/pull/92),
  [`f0028e7`](https://github.com/brianporeilly/strands-harness/commit/f0028e7e43f698948685202cf74e7d2502fed708))


## v0.12.0 (2026-11-10)

### Features

- **jobs**: Memory between runs, [job] memory = true
  ([#91](https://github.com/brianporeilly/strands-harness/pull/91),
  [`e894f53`](https://github.com/brianporeilly/strands-harness/commit/e894f53d8e0f1c037d6080b2a79eb0defd1ca5da))


## v0.11.0 (2026-11-10)

### Features

- **orchestrator**: Llama.cpp servers take turns, one run at a time and only once idle
  ([#90](https://github.com/brianporeilly/strands-harness/pull/90),
  [`d883a6f`](https://github.com/brianporeilly/strands-harness/commit/d883a6ff468697797829be781633d4168950c4df))


## v0.10.0 (2026-11-09)

### Features

- **orchestrator**: /version, and the page and log say the release and commit running
  ([#89](https://github.com/brianporeilly/strands-harness/pull/89),
  [`7f09556`](https://github.com/brianporeilly/strands-harness/commit/7f09556e1a157fbe1ef8ed44946791c9cea21c99))


## v0.9.1 (2026-11-09)

### Bug Fixes

- **orchestrator**: Settings keeps what a job names as a credential, such as an OAuth client id
  ([#88](https://github.com/brianporeilly/strands-harness/pull/88),
  [`ffd2663`](https://github.com/brianporeilly/strands-harness/commit/ffd266364335ca7586d9bacefbda570de8f320c1))


## v0.9.0 (2026-11-09)

### Features

- **jobs**: A daily-brief example job, today's calendar and the mail that may need you
  ([#86](https://github.com/brianporeilly/strands-harness/pull/86),
  [`33e08c6`](https://github.com/brianporeilly/strands-harness/commit/33e08c638b2f9e0dbfa4f345a54591404636fec5))


## v0.8.0 (2026-11-09)

### Features

- **jobs**: Harness oauth --paste, to sign in over ssh with a browser elsewhere
  ([#87](https://github.com/brianporeilly/strands-harness/pull/87),
  [`9172c15`](https://github.com/brianporeilly/strands-harness/commit/9172c158f4f50aa7bb907a8a0c2dcc8f596211b9))


## v0.7.0 (2026-11-09)

### Features

- **jobs**: OAuth refresh-token secrets, and harness oauth to sign in once
  ([#85](https://github.com/brianporeilly/strands-harness/pull/85),
  [`3a34dbc`](https://github.com/brianporeilly/strands-harness/commit/3a34dbc90e4dbedd0900fc61bd4b8162b799fc80))


## v0.6.0 (2026-11-07)

### Features

- **orchestrator**: Run jobs' runs on the burst launcher, as Kubernetes Jobs on a cluster
  ([#84](https://github.com/brianporeilly/strands-harness/pull/84),
  [`04125fe`](https://github.com/brianporeilly/strands-harness/commit/04125fe936e456524cb1719a690e4c186ccb83bf))


## v0.5.0 (2026-11-06)

### Features

- **orchestrator**: Profiles and delegate_to in Settings, and per task
  ([#83](https://github.com/brianporeilly/strands-harness/pull/83),
  [`994e980`](https://github.com/brianporeilly/strands-harness/commit/994e9805955403e60ffb0a882d43d247ec264b9f))


## v0.4.2 (2026-11-06)

### Bug Fixes

- **harness**: A stopped run says Stopped. rather than ending in a traceback
  ([#82](https://github.com/brianporeilly/strands-harness/pull/82),
  [`18bca38`](https://github.com/brianporeilly/strands-harness/commit/18bca388d2f7373b43677c472d5b3426dd111081))


## v0.4.1 (2026-11-06)

### Bug Fixes

- **tasks**: Stop a burst run as a Job mid-tool, as Stop does on podman
  ([#81](https://github.com/brianporeilly/strands-harness/pull/81),
  [`68e5f70`](https://github.com/brianporeilly/strands-harness/commit/68e5f70349f77df00032a96a8a5887b8d20f20b7))


## v0.4.0 (2026-11-05)

### Features

- **orchestrator**: Keep its own records apart from the harness's state
  ([#80](https://github.com/brianporeilly/strands-harness/pull/80),
  [`18ec6a8`](https://github.com/brianporeilly/strands-harness/commit/18ec6a80512017702a9078e30d77b3c983ef39c2))


## v0.3.0 (2026-11-05)

### Documentation

- **orchestrator**: Design for running each burst as a Kubernetes Job
  ([#77](https://github.com/brianporeilly/strands-harness/pull/77),
  [`e989249`](https://github.com/brianporeilly/strands-harness/commit/e989249a0a2673338efbb2dac18cead1ea8f4e2f))

- **roadmap**: Profiles and delegate_to cannot yet be set per task or in Settings
  ([#76](https://github.com/brianporeilly/strands-harness/pull/76),
  [`305c84d`](https://github.com/brianporeilly/strands-harness/commit/305c84d9f27a66dfe8c849edf4c9c808d0a42e12))

### Features

- **tasks**: On a cluster, run each burst as a Kubernetes Job
  ([#79](https://github.com/brianporeilly/strands-harness/pull/79),
  [`49a0241`](https://github.com/brianporeilly/strands-harness/commit/49a024136a2f035b9d63d4f9d5b80e5742120904))

### Refactoring

- **tasks**: Run each burst through orchestrator.burst
  ([#78](https://github.com/brianporeilly/strands-harness/pull/78),
  [`26fa73f`](https://github.com/brianporeilly/strands-harness/commit/26fa73f1e7312aa2da869b9a339a78bc12698c95))


## v0.2.0 (2026-11-05)

### Features

- **claude-code**: Claude Code leads and delegates to Strands profiles
  ([#73](https://github.com/brianporeilly/strands-harness/pull/73),
  [`11304f8`](https://github.com/brianporeilly/strands-harness/commit/11304f8455a5860af6be664d80a70061fe036fad))


## v0.1.2 (2026-11-05)

### Bug Fixes

- **agent**: A SIGINT stops a run at once, not when Strands' loop returns
  ([#75](https://github.com/brianporeilly/strands-harness/pull/75),
  [`1f5146b`](https://github.com/brianporeilly/strands-harness/commit/1f5146b2c546a2e31aa8f0c040aa8cf51c3a8fce))


## v0.1.1 (2026-11-05)

### Bug Fixes

- **tools**: A terminal state ends the run, however often the model calls it
  ([#74](https://github.com/brianporeilly/strands-harness/pull/74),
  [`b64feb4`](https://github.com/brianporeilly/strands-harness/commit/b64feb4f63a4173a17c0a62d44aed843a090f0a9))


## v0.1.0 (2026-11-05)

- Initial Release
