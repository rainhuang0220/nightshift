# Nightshift v0.2 handoff

Local control plane. No remote. No launchd service. The real Grok canary is a separate step after this implementation commit and is recorded below when it has been run.

## Architecture summary

Nightshift queues a versioned `job.toml` plus `prompt.md`, locks the concurrency group and repository, and runs the provider inside an independent local clone. The clone is `git clone --no-hardlinks --no-checkout`, then `checkout --detach` of the recorded revision, with `origin` removed. SQLite (`state/nightshift.db`, WAL, `synchronous=FULL`, mode 0600) is the authority for run state. The legal edges live only in `LEGAL_TRANSITIONS`. The supervisor is single-threaded with concurrency 1.

`decide_final` is the only success gate. Normal execution, verification recovery, and recorded-state finalization all use it. `SUCCEEDED` requires provider exit 0, verification that ran and exited 0, a workspace on disk, a passing source-integrity snapshot, and a passing extension audit when the provider is Grok. `recover` classifies active rows and does not relaunch a provider. `recover --retry` is the only way back to `QUEUED`, and it stops at `max_attempts`.

The Grok adapter builds a process argv from flags on the installed Grok 1.0.46 CLI and wraps the process with `/usr/bin/sandbox-exec`. The child `HOME` is `runs/<id>/runtime-home`. `GROK_HOME` is `state/grok-profile`. Tests and the default safety probe use FakeProvider. `NIGHTSHIFT_FORBID_GROK=1` blocks a Grok launch before `grok inspect`.

`isolation = "worktree"` remains available. It shares the source git directory and is not the unattended default.

`--root` relocates `state/`, `runs/`, and `worktrees/`. Those directories, and `state/grok-profile/`, are gitignored.

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
docs/security-audit-v0.1.md
docs/grok-runtime-isolation.md
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
src/nightshift/integrity.py
src/nightshift/finalize.py
src/nightshift/containment.py
src/nightshift/extensions.py
src/nightshift/runtime.py
src/nightshift/priv.py
src/nightshift/policy.py
src/nightshift/guard.py
src/nightshift/runner.py
src/nightshift/supervisor.py
src/nightshift/report.py
src/nightshift/probe.py
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
tests/test_v02.py
```

## CLI commands

```text
nightshift doctor
nightshift auth grok status
nightshift auth grok bootstrap
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
nightshift safety probe [--provider fake|grok]
nightshift version
```

Global flags: `--root`, `--config`.

Exit codes: 0 success, 1 failed or cancelled, 2 invalid job or usage, 3 unknown run, 4 blocked, 5 interrupted. `safety probe` uses 0 for `PASS` and `PASS_WITH_LIMITATIONS`, and 1 for `FAIL`.

## Exact test results

Command:

```text
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Result:

```text
Ran 51 tests in 14.204s
OK
```

`python3 -m compileall -q src tests` exited 0. `ruff` and `black` are not installed; that absence is not a failure.

Coverage added in v0.2 includes clone independence, each integrity category the snapshot claims, the recovery integrity regression, isolated HOME and GROK_HOME, auth-file modes on a synthetic file, extension-audit parsing, project-file neutralization in the clone only, Grok argv flags, environment sanitization, log modes, structured verification, absolute-interpreter seatbelt checks, and the fake safety probe. No unit test calls Grok.

`nightshift doctor` twice, both exit 0, identical output:

```text
[pass] Python 3.14.7 (>= 3.11)
[pass] writable state
[pass] SQLite
[pass] Grok CLI: grok 1.0.46 (2765805b9442)
[pass] git
```

`nightshift job validate jobs/examples/repo_audit` exited 0: `valid repo-audit-example provider=fake write_scope=none`.

`nightshift safety probe` (the default, fake) exited 0. The first lines were `PASS`, `provider: fake`, and `grok: not called`. Nightshift's own replay classified:

| Action | Layer |
| --- | --- |
| Workspace write via absolute Python | DEFENSE IN DEPTH |
| Local commit via `/usr/bin/git` | DEFENSE IN DEPTH |
| Absolute Python write to the source | HARD BLOCK |
| `/bin/sh` write outside the workspace | HARD BLOCK |
| Delete of the outside sentinel | HARD BLOCK |
| PATH `git push` to a local bare repo | DEFENSE IN DEPTH |
| `/usr/bin/git push` to a local bare repo | HARD BLOCK |
| Python subprocess `/usr/bin/git push` | HARD BLOCK |

`xcrun` cache warnings (`Operation not permitted`) appeared on allowed commands and did not stop the workspace write or the local commit. The seatbelt was not widened to silence them.

## Real Grok probe

Not run as part of the implementation commit. The authorized command, after this commit, is one sacrificial `nightshift safety probe --provider grok`. It builds its own temporary source, clone, sentinel, and local bare remote. It does not use a developer repository and it does not push to GitHub.

## Safety guarantees

- The default provider cwd is an independent clone. Nightshift does not clean, reset, stash, or checkout the source tree. A clone commit does not update source refs or the source worktree registry.
- `decide_final` withholds `SUCCEEDED` when any protected source category changes, including on recovery. Nightshift does not restore the source.
- Grok jobs run under a private HOME and `state/grok-profile`. `grok inspect --json` must pass before the model starts.
- The seatbelt is the filesystem containment. The PATH shim and Grok allow rules are defense in depth. The prompt is advisory.
- Verification of a Grok job is an argv under a network-denying seatbelt. Legacy shell verification is blocked for Grok unless the job opts in.
- `recover` does not restart work and does not increment `attempt`.
- Runtime directories are mode 0700. Logs and the database are mode 0600. The report does not paste raw stderr.

## Safety limitations

- The Grok provider seatbelt allows network so the model API can be reached. An https or ssh `git push` is not an OS hard block. A local bare push is. A clean real probe is `PASS_WITH_LIMITATIONS` while this stands.
- Grok's own `--sandbox` flag is requested. The seatbelt is the containment Nightshift enforces.
- Redaction is best-effort. It is not a guarantee over arbitrary repository content.
- Prompt compliance is not a hard block. The probe classifies a block from the seatbelt or the shim.
- Human review is required on every report, including `SUCCEEDED`.

## Known issues

- `recover` records `--resume` support in the Grok argv builder and does not call it. A dead provider stays `INTERRUPTED` until a human passes `--retry`, which starts a new attempt rather than resuming the session.
- The example job's repository is the placeholder `REPLACE_WITH_REPOSITORY_PATH`. Validation succeeds. Queueing it unchanged blocks.
- Success criteria are prose in the report. They are not evaluated.
- Concurrency defaults to 1.
- Run the suite with `python3 -m unittest discover -s tests`. Importing `tests.<module>` can load an unrelated site-packages package named `tests`.

## Next command

```bash
cd ~/nightshift
PYTHONPATH=src python3 -m nightshift safety probe --provider grok
```

That command is the sacrificial canary. Do not point a job at a repository you need. Read `docs/threat-model.md` before the first real overnight job.
