# CHANGELOG

Releases before v0.21.0 were made in the project's earlier, private repository;
their entries are kept without links.

<!-- version list -->

## v0.52.0 (2026-11-20)

### Features

- **harness**: The setup round: a foundation document or a prompt turned into a project's plan,
  ending in a PR to approve ([#56](https://github.com/brianporeilly/burst-harness/pull/56),
  [`47b7fcc`](https://github.com/brianporeilly/burst-harness/commit/47b7fccb1e5b622c4830414a9b42385355c526c4))


## v0.51.0 (2026-11-20)

### Features

- **harness**: Keep the record and the docs current: a nudge, a burst sent back once to record,
  docs_style, one docs reminder ([#55](https://github.com/brianporeilly/burst-harness/pull/55),
  [`64f6ccd`](https://github.com/brianporeilly/burst-harness/commit/64f6ccdd3085b7228ebf0a48fa478cc1038b8fa7))


## v0.50.0 (2026-11-20)

### Features

- **harness**: A project's milestones: criteria ticked with evidence, the plan amended on the
  record, finishing checked, the PR written from the record
  ([#54](https://github.com/brianporeilly/burst-harness/pull/54),
  [`5832058`](https://github.com/brianporeilly/burst-harness/commit/5832058bd856a77e161f1f5bfa2e83417a90862a))


## v0.49.0 (2026-11-20)

### Documentation

- Design for projects on rails: the plan, its progress, and the tools that keep them
  ([#52](https://github.com/brianporeilly/burst-harness/pull/52),
  [`a2187af`](https://github.com/brianporeilly/burst-harness/commit/a2187afc101437244549400de632cca60dfce7fb))

### Features

- **harness**: A project's PLAN.md and PROGRESS.md as its record, kept through tools, in place of
  the task document ([#53](https://github.com/brianporeilly/burst-harness/pull/53),
  [`acb8d54`](https://github.com/brianporeilly/burst-harness/commit/acb8d54bcbf9bf90cb303946727a0c857790f1c6))


## v0.48.0 (2026-11-20)

### Features

- **harness**: Notice replies that keep restating the same summary, and tell the model it is settled
  ([#51](https://github.com/brianporeilly/burst-harness/pull/51),
  [`6cda975`](https://github.com/brianporeilly/burst-harness/commit/6cda975b9f20dd94e86e2ca05d8c687b4532345b))


## v0.47.0 (2026-11-20)

### Features

- **harness**: A note where the window cut the conversation, with the task document and git as they
  are now ([#50](https://github.com/brianporeilly/burst-harness/pull/50),
  [`3ef8aa7`](https://github.com/brianporeilly/burst-harness/commit/3ef8aa7d637fb1b6a26532a6da0c06de0ffd8346))


## v0.46.0 (2026-11-20)

### Bug Fixes

- **harness**: A smoke test and replay of a whole burst; checks always say how many failed
  ([#48](https://github.com/brianporeilly/burst-harness/pull/48),
  [`006be91`](https://github.com/brianporeilly/burst-harness/commit/006be9144ff67d500b8e07b8ef5e298e8d44051a))

### Features

- **harness**: Every burst starts from the task document and git, not the last burst's conversation
  ([#49](https://github.com/brianporeilly/burst-harness/pull/49),
  [`78ceb42`](https://github.com/brianporeilly/burst-harness/commit/78ceb42525a5ba25f34be1bfc6317d80abf79822))


## v0.45.2 (2026-11-20)

### Bug Fixes

- **harness**: A smoke test and replay of a whole burst; checks always say how many failed
  ([#48](https://github.com/brianporeilly/burst-harness/pull/48),
  [`006be91`](https://github.com/brianporeilly/burst-harness/commit/006be9144ff67d500b8e07b8ef5e298e8d44051a))


## v0.45.1 (2026-11-19)

### Bug Fixes

- **models**: A llama.cpp client for each event loop, so a kept-alive connection can't crash the
  next call ([#47](https://github.com/brianporeilly/burst-harness/pull/47),
  [`4033cc4`](https://github.com/brianporeilly/burst-harness/commit/4033cc4c7fbebd9bca571ffa40a02417bb42accb))


## v0.45.0 (2026-11-19)

### Features

- **orchestrator**: Set the review and the fallback for every task in Settings
  ([#46](https://github.com/brianporeilly/burst-harness/pull/46),
  [`a4187c5`](https://github.com/brianporeilly/burst-harness/commit/a4187c5c276b84cafe8de77f9b4af76a9a789302))


## v0.44.0 (2026-11-19)

### Bug Fixes

- **gateway**: Certificates with key identifiers, which strict verification requires
  ([#44](https://github.com/brianporeilly/burst-harness/pull/44),
  [`b9ad9fe`](https://github.com/brianporeilly/burst-harness/commit/b9ad9fef4fe337d7ab16c7b3862e38fc0420811a))

### Features

- **harness**: Fall back to Claude Code, or a profile, when the local model's server is down
  ([#45](https://github.com/brianporeilly/burst-harness/pull/45),
  [`a94cbc4`](https://github.com/brianporeilly/burst-harness/commit/a94cbc4bd7f410aed9702e5f7c5cb2683599731f))


## v0.43.3 (2026-11-19)

### Bug Fixes

- **harness**: Nothing stranded by a long check, review or sandbox deadline; Claude Code ended once
  done ([#43](https://github.com/brianporeilly/burst-harness/pull/43),
  [`da22c66`](https://github.com/brianporeilly/burst-harness/commit/da22c6681e0cb39f5f4c60d5815f3669b70d66ba))


## v0.43.2 (2026-11-18)

### Bug Fixes

- **harness**: Tell the agent its builds are cached, and not to point them elsewhere
  ([#42](https://github.com/brianporeilly/burst-harness/pull/42),
  [`ac791c5`](https://github.com/brianporeilly/burst-harness/commit/ac791c568c504ccfa367d2d4b50289188a8d0971))


## v0.43.1 (2026-11-18)

### Bug Fixes

- **harness**: A loop is stopped only when nothing new worked between its repeats
  ([#41](https://github.com/brianporeilly/burst-harness/pull/41),
  [`54b3865`](https://github.com/brianporeilly/burst-harness/commit/54b386517446da5f7beb2bb560f69b423854874e))


## v0.43.0 (2026-11-18)

### Features

- **metrics**: A transcript per burst: every message, tool and model call timed, beside the
  snapshots ([#40](https://github.com/brianporeilly/burst-harness/pull/40),
  [`b653c5c`](https://github.com/brianporeilly/burst-harness/commit/b653c5c437fe5f23f915ab5ffe37aeadd7123340))


## v0.42.0 (2026-11-18)

### Features

- **harness**: A review before the PR: review_with's profile reads the change, its findings go back
  once ([#39](https://github.com/brianporeilly/burst-harness/pull/39),
  [`f80eaeb`](https://github.com/brianporeilly/burst-harness/commit/f80eaebed7e71d87eb20ca748820e5b4ae91294b))


## v0.41.0 (2026-11-18)

### Features

- **harness**: Setup before the model: setup_command runs before each burst, its output in the first
  prompt ([#38](https://github.com/brianporeilly/burst-harness/pull/38),
  [`d6d360e`](https://github.com/brianporeilly/burst-harness/commit/d6d360e108c594d599c8fbffc396bd89655eff4b))


## v0.40.0 (2026-11-18)

### Features

- **harness**: Check before a PR: check_command must pass before finish_with_pr pushes
  ([#37](https://github.com/brianporeilly/burst-harness/pull/37),
  [`b3e5bdc`](https://github.com/brianporeilly/burst-harness/commit/b3e5bdc259e4f01dc99e126d6bce132f797b50bf))


## v0.39.0 (2026-11-18)

### Features

- **harness**: Notice a loop: the same call failing the same way is warned, then stopped
  ([#36](https://github.com/brianporeilly/burst-harness/pull/36),
  [`9ce68d4`](https://github.com/brianporeilly/burst-harness/commit/9ce68d4fb01c2ca0a64dd72ec562bcb4019594b9))


## v0.38.0 (2026-11-18)

### Features

- **metrics**: Claude Code's own usage: per model, the plan's utilization, the largest prompt
  ([#35](https://github.com/brianporeilly/burst-harness/pull/35),
  [`b96e29c`](https://github.com/brianporeilly/burst-harness/commit/b96e29ce55dbb48b9fa8a7778fc1fa6d22eed270))


## v0.37.0 (2026-11-18)

### Features

- **harness**: Budgets per burst: model calls, dollars and minutes, paused for review when reached
  ([#34](https://github.com/brianporeilly/burst-harness/pull/34),
  [`d9dc385`](https://github.com/brianporeilly/burst-harness/commit/d9dc38568942203c42bcfcf869b2efbedf4d1342))


## v0.36.0 (2026-11-18)

### Features

- **metrics**: A burst's numbers in HARNESS_RESULT and HARNESS_JOB, and the variant it ran as
  ([#31](https://github.com/brianporeilly/burst-harness/pull/31),
  [`5c8b998`](https://github.com/brianporeilly/burst-harness/commit/5c8b9985f6eea3077af2403bc9ffa24b8298e6e3))


## v0.35.0 (2026-11-18)

### Documentation

- **roadmap**: Phase 10, reliable runs and measuring harness changes
  ([#33](https://github.com/brianporeilly/burst-harness/pull/33),
  [`a424a3e`](https://github.com/brianporeilly/burst-harness/commit/a424a3eda43a520b0a82b6be28f5177a7ab7ee84))

### Features

- **agent**: The Strands agent is given the repo's AGENTS.md (or CLAUDE.md)
  ([#32](https://github.com/brianporeilly/burst-harness/pull/32),
  [`53d9313`](https://github.com/brianporeilly/burst-harness/commit/53d9313f96d600862839c0b753f2021f754b9dfa))


## v0.34.0 (2026-11-16)

### Documentation

- **roadmap**: What shipped since 2026-11-11, and what is next
  ([#29](https://github.com/brianporeilly/burst-harness/pull/29),
  [`4c2a3db`](https://github.com/brianporeilly/burst-harness/commit/4c2a3dbde553958927ad45c2b365c348d4dad5d8))

### Features

- **slack**: A task from the page or the API can be told in a direct message when a burst ends, and
  continued from its thread ([#30](https://github.com/brianporeilly/burst-harness/pull/30),
  [`0b5518f`](https://github.com/brianporeilly/burst-harness/commit/0b5518f9052a8d96522b8dc47c518b443765cabe))


## v0.33.1 (2026-11-16)

### Bug Fixes

- **orchestrator**: A cluster backend is named agent-sandbox, so warm-pool recycling starts on the
  cluster ([#28](https://github.com/brianporeilly/burst-harness/pull/28),
  [`9e5c311`](https://github.com/brianporeilly/burst-harness/commit/9e5c3118cf908fe8879dbdba2370ee283ba3bd4e))


## v0.33.0 (2026-11-16)

### Features

- **slack**: A job waiting for Accept is told to the listed users, with an Accept button
  ([#27](https://github.com/brianporeilly/burst-harness/pull/27),
  [`8b94e15`](https://github.com/brianporeilly/burst-harness/commit/8b94e154e34b77671050c3e2c9323a19df201b8a))


## v0.32.1 (2026-11-15)

### Bug Fixes

- **tasks**: A gateway error on a task's refusals goes to the log, not the page
  ([#26](https://github.com/brianporeilly/burst-harness/pull/26),
  [`73be5a7`](https://github.com/brianporeilly/burst-harness/commit/73be5a7f42238e5853bf62bdb8bf1200b5abcf28))


## v0.32.0 (2026-11-15)

### Continuous Integration

- **release**: A run overtaken by a newer merge stands down instead of failing
  ([#25](https://github.com/brianporeilly/burst-harness/pull/25),
  [`45d399b`](https://github.com/brianporeilly/burst-harness/commit/45d399bbe252599eb4966751744d6a771c58e118))

### Features

- **orchestrator**: Webhooks from GitHub and Forgejo, signed, make a merged PR or a job repo push
  seen now ([#24](https://github.com/brianporeilly/burst-harness/pull/24),
  [`1d6d4bb`](https://github.com/brianporeilly/burst-harness/commit/1d6d4bbbb784fa23bb72f926c971f0f1cd7f3481))


## v0.31.0 (2026-11-15)

### Features

- **orchestrator**: Warm pools' idle sandboxes out of date with their template are replaced, so an
  image bump needs no hand recycling ([#23](https://github.com/brianporeilly/burst-harness/pull/23),
  [`35628ca`](https://github.com/brianporeilly/burst-harness/commit/35628ca90be7eb0c532833f935deca6f341f51b2))

- **tasks**: The agent is told its sandbox's memory limit and CPUs, to keep its builds within them
  ([#22](https://github.com/brianporeilly/burst-harness/pull/22),
  [`8e2b956`](https://github.com/brianporeilly/burst-harness/commit/8e2b95663377d85e50eb1f111a44bdc509498547))


## v0.30.0 (2026-11-15)

### Features

- **jobs**: A job repo's commit that changes a job's access waits for Accept, and the job runs as
  accepted meanwhile ([#21](https://github.com/brianporeilly/burst-harness/pull/21),
  [`e40ec79`](https://github.com/brianporeilly/burst-harness/commit/e40ec7943317748141e96d786403c070ac554be6))

- **orchestrator**: Runners in Settings, picked per task, with their SSH keys granted to the runs on
  them ([#20](https://github.com/brianporeilly/burst-harness/pull/20),
  [`6d83ab3`](https://github.com/brianporeilly/burst-harness/commit/6d83ab3d70b11d1dcc859f08ed97f3f6176b2c4c))


## v0.29.0 (2026-11-15)

### Features

- **runners**: Run on hosts of your own over SSH, as a plain host or a remote podman/docker
  ([#18](https://github.com/brianporeilly/burst-harness/pull/18),
  [`3e0990a`](https://github.com/brianporeilly/burst-harness/commit/3e0990aa5f0f30d7ed19960c41f913ff9571aaa1))


## v0.28.0 (2026-11-14)

### Features

- **tasks**: A run whose sandbox ran out of memory says so, with its limit
  ([#19](https://github.com/brianporeilly/burst-harness/pull/19),
  [`38abbb3`](https://github.com/brianporeilly/burst-harness/commit/38abbb32e6ef0ac9deff19d814607e6212a38e0a))


## v0.27.0 (2026-11-14)

### Features

- **tasks**: Opt-in auto_continue starts a task's next round once its PR is merged
  ([#16](https://github.com/brianporeilly/burst-harness/pull/16),
  [`4aee7f6`](https://github.com/brianporeilly/burst-harness/commit/4aee7f6f6152361d84024b7a09db1eb9fdd1bb8a))

### Testing

- **jobs**: An unreachable model is retried without backoff, taking the suite from 8 minutes to 20s
  ([#17](https://github.com/brianporeilly/burst-harness/pull/17),
  [`1e28ab1`](https://github.com/brianporeilly/burst-harness/commit/1e28ab144edb0fb59078d20e40fefbb0e1eff295))


## v0.26.4 (2026-11-14)

### Bug Fixes

- **tasks**: A kept sandbox that is gone is replaced by a fresh claim
  ([#15](https://github.com/brianporeilly/burst-harness/pull/15),
  [`f2daf28`](https://github.com/brianporeilly/burst-harness/commit/f2daf2807514f3e5c059bbd30ca089f86a4ac8b0))


## v0.26.3 (2026-11-13)

### Bug Fixes

- **tasks**: A burst that reattaches to a kept sandbox renews the claim's deadline
  ([#14](https://github.com/brianporeilly/burst-harness/pull/14),
  [`49e3726`](https://github.com/brianporeilly/burst-harness/commit/49e37261ce1f0b76e0886f6d7619f01e28b40ebe))


## v0.26.2 (2026-11-13)

### Bug Fixes

- **tasks**: A refused push is push_failed, opens no PR, and keeps the sandbox
  ([#13](https://github.com/brianporeilly/burst-harness/pull/13),
  [`2049e8a`](https://github.com/brianporeilly/burst-harness/commit/2049e8a90783019fe6b5f23b16fff7e2cf7d7d37))


## v0.26.1 (2026-11-13)

### Bug Fixes

- **sandbox**: Claude Code 2.1.323, for the newest models
  ([#12](https://github.com/brianporeilly/burst-harness/pull/12),
  [`624ad5d`](https://github.com/brianporeilly/burst-harness/commit/624ad5da139afa934a220e992391cab861b83414))


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
