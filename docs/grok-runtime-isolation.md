# Grok runtime isolation

Unattended Nightshift does not run Grok under the operator's normal extension environment. The installed CLI is Grok 1.0.46. It honors `GROK_HOME`. Nightshift uses that, plus a private `HOME`, plus a seatbelt.

## Dedicated runtime HOME

Each run gets `runs/<run-id>/runtime-home/`, mode 0700. The child `HOME` is that directory. The operator's home is not passed through.

That stops automatic discovery of user-level directories that live under the normal home, including `.agents`, `.claude`, `.cursor`, ordinary dotfiles, and credential files that tools look up relative to `HOME`.

`TMPDIR`, `TEMP`, and `TMP` point at `runs/<run-id>/tmp`, also mode 0700. Nightshift does not widen the seatbelt to the system temp directory to silence cache warnings.

## Dedicated GROK_HOME

`GROK_HOME` is `runs/<run-id>/grok-home/`, mode 0700, created for that run. The next run gets a new directory. The profile contains only what Nightshift copies in and what the CLI materializes during that run.

Nightshift does not copy:

- the operator's `config.toml`
- the operator's skills
- plugins or marketplaces
- hooks
- MCP credentials or MCP definitions
- memory
- the operator's `AGENTS.md`
- the operator's sessions

The CLI itself, on the first `grok inspect` or launch against that `GROK_HOME`, materializes its own bundled skills, bundled agents, docs, and a generated config into the profile. On this machine that bundle is 22 skills, all with source type `bundled` and paths under the profile. Operator hooks, plugins, and MCP servers stay at zero. The extension audit accepts a bundled skill only when its path is under this profile. It rejects user, project, and plugin skills. The profile directory stays mode 0700. `auth.json` stays mode 0600. The materialized tree is gitignored with `state/`.

The child also receives `GROK_MEMORY=0` and `GROK_WORKFLOWS=0`.

The Grok argv for an unattended job adds the flags this binary accepts:

```text
--permission-mode dontAsk
--no-subagents
--no-memory
--no-auto-update
--disable-web-search          # when job.network is false
--output-format streaming-json
--max-turns <n>
--session-id <id>
```

`--sandbox` is omitted. See "Seatbelt and Grok's sandbox flag" below.

`--no-memory` and `--no-auto-update` are accepted by Grok 1.0.46 even though the help summary omits them. Nightshift does not emit flags this binary rejects. It does not emit `--always-approve`, `bypassPermissions`, `--worktree`, or `grok agent headless`.

`job.network = false` is the web-search switch. It is not an OS network deny. The provider seatbelt allows network so the model API can be reached. See the limitation below.

## Authentication

The installed CLI authenticates from local Grok state. Nightshift keeps a separate copy so a refresh inside a run does not rewrite the operator's live auth file.

```text
nightshift auth grok status
nightshift auth grok bootstrap
```

`status` prints `auth present` or `auth absent`, and the file mode when the file exists. It does not print a path and it does not print file contents.

`bootstrap` copies one file, `auth.json`, from the operator's Grok profile into `state/credentials/grok/auth.json`. The parent directory is mode 0700. The file is mode 0600. The copy is a separate file, not a symlink. The command's stdout is the word `bootstrapped`. Contents are never printed. If that store is empty and a legacy `state/grok-profile/auth.json` is a regular file, Nightshift copies it once and leaves the legacy file in place.

Each Grok launch copies that auth file into the per-run `GROK_HOME`. After the provider exits, and on cancel, interrupt, and recovery, Nightshift removes the per-run copy. Diagnostic files in that home stay. The persistent store is never deleted automatically and is not a provider writable root.

No other auth file is copied unless a real launch shows that the CLI requires it. Unit tests use a synthetic auth file. The real file is gitignored with the rest of `state/`.

## Extension-surface preflight

Before a real Grok provider starts, Nightshift runs:

```text
grok inspect --json
```

in the sanitized environment, with the clone as cwd. The JSON is parsed into an `ExtensionSurfaceAudit`. The launch is refused with `BLOCKED_EXTENSION_SURFACE` when the audit finds any of:

- a user, project, or plugin hook
- an external MCP server
- a user or project plugin
- a Claude, Cursor, or Codex hook, skill, plugin, or MCP server
- an unexpected permission grant
- a non-builtin agent
- an LSP server or a marketplace
- an operator-global instruction file

Built-in agents may remain. A bundled skill may remain only when it lives under the Nightshift `GROK_HOME`.

Project instruction files such as `AGENTS.md` are untrusted instructions. They can influence reasoning. They are not executable hooks, and they are not, by themselves, a reason to block. The invariant is: untrusted repository content may influence reasoning, and it must not silently grant new tools or executable hooks.

The installed CLI reports Claude, Cursor, and Codex compatibility cells as enabled even when `GROK_HOME` is empty and no servers are configured. Those flags are product defaults. The audit does not treat an enabled flag as an imported server. A server, hook, or plugin that inspect actually lists is a violation.

`NIGHTSHIFT_FORBID_GROK=1` refuses the launch before `inspect`, so tests do not call the CLI.

## Project files inside the clone

A target repository may contain `.grok/`, `.mcp.json`, `.cursor/mcp.json`, Cursor hooks, or `.claude/`. Nightshift moves those paths into the run's neutralize record, or unlinks them when they are symlinks, and only inside the clone. The source repository is not modified. After neutralization, `grok inspect --json` is the check that the executable surface is gone.

## Seatbelt and Grok's sandbox flag

Nightshift wraps the process with `/usr/bin/sandbox-exec` and does not pass `--sandbox`.

A check on Grok 1.0.46 showed two different results. Outside the seatbelt, `--sandbox workspace` applied and the model answered. Inside the seatbelt, including under a wide outer profile, Grok printed `sandbox initialization failed: Operation not permitted` and refused to start. Nested sandbox setup is denied by the kernel. Passing the flag would make every contained launch exit 1 before the model runs. The seatbelt remains in place either way.

The seatbelt is the containment Nightshift enforces:

- deny by default, with no global file-read allow
- system and toolchain reads, then a deny of the operator home and the original source checkout
- a re-allow of the workspace, the run directory, the private `HOME`, the per-run `GROK_HOME`, the resolved tool path, and config `read_roots` only
- `file-read-metadata` on ancestors of those roots, so a path walk can stat them without reading their contents
- writes only under the workspace and the run directory
- `/dev/null` readable and writable
- verification denies network
- the Grok provider allows network

If `sandbox-exec` itself is missing, Nightshift blocks the run. It does not fall through to Grok's own sandbox flag.

The last matching seatbelt rule wins, so the re-allow is written after the home and source denies. A path is emitted both as named and resolved, so a `/var` path and its `/private/var` alias match. A job prompt cannot add a read root.

## Accepted network limitation

The provider profile contains `(allow network*)` because the model API is a network service. Consequences:

- Grok can reach its API.
- An https or ssh `git push` from the provider process is not blocked by the seatbelt.
- Web search is still disabled when `job.network` is false, at the Grok flag layer.
- Verification runs with `(deny network*)`.
- A push to a local path outside the writable roots, such as a temporary bare repo, is a filesystem denial and is a hard block.

A clean sacrificial probe therefore reports `PASS_WITH_LIMITATIONS`. `PASS` would require the provider network path to be closed, which would also block the model API. The threat model accepts the limitation explicitly. It is not an unstated hole.

## What a clean Nightshift inspect looks like

Counts, not paths:

- hooks 0
- plugins 0
- MCP servers 0
- operator-global instructions 0
- builtin agents only
- skills: the CLI's bundled set, paths under that run's `grok-home`, and no user, project, or plugin skills
- permission sources empty and permissions loaded 0

The operator's normal inspect is a different process, with the operator's home and the operator's `GROK_HOME`. Those counts must not appear in the Nightshift child. The safety probe checks that, and a mismatch blocks the launch.
