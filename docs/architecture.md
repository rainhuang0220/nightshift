# Architecture

Nightshift v0.1 is one process, one SQLite database, and one child provider at a time. The control plane never becomes a checkout of a target repository.

## Responsibilities

The operator writes a job and approves what it is allowed to touch. Nightshift queues the job, locks it, builds an isolated worktree, starts the provider, streams logs, runs verification, writes the report, and releases the lock. The provider process does the reasoning. v0.1 ships a Grok CLI adapter and a FakeProvider used by tests and dry runs. Cursor is rejected at validation. Codex is not a provider.

## Modules

| Module | Role |
| --- | --- |
| `cli.py` | argparse entry point |
| `config.py` | root, paths, concurrency, permission mode |
| `models.py` | states, the only transition table, job and run records |
| `job.py` | TOML + `prompt.md` parsing |
| `db.py` | SQLite runs, events, locks; transactions |
| `queue.py` | enqueue and conditional claim |
| `locks.py` | concurrency-group, repo, and workspace locks |
| `workspace.py` | inspect a source repo and add a detached worktree |
| `gitutil.py` | git subprocess helpers used by Nightshift itself |
| `policy.py` | allow/deny rules, preamble, environment minimization |
| `guard.py` | PATH shims for git, gh, sudo, kaggle, npm, twine |
| `runner.py` | one run: prepare, execute, verify, report |
| `supervisor.py` | daemon loop, recover, cancel |
| `report.py` | morning report text |
| `doctor.py` | local environment check |
| `providers/base.py` | request, result, subprocess, termination |
| `providers/grok.py` | argv builder and Grok process adapter |
| `providers/fake.py` | deterministic provider |
| `providers/fake_child.py` | child program the fake provider execs |
| `testkit.py` | fixtures imported by the suite |

`job.py`, `guard.py`, `gitutil.py`, `fake_child.py`, and `testkit.py` are extra modules so parsing, the PATH policy, and git calls stay out of the supervisor. `testkit.py` lives in the package because a site-packages distribution named `tests` would otherwise shadow a top-level test helper.

`--root` (or `NIGHTSHIFT_ROOT`) relocates `state/`, `runs/`, and `worktrees/`. Tests and the bootstrap dry run use it so they never write into a real project.

## State

SQLite at `state/nightshift.db` is authoritative. The connection uses WAL and `synchronous=FULL`. State changes run inside `BEGIN IMMEDIATE`. `transition` updates a row only when the current state is the expected one, and `ensure_transition` rejects anything outside `LEGAL_TRANSITIONS`.

```text
QUEUED -> PREPARING -> RUNNING -> VERIFYING -> SUCCEEDED
```

`FAILED` and `INTERRUPTED` may return to `QUEUED` only through an explicit retry, which increments `attempt` and stops at `max_attempts`. `BLOCKED`, `CANCELLED`, and `SUCCEEDED` have no outbound edge. Active states are `QUEUED`, `PREPARING`, `RUNNING`, and `VERIFYING`.

Timestamps are UTC with microseconds so two inserts in the same second keep queue order.

## One run

1. `queue add` stores the parsed job JSON and the resolved repository path.
2. `claim_next` moves `QUEUED` to `PREPARING` and takes the group and repo locks, skipping a group or repo still held by a live owner.
3. The runner checks the source is a git repo, records HEAD and `git status --porcelain=v1`, and runs `git worktree add --detach` under `worktrees/<run-id>`. The source HEAD, index, and working tree are not cleaned, reset, or stashed.
4. The provider starts with that worktree as cwd, a new session id, and a minimized environment. Stdout and stderr append to the run directory. The PID and a match token (the session id on the argv) are stored.
5. After the child exits, snapshotted verification commands run in the worktree. They come from the job recorded before launch, not from provider output.
6. Nightshift re-reads the source HEAD and porcelain. A difference, a non-zero provider, or a non-zero verification fails the run.
7. The report is written. Locks are released. Successful worktrees are kept. Failed worktrees are kept unless `destroy_failed_worktrees` is true.

`nightshift run` does the same path without the daemon. The daemon calls `recover` first (no relaunch), then claims while active `PREPARING`/`RUNNING`/`VERIFYING` rows are below `concurrency`. The default concurrency is 1. The claim and lock gate is the place a later version can run more than one job.

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
| Provider exit recorded, verification not run | `verification_never_ran` | verification only |
| Provider exit and verification already recorded | `provider_exited` | finalize from recorded codes |

A live PID is accepted only when `ps` shows the session token. A recycled PID is not treated as the child.

`cancel` moves an active run to `CANCELLED`, signals the child process group when the token matches, and releases locks.

## Grok adapter

`build_grok_argv` uses flags present on the installed Grok 1.0.46 CLI: `--cwd`, `--prompt-file`, `--output-format plain`, `--permission-mode`, `--sandbox`, `--max-turns`, `--no-subagents`, `--rules`, `--session-id` or `--resume`, optional `--model`, `--disable-web-search`, and repeated `--deny` / `--allow`. Nightshift creates the worktree; `--worktree` is not passed. `grok agent headless` is a WebSocket relay and is not the prompt API. `NIGHTSHIFT_FORBID_GROK=1` makes the adapter return exit 126 without spawning. That variable is an operator circuit breaker for tests, not a security boundary.

## Logs and the morning report

`runs/<run-id>/` holds `metadata.json`, `prompt.final.md`, `provider.stdout.log`, `provider.stderr.log`, `events.jsonl`, `verification.log`, and `report.md`. Logs are append-only. The report is a projection of SQLite plus the workspace diff. It records duration, exit status, and host OS, release, architecture, and Python version. It does not record hostname or home directory. Human review is required on every v0.1 report.

## Resource limit

v0.1 records time and exit status. It does not schedule CPU, memory, or battery. The timeout is the job's `max_runtime_seconds`, enforced by the supervisor killing the child process group.
