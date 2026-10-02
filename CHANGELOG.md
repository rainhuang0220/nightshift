# Changelog

All notable changes to Nightshift are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Nightshift uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-10-02

### Added

- An independent local Git clone as the default workspace. The clone uses `--no-hardlinks`, checks out the recorded revision detached, and removes `origin`.
- A source-integrity snapshot covering HEAD, porcelain status, the current branch, refs and object IDs, `packed-refs`, local Git config, the index, hooks, and the worktree registry.
- One success gate, `decide_final`, shared by normal execution, verification recovery, and recorded-state finalization.
- A per-run private `HOME` and a dedicated `GROK_HOME` at `state/grok-profile`.
- `nightshift auth grok status` and `nightshift auth grok bootstrap`, which copy only the Grok auth file into that profile.
- An extension-surface audit through `grok inspect --json` before a real Grok launch.
- Neutralization of project extension files inside the clone only.
- Structured `[[verification]]` argv steps, with a timeout and process-group termination.
- Seatbelt profiles for the provider and for verification.
- `nightshift safety probe`, deterministic by default. `nightshift safety probe --provider grok` runs one sacrificial canary.
- Private runtime directories (mode 0700) and private logs and database (mode 0600).
- Apache-2.0 license, contributor notes, and a security note. A private vulnerability channel is not configured yet.
- Package metadata for installation from a checkout. The version is `nightshift.__version__`.
- A GitHub Actions workflow that runs the unit tests on macOS for Python 3.11 through 3.14. It does not call the model.

### Changed

- Linked Git worktrees remain available as `isolation = "worktree"` and are labeled weaker isolation. They are not the unattended default.
- The Grok adapter uses streaming JSON, `--no-subagents`, `--no-memory`, `--no-auto-update`, and `--disable-web-search` when the job disables network.
- Morning reports summarize provider output and omit raw stderr.
- Child environments drop cloud, package-registry, and generic secret variables, and they set a run-local Git config.

### Security

- A recovered run cannot become `SUCCEEDED` through a weaker integrity check than a normal run. A missing integrity baseline fails closed when a source repository is set.
- Legacy shell verification is blocked for Grok jobs unless the job opts in.
- Operator Grok skills, plugins, hooks, and MCP servers are not the child profile. A dirty extension surface blocks the launch.
- Grok's `--sandbox` flag is omitted. Nested sandbox setup fails inside the seatbelt, and Grok then refuses to start. The seatbelt is the containment Nightshift enforces.
- The sacrificial canary result is `PASS_WITH_LIMITATIONS`. The provider seatbelt allows network so the model API can be reached. A network `git push` is not an operating-system hard block. A push to a local bare repository outside the writable roots is a filesystem hard block.

## [0.1.0] - 2026-10-02

### Added

- A local queue, SQLite run state, repository and concurrency locks, and a single-job daemon.
- Job manifests (`job.toml` plus `prompt.md`) with validation.
- Detached Git worktrees as the v0.1 workspace, without cleaning or checking out the source tree.
- A Grok CLI adapter and a `FakeProvider` for tests and dry runs.
- Morning reports, `doctor`, logs, cancel, and `recover`.
- `recover` classifies active runs and does not relaunch a provider.
- A missing workspace on a dead run is `process_gone_workspace_missing` and `FAILED`.

[Unreleased]: https://github.com/rainhuang0220/nightshift/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/rainhuang0220/nightshift/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rainhuang0220/nightshift/releases/tag/v0.1.0
