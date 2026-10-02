# Architecture

Nightshift v0.2 is one process, one SQLite database, and one child provider at a time. The control plane never becomes a checkout of a target repository.

```text
Trusted Nightshift control plane
            |
            v
      Job snapshot
            |
            v
  Read-only source input
            |
            v
 Independent local clone
            |
            v
 Sanitized Grok runtime
            |
     +------+------+
     |             |
 permission      sandbox
 policy         seatbelt
     |             |
     +------+------+
            |
            v
   isolated workspace
            |
            v
  independent verification
            |
            v
 source integrity gate
            |
            v
     Morning report
            |
            v
 Human-controlled import
```

There is no automatic merge-back and no automatic push.

## Responsibilities

The operator writes a job and approves what it is allowed to touch. Nightshift queues the job, locks it, clones the recorded revision, starts the provider, streams logs, runs verification, checks source integrity, writes the report, and releases the lock. The provider process does the reasoning. v0.2 ships a Grok CLI adapter and a FakeProvider used by tests and dry runs. Cursor is rejected at validation. Codex is not a provider.

## Modules

| Module | Role |
| --- | --- |
| `cli.py` | argparse entry point, including auth and safety probe |
| `config.py` | root, paths, concurrency, permission mode |
| `models.py` | states, the only transition table, job and run records |
| `job.py` | TOML + `prompt.md` parsing, structured verification |
| `db.py` | SQLite runs, events, locks; transactions; mode 0600 |
| `queue.py` | enqueue and conditional claim |
| `locks.py` | concurrency-group, repo, and workspace locks |
| `workspace.py` | clone backend (default) and worktree backend |
| `gitutil.py` | git subprocess helpers used by Nightshift itself |
| `integrity.py` | source integrity snapshot and comparison |
| `finalize.py` | the only success decision |
| `containment.py` | seatbelt profiles and contained process runs |
| `extensions.py` | `grok inspect` audit and clone-only neutralization |
| `runtime.py` | per-attempt HOME, GROK_HOME, auth copy |
| `priv.py` | mode 0700 directories and mode 0600 files |
| `policy.py` | allow/deny rules, preamble, environment minimization |
| `guard.py` | PATH shims for git, gh, sudo, kaggle, npm, twine |
| `runner.py` | one run: prepare, execute, verify, gate, report |
| `supervisor.py` | daemon loop, recover, cancel |
| `report.py` | morning report text |
| `probe.py` | fake safety probe and sacrificial Grok canary |
| `doctor.py` | local environment check |
| `providers/base.py` | request, result, subprocess, termination |
| `providers/grok.py` | argv builder and Grok process adapter |
| `providers/fake.py` | deterministic provider |
| `providers/fake_child.py` | child program the fake provider execs |
| `testkit.py` | fixtures imported by the suite |

`job.py`, `guard.py`, `gitutil.py`, `fake_child.py`, and `testkit.py` are extra modules so parsing, the PATH policy, and git calls stay out of the supervisor. `testkit.py` lives in the package because a site-packages distribution named `tests` would otherwise shadow a top-level test helper.

`--root` (or `NIGHTSHIFT_ROOT`) relocates `state/`, `runs/`, and `worktrees/`. Tests and the bootstrap dry run use it so they never write into a real project.

## State

SQLite at `state/nightshift.db` is authoritative. The connection uses WAL and `synchronous=FULL`. The database file is mode 0600 and its parent is mode 0700. State changes run inside `BEGIN IMMEDIATE`. `transition` updates a row only when the current state is the expected one, and `ensure_transition` rejects anything outside `LEGAL_TRANSITIONS`.

```text
QUEUED -> PREPARING -> RUNNING -> VERIFYING -> SUCCEEDED
```

`FAILED` and `INTERRUPTED` may return to `QUEUED` only through an explicit retry, which increments `attempt` and stops at `max_attempts`. `BLOCKED`, `CANCELLED`, and `SUCCEEDED` have no outbound edge. Active states are `QUEUED`, `PREPARING`, `RUNNING`, and `VERIFYING`. `PREPARING` may move to `BLOCKED`. `RUNNING` cannot move to `BLOCKED`; a block discovered after launch is recorded as `FAILED`.

`SOURCE_INTEGRITY_VIOLATION` and `BLOCKED_EXTENSION_SURFACE` are reasons, not new states.

Timestamps are UTC with microseconds so two inserts in the same second keep queue order.

Run rows store `source_integrity` (the post-prepare snapshot) and `invocation` (provider metadata with counts, not secret values and not an absolute home path).

## Workspace backends

`WorkspaceBackend` has `prepare`, `inspect`, `cleanup`, and `import_instructions`.

`CloneWorkspaceBackend` is the default. It runs:

```text
git clone --no-hardlinks --no-checkout <source> <dest>
git -C <dest> remote remove origin
git -C <dest> checkout --detach <recorded-revision>
```

The clone and the detached checkout already run with hooks and templates disabled, using an invocation-scoped Git config that does not read or edit the operator's global config. The clone then gets an empty local `core.hooksPath`, the identity `Nightshift <nightshift@localhost>`, and `commit.gpgsign=false`. `--shared` is not used. Untracked dirty files in the source are not copied. A commit in the clone does not create or update source refs, and it does not register a source worktree.

`WorktreeWorkspaceBackend` is `isolation = "worktree"`. Its instructions say:

```text
WEAKER ISOLATION
SHARES SOURCE GIT METADATA
NOT DEFAULT FOR UNATTENDED JOBS
```

`git worktree add` updates the source `.git/worktrees` registry. That is the only source category a worktree prepare is allowed to change. Clone prepare must change nothing.

## One run

1. `queue add` stores the parsed job JSON, including isolation and the verification plan, and the resolved repository path.
2. `claim_next` moves `QUEUED` to `PREPARING` and takes the group and repo locks, skipping a group or repo still held by a live owner.
3. The runner resolves `base_ref` to a commit and captures a source integrity snapshot.
4. It prepares the workspace. Clone prepare must leave every integrity category unchanged. Worktree prepare may change only `worktrees`.
5. The post-prepare snapshot is stored. Later success checks compare against that snapshot. Nightshift does not clean, reset, stash, or checkout the source.
6. Project extension files (`.grok`, `.mcp.json`, `.cursor/mcp.json`, `.cursor/hooks.json`, `.cursor/hooks`, `.claude`) are moved or unlinked inside the clone only. Symlinks are unlinked and not followed. `AGENTS.md` and `CLAUDE.md` stay, and are recorded as untrusted instructions.
7. A Grok job with a legacy shell verification step is blocked unless `allow_legacy_shell_verification` is true. A missing `sandbox-exec` blocks the run. `NIGHTSHIFT_FORBID_GROK=1` blocks a Grok launch before `inspect`.
8. Otherwise a Grok launch runs `grok inspect --json` in the sanitized environment, first under the preflight seatbelt. A failing `ExtensionSurfaceAudit` raises `BLOCKED_EXTENSION_SURFACE` and does not start the model. Inspect is its own cancellable process group. Nightshift records the phase (`preparing`, `inspect`, `provider`, or `verifying`), the child pid and process group when there is one, and the process start time.
9. The provider starts with the clone as cwd, a new session id, isolated `HOME` and `GROK_HOME`, and a seatbelt profile. Its writable roots are the workspace (unless `write_scope` is `none`), `grok-home`, `runtime-home`, and `tmp` inside `runs/<id>/attempt-<n>/`. Stdout and stderr are scrubbed on the way to disk. The PID, process group, start time, and the session id on the argv are stored. A retry builds `attempt-<n+1>` and does not resume the previous Grok session.
10. After the child exits, Nightshift builds a fresh verification runtime that the provider could not write, then runs the snapshotted verification plan in the clone under a second seatbelt that denies network. A verification exit of 0 means that declared check passed in the mutable workspace. It does not prove the model could not influence the check. A timeout or cancel kills the verification process group and does not signal Nightshift's own group.
11. `decide_final` is the only success decision. `SUCCEEDED` requires provider exit exactly 0, verification that ran and exited 0, a workspace that exists, a passing integrity check, and a passing safety audit. A run already marked `CANCELLED` cannot move to `SUCCEEDED`.
12. The report is written. Locks are released. Workspaces are kept. Failed workspaces are removed only when `destroy_failed_worktrees` is true, and clone removal refuses to delete a path that overlaps the source.

`nightshift run` does the same path without the daemon. The daemon calls `recover` first (no relaunch), then claims while active `PREPARING`/`RUNNING`/`VERIFYING` rows are below `concurrency`. The default concurrency is 1.

## Finalization gate

`decide_final` is used by normal execution, by verification-only recovery, and by recorded-state finalization. The three paths do not keep separate success rules.

Before `SUCCEEDED`:

- the provider result is acceptable (exit exactly 0; a missing exit is not success)
- verification completed and exited 0
- the workspace exists
- source integrity passes
- the required safety audit passes

`check_stored_integrity` treats an empty source path as nothing to protect. A source path with no stored snapshot fails closed.

## Recovery

`nightshift recover` never starts a provider and never increments `attempt`.

| Situation | Classification | Result |
| --- | --- | --- |
| Already terminal | `already_terminal` | left as-is |
| Still queued | `queued_not_started` | left queued |
| Preparing or running, matching process alive | `process_still_alive` | left as-is |
| Preparing, process gone | `interrupted_before_launch` | `INTERRUPTED` |
| Running, process gone, no provider exit, workspace present | `process_gone_workspace_intact` | `INTERRUPTED` |
| Running, process gone, workspace missing | `process_gone_workspace_missing` | `FAILED` |
| Provider exit recorded, verification not run | `verification_never_ran` | verification only, then `decide_final` |
| Provider exit and verification already recorded | `provider_exited` | `decide_final` from recorded codes |

A provider that exited 0, with verification that then exits 0, still becomes `FAILED` with `SOURCE_INTEGRITY_VIOLATION` when the stored snapshot no longer matches the source. Recovery cannot mark that run `SUCCEEDED`.

A live PID is accepted only when `ps` shows the session token. A recycled PID is not treated as the child.

`cancel` records `CANCELLED` before it signals the provider, inspect, or verification process group. It does not signal the preparing controller or Nightshift's own group. Locks stay until that child is gone.

## Grok adapter

`build_grok_argv` uses flags accepted by the installed Grok 1.0.46 CLI: `--cwd`, `--prompt-file`, `--output-format streaming-json`, `--permission-mode`, `--max-turns`, `--no-subagents`, `--no-memory`, `--no-auto-update`, `--rules`, `--session-id` or `--resume`, optional `--model`, `--disable-web-search`, and repeated `--deny` / `--allow`. `--no-memory` and `--no-auto-update` are accepted by this binary even though the help summary omits them. Nightshift creates the workspace itself; `--worktree` is not passed. `grok agent headless` is a WebSocket relay and is not the prompt API.

`--sandbox` is accepted by this binary. A startup check showed that `--sandbox workspace` applies when Grok is not already under `sandbox-exec`, and that the same flag inside Nightshift's seatbelt fails with `sandbox initialization failed: Operation not permitted`. Grok then refuses to start. Nested `sandbox_init` stays denied even under a wide outer profile. Nightshift therefore omits `--sandbox` and keeps the seatbelt. The invocation records `grok_sandbox_flag: omitted` and the write-scope profile name (`workspace` or `read-only`) as a label, not as an applied Grok jail.

The child also gets `GROK_MEMORY=0` and `GROK_WORKFLOWS=0`.

Invocation metadata stored on the run records binary version, model, session id, permission mode, the requested Grok sandbox profile name, the seatbelt label, max turns, memory and subagent flags, web-search status, deny and allow counts, the label `per-run` for `GROK_HOME`, isolation, neutralized relative paths, and the extension-audit counts. It does not store secrets or an absolute credential path.

`NIGHTSHIFT_FORBID_GROK=1` blocks the launch before `inspect`. That variable is an operator circuit breaker for tests.

The seatbelt wraps the provider with `/usr/bin/sandbox-exec`. See `docs/grok-runtime-isolation.md`.

## Logs and the morning report

`runs/<run-id>/` holds `metadata.json`, `prompt.final.md`, `provider.stdout.log`, `provider.stderr.log`, `events.jsonl`, `verification.log`, `report.md`, `provider.sb`, and `verification.sb`. The directory is mode 0700. Those files are mode 0600. Logs are kept. The report summarizes provider stdout and does not paste raw stderr. It says stderr was captured privately and omitted. It records duration, exit status, and host OS, release, architecture, and Python version. It does not record hostname or home directory.

The report's import section tells the human to review the clone and import commits by hand. Human review is required on every report, including `SUCCEEDED`.

## Resource limit

v0.2 records time and exit status. It does not schedule CPU, memory, or battery. The timeout is the job's `max_runtime_seconds`, enforced by killing the child process group. Verification steps have their own timeout and the same process-group kill.
