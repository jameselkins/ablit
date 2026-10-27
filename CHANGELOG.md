# Changelog

All notable changes to ablit are recorded in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Entries come from the git history and the release tags.

## [Unreleased]

## [0.1.3] - 2026-10-27

### Added

- The `ds41-1017-xq-v4-uncensored` entry for the V4 build of the DeepSeek-V4.1-Flash 1017 model, boot-tested on Vast on 2026-10-26.
- The boot-tested Nanbeige 4.3 33B uncensored entry for llama.cpp.
- GPU model filters in `plan` and `up`, through `--gpu` and `--exclude-gpu`.
- A Claude Code configuration block printed when a compatible endpoint is ready.
- Boot test records tied to the tested model files, serving profile, image digest and bootstrap script. New run records and JSON reports carry the hashes.
- A pinned Xolotl `961d390` image digest in the engine lock. The image builds against llama.cpp `6050b467`, and the bootstrap passes `--tier-batch-pcie 0.25`.
- Clore renting in `ablit up`, connected to the Clore driver.
- The Lium marketplace backend, with keyless search, renting in `ablit up`, setup and `backends/lium.md`. Renting passed one paid round trip on 2026-10-26.
- A pinned Xolotl `03be8a6` image digest in the engine lock, with the conversation cache set to 4096 MiB.
- A boot test record for the Xolotl entry on `03be8a6`, and an 8B RunPod boot test record.
- A comparison of Morph with the other hosted services in the documentation.
- Two unverified llama.cpp entries for Ornith-1.5-122B UNCENSORED (IQ2_XXS and Q4_K_M, text only), the qwen35moe architecture in the llama.cpp engine lock, and Lyber in the hosted service comparison.
- A ruff check, a mypy check, Python 3.13 in the CI matrix, a Dependabot configuration, a pre-commit configuration with ruff and gitleaks, and this changelog.

### Changed

- The Xolotl entries serve 131072 tokens of context instead of 4096, so Claude Code's system prompt and tools fit.
- The September and V3 DeepSeek entries require `--untested` again because their recorded tests cover older engine images and profiles. Historical test records are preserved.
- CI runs the shell tests with macOS's bundled Bash and checks launch bundles from an installed wheel.
- Mypy checks function bodies that have no type annotations.
- `ablit up` stops waiting silently on a machine that does not answer SSH.
- RunPod paid runs use Secure Cloud only, and Community offers are dropped. Exit codes were also fixed.
- The README was rewritten around what works and why to use it.
- The Xolotl 1017 entry is marked boot-tested with its Vast record.

### Fixed

- Launch bundles find the watchdog in installed packages, and `up` checks the bundle before renting.
- Local spending checks enforce the cap even when the watchdog is still sending heartbeats but cannot destroy the machine.
- Host denials are stored per marketplace, with older records preserved.
- Failed searches mark the comparison incomplete, and `up` refuses incomplete quotes.
- Monitor lock contention skips a poll instead of triggering cleanup.
- The Xolotl bootstrap works with Bash 3.2, and agent instructions explain that `up` stays running after readiness.
- Resuming `up` cannot take over a run on a different marketplace or share a run with another live `up` process.
- Remote progress and failure records are treated as untrusted input.

## [0.1.2] - 2026-10-21

### Added

- Paid renting on RunPod.
- The Clore marketplace backend, with search, the rental lifecycle and setup.
- A Xolotl container image build and its workflow, with Blackwell built as `sm_120a`.
- Faster, overlapped downloads when the Xolotl entries boot on Vast.

### Changed

- The 8B entry and the September Xolotl entry are marked boot-tested with their Vast test records.
- The README was rewritten around a first-screen quickstart and status, and the catalog was curated with the runtimes explained.
- The bootstrap logs a low locked-memory limit instead of failing.

### Fixed

- The status lock, the tunnel message and the start deadline.

## [0.1.1] - 2026-10-21

### Added

- The Vast referral ID, a `--yes` option for agents, and `AGENTS.md`.

## [0.1.0] - 2026-10-21

### Added

- The initial scaffold with the catalog, the planner, the engine templates and CI.
- Live read-only search for Vast and RunPod, which makes `ablit plan` work.
- The Vast run lifecycle for the 8B llama.cpp entry.
- Packaging for PyPI, with a release workflow that runs when a tag is pushed.

### Changed

- Vast API paths have trailing slashes, and redirects are refused.
- Vast reads and deletes retry after a rate limit, and polling happens every 10 seconds.
- The watchdog waits 30 seconds between its decision and its destroy call.
- `ablit down` and `ablit status` wait for the run lock.

### Fixed

- The scoped watchdog key and the status of an instance that is still starting.
- A run now detects an instance that Vast gave up on, and denies that host.
- A run survives short network outages, and says why cleanup failed.
- The `/root/.ssh` modes are repaired at start, and a run whose SSH keeps failing is stopped.
- llama-server starts with its library path, fails fast when the engine exits, and keeps its logs.
- The live_plan tests clear CI variables.

[Unreleased]: https://github.com/jameselkins/ablit/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/jameselkins/ablit/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/jameselkins/ablit/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/jameselkins/ablit/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/jameselkins/ablit/releases/tag/v0.1.0
