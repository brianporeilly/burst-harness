# CHANGELOG

<!-- version list -->

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
