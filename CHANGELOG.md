# Changelog

All notable changes to Nightshift are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Nightshift uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- Provider filesystem reads are deny-by-default. The seatbelt allows required system and toolchain reads, denies the operator home and the original source checkout, then re-allows only the isolated workspace, the attempt directory, the private runtime home, the per-attempt `GROK_HOME`, the resolved tool path, and optional `[containment] read_roots`. Ancestor directories of those roots get `file-read-metadata` only, so a path walk can stat them without listing or reading their contents. There is no global file-read allow. A job prompt cannot add a read root.
- `GROK_HOME` is a new directory for each attempt (`runs/<id>/attempt-<n>/grok-home`). The persistent auth file is `state/credentials/grok/auth.json` (directory mode 0700, file mode 0600, gitignored). A provider launch copies that file into the per-attempt home. The success path removes that copy before verification, so a verification command cannot read it. Exit, cancel, interrupt, and recovery of a dead phase remove the copy. A live phase is left untouched. Verification recovery does not copy it. If `grok-home` is a symlink, cleanup removes that link and does not follow it; an auth copy refuses a symlinked parent and does not follow a symlinked destination. A scrub error does not skip lock release. The persistent store is not deleted and is not provider-writable.
- Trusted parent writes use `O_NOFOLLOW` and `O_CLOEXEC`, require a regular file, and fail closed when the path is a symlink. The link is left in place.
- Provider writes are the workspace when `write_scope` allows it, plus that attempt's `grok-home`, `runtime-home`, and `tmp`. Profiles, the prompt snapshot, shims, logs, and the verification runtime are not in that set.
- Verification builds a fresh runtime after the provider exits. A verification exit of 0 means the declared check passed in the mutable workspace. It does not mean the model could not influence that check.
- Lock acquisition is one immediate transaction shared by the lock manager and queue claiming. A live owner is never replaced.
- Active phases record `preparing`, `inspect`, `provider`, or `verifying`, plus the child pid, process group, and process start time. Recover leaves a live phase in place and does not relaunch a provider. Cancellation is recorded before the live child process group is signalled, and that signal never targets Nightshift's own group. A cancelled run cannot become `SUCCEEDED`.
- Recovery no longer scrubs active attempt credentials before determining that a phase is dead.
- Retry starts a new session under `runs/<id>/attempt-<n>/` and keeps the previous attempt's logs. It does not pass `--resume`.
- The first clone and worktree checkout run with hooks disabled through an invocation-scoped Git config. The operator's global Git config is not edited.
- `grok inspect` and `grok --version` try a narrow preflight seatbelt and record a fallback instead of widening the provider profile.
- Morning reports keep the provider's user-visible final text as `## Provider conclusion`. A `finding:` prefix is not required. Thought and usage records are omitted.
- Project extension files are moved out of the clone only while the provider runs. They are restored before verification, so a read-only job does not keep those deletions. A symlink that points outside the workspace is unlinked and restored as a symlink; its target is not moved. The real safety probe judges the launch-time extension audit, because those restored project files are visible again after the provider exits.
- Protected Git hooks and worktree metadata are content-hashed, including symlink targets. A protected tree file above 1,000,000 bytes, or an unreadable protected path, fails the snapshot closed. The Git object database is not hashed.

### Changed

- The morning report includes a short trust-boundary section: per-run `GROK_HOME`, source read isolation, operator-home read isolation, source integrity, extension audit, and network containment.
- `nightshift safety probe` reports filesystem write containment, filesystem read containment, the accepted provider-network limitation, cross-run runtime isolation, trusted-parent symlink handling, provider control-directory writes, atomic lock ownership, attempt isolation, and checkout hook isolation as separate rows. The default probe does not call Grok.
- The policy preamble says local commits are in the workspace. The unimplemented `cursor` provider error no longer says `v0.1`.

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
