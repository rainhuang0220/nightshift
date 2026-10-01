# Nightshift v0.1 handoff

Local control plane. No remote. No launchd service. No live Grok job was started while building this version.

## Architecture summary

Nightshift queues a versioned `job.toml` plus `prompt.md`, locks the concurrency group and repository, and runs the provider inside a detached git worktree it creates under `worktrees/`. SQLite (`state/nightshift.db`, WAL, `synchronous=FULL`) is the authority for run state. The legal edges live only in `LEGAL_TRANSITIONS`. The supervisor is single-threaded with concurrency 1. `recover` classifies active rows and does not relaunch a provider. `recover --retry` is the only way back to `QUEUED`, and it stops at `max_attempts`.

The Grok adapter builds a process argv from flags on the installed Grok 1.0.46 CLI. It does not plan. Tests and the bootstrap dry run use FakeProvider. `NIGHTSHIFT_FORBID_GROK=1` makes the Grok adapter return without spawning.

`--root` relocates `state/`, `runs/`, and `worktrees/`. Those directories are gitignored.

## Files and modules

```text
README.md
AGENTS.md
HANDOFF.md
pyproject.toml
config/nightshift.example.toml
jobs/examples/repo_audit/job.toml
jobs/examples/repo_audit/prompt.md
docs/architecture.md
docs/job-format.md
docs/threat-model.md
src/nightshift/__init__.py
src/nightshift/__main__.py
src/nightshift/cli.py
src/nightshift/config.py
src/nightshift/models.py
src/nightshift/job.py
src/nightshift/db.py
src/nightshift/queue.py
src/nightshift/locks.py
src/nightshift/workspace.py
src/nightshift/gitutil.py
src/nightshift/policy.py
src/nightshift/guard.py
src/nightshift/runner.py
src/nightshift/supervisor.py
src/nightshift/report.py
src/nightshift/doctor.py
src/nightshift/testkit.py
src/nightshift/providers/__init__.py
src/nightshift/providers/base.py
src/nightshift/providers/grok.py
src/nightshift/providers/fake.py
src/nightshift/providers/fake_child.py
tests/test_cli.py
tests/test_job_and_policy.py
tests/test_recover_and_report.py
tests/test_state_and_db.py
tests/test_workspace_and_guard.py
```

`job.py`, `guard.py`, `gitutil.py`, `fake_child.py`, and `testkit.py` are extra modules. Parsing, the PATH policy, and git calls stay out of the supervisor. `testkit.py` lives in the package because a site-packages distribution named `tests` shadows a top-level import of that name.

## CLI commands

```text
nightshift doctor
nightshift job validate <path>
nightshift queue add <path> [--provider fake|grok]
nightshift queue list
nightshift run <path> [--provider fake|grok]
nightshift daemon
nightshift status
nightshift logs <run-id>
nightshift report [run-id]
nightshift recover [--retry]
nightshift cancel <run-id>
nightshift version
```

Global flags: `--root`, `--config`.

Exit codes: 0 success, 1 failed or cancelled, 2 invalid job or usage, 3 unknown run, 4 blocked, 5 interrupted.

## Exact test results

Command:

```text
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Result:

```text
Ran 34 tests in 9.598s
OK
```

All 34 methods passed, including job validation, illegal transitions, SQLite reopen, queue order, locks, dirty-worktree isolation, PATH-shim denies, FakeProvider success and failure, every recover class, report sections, Grok argv denies without `--always-approve` or `bypassPermissions`, daemon drain of one queued fake job, and child-group termination.

`python3 -m compileall -q src tests` exited 0. `ruff` and `black` are not installed; that absence is not a failure.

`nightshift doctor` twice, both exit 0, identical conclusions:

```text
[pass] Python 3.14.7 (>= 3.11)
[pass] writable state
[pass] SQLite
[pass] Grok CLI: grok 1.0.46 (2765805b9442)
[pass] git
```

`nightshift job validate jobs/examples/repo_audit` exited 0: `valid repo-audit-example provider=fake write_scope=none`. A manifest missing required fields exited 2 and named `missing required field 'id'`.

Two FakeProvider `nightshift run` invocations, each against its own throwaway root and its own dirty temporary git repository:

| Run | State | Attempt | Source HEAD before and after | Porcelain |
| --- | --- | --- | --- | --- |
| `ns-a156addb6add` | SUCCEEDED | 1 | `9e9e5135f44f150a586b6860d9c3d12d6187db3c` | `?? DIRTY.txt` unchanged |
| `ns-3d8b00ae5f8f` | SUCCEEDED | 1 | `a6abd9f9d2ec843ed037ca18dd9a34353ad0c988` | `?? DIRTY.txt` unchanged |

Each run directory contained `metadata.json`, `prompt.final.md`, `provider.stdout.log`, `provider.stderr.log`, `events.jsonl`, `verification.log`, and `report.md`. Each report named that repository and the original revision, recorded local commit `nightshift: record fake provider note`, and set `Human review required: yes`.

`nightshift recover` on a `RUNNING` row whose PID was dead (`999999`) and whose workspace directory still existed printed `INTERRUPTED process_gone_workspace_intact attempt=1` and did not launch a provider. `nightshift cancel` on a queued run printed `CANCELLED`. `queue list`, `status`, `logs`, and `report` showed those run ids and outcomes.

## Safety guarantees

- The provider cwd is a Nightshift-created detached worktree. Nightshift does not clean, reset, stash, or checkout the source tree.
- A changed source HEAD or porcelain fails the run. Failed worktrees are kept unless `destroy_failed_worktrees` is set.
- Default Grok mode is `dontAsk` plus explicit allow and deny rules. `--always-approve`, `bypassPermissions`, `--worktree`, and `grok agent headless` are not emitted.
- PATH shims refuse push, sudo, Kaggle, package publish, mutating `gh`, and git writes outside the worktree, for PATH lookup of those tools.
- `GIT_CONFIG_GLOBAL` points at an empty file in the run directory. `GIT_CONFIG_NOSYSTEM=1`.
- Child environment drops names that look like secrets. Logs redact common assignment shapes.
- `recover` does not restart work and does not increment `attempt`. Retry is explicit and capped.
- Locks stop two runs from sharing a concurrency group or a live workspace.
- Verification commands are the operator's snapshotted list, not model output.

## Safety limitations

- The prompt preamble is advisory. It is not a security boundary.
- Grok deny rules are hard only inside Grok's permission engine.
- `--sandbox workspace` and `--sandbox read-only` are requested from the installed user guide. Seatbelt was not probed. If a profile fails to apply, Grok continues without that enforcement. This is unverified.
- On macOS, Grok's child-network sandbox is a documented no-op. Offline policy is `--disable-web-search` and withholding web tools. `curl`, `wget`, and `npx` are not wrapped.
- An absolute binary such as `/usr/bin/git` bypasses the PATH shim. `pnpm`, `yarn`, and `cargo` are not wrapped except where a Grok deny names them.
- A linked worktree shares the source repo's git config. Shimmed git refuses config writes and forces an empty `core.hooksPath`. An absolute git binary can still write that shared config.
- The post-run audit detects source mutation. It does not revert it.
- `HOME` remains in the child environment. Credential files on disk are not removed.
- `NIGHTSHIFT_FORBID_GROK` is an operator switch, not a sandbox.
- Verification runs the operator's shell commands. A dangerous verification line runs.

## Known issues

- `recover` records `--resume` support in the Grok argv builder and does not call it. A dead provider stays `INTERRUPTED` until a human passes `--retry`, which starts a new attempt rather than resuming the session.
- The example job's repository is the placeholder `REPLACE_WITH_REPOSITORY_PATH`. Validation succeeds. Queueing it unchanged blocks.
- Human review is required on every v0.1 report, including `SUCCEEDED`.
- Success criteria are prose in the report. They are not evaluated.
- Concurrency defaults to 1. The claim and lock gate is the extension point, and the daemon is still one thread.
- Doctor creates `state/` on first run. That directory is gitignored.
- Run the suite with `python3 -m unittest discover -s tests`. Importing `tests.<module>` can load an unrelated site-packages package named `tests`.

## Next recommended milestone

Prove the Grok sandbox on one throwaway repository. Run a single short `provider = "grok"` job with `write_scope = "workspace"`, `network = false`, and a small verification command. Inspect whether Grok warns that the sandbox profile did not apply. Do not point that job at a repository you need. After that, decide whether `recover` should grow a real `--resume` path.

## Commands to run next

```bash
cd ~/nightshift
.venv/bin/nightshift doctor
PYTHONPATH=src python3 -m unittest discover -s tests -v
caffeinate -i .venv/bin/nightshift daemon
```

Stop the daemon with Ctrl-C. Queued jobs stay queued. Nightshift does not install a launchd job. Read `docs/threat-model.md` before the first real Grok job.
