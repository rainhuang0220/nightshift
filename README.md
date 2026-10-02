# Nightshift

Nightshift is a local control plane for unattended overnight work. It queues a job, copies the recorded revision into an independent local clone, runs a provider inside a seatbelt and a private Grok profile, checks the original repository, and writes a morning report. A human imports any result. Nightshift does not merge and does not push.

This repository is the control plane. It is not tied to any target project. Point a job at a repository; Nightshift does not vendor that repository.

v0.2 runs on macOS with Python 3.11+ and the standard library. It uses `/usr/bin/sandbox-exec` for process containment. It does not need Docker, a server, or a cloud account.

## Install

From this repository:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/nightshift doctor
.venv/bin/nightshift auth grok bootstrap
```

Without installing, the same entry point is:

```bash
PYTHONPATH=src python3 -m nightshift doctor
```

`auth grok bootstrap` copies only the Grok auth file into `state/grok-profile/`. That directory is gitignored. The command prints whether the copy succeeded. It does not print the file.

Copy `config/nightshift.example.toml` to `config/nightshift.toml` only when you want to override the defaults. The example file is not loaded automatically.

## Commands

```text
nightshift doctor
nightshift auth grok status
nightshift auth grok bootstrap
nightshift job validate <path>
nightshift queue add <path>
nightshift queue list
nightshift run <path>
nightshift daemon
nightshift status
nightshift logs <run-id>
nightshift report [run-id]
nightshift recover
nightshift cancel <run-id>
nightshift safety probe
nightshift safety probe --provider grok
```

`run` and `queue add` accept `--provider fake` or `--provider grok`. `--root` puts `state/`, `runs/`, and `worktrees/` somewhere other than the current directory. Exit codes: 0 success, 1 failed or cancelled, 2 invalid usage or job, 3 unknown run, 4 blocked, 5 interrupted.

`safety probe` exits 0 for `PASS` and `PASS_WITH_LIMITATIONS`, and 1 for `FAIL`. The default probe is deterministic and does not call Grok. `--provider grok` is the sacrificial canary.

`recover` reconciles active rows with real processes. It does not relaunch a provider. `recover --retry` requeues a `FAILED` or `INTERRUPTED` run only when attempts remain.

## Overnight daemon

Keep the machine awake and run one job at a time:

```bash
caffeinate -i nightshift daemon
```

Stop it with Ctrl-C or SIGTERM. Queued jobs stay queued. Nightshift does not install a launchd job or any other persistent system service. You start it when you want it.

## Jobs

A job is a directory with `job.toml` and `prompt.md`. See `docs/job-format.md` and `jobs/examples/repo_audit/`. The example uses `provider = "fake"` and a placeholder repository path so validation cannot silently target a real checkout. Replace `repository` before you queue it.

The default `isolation` is `clone`. `isolation = "worktree"` is optional, shares the source git directory, and is not the unattended default.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Discover the tests by directory. A third-party package named `tests` may exist on the interpreter path; importing `tests.*` as a module name loads that package instead of this suite.

Tests use `FakeProvider` and temporary git repositories. They do not call Grok.

## Safety

Read `docs/threat-model.md` and `docs/grok-runtime-isolation.md` before a real Grok job. The layers, in the order Nightshift relies on them, are:

- model instructions, which are advisory
- Grok permission rules, which filter Grok tool calls
- a PATH shim, which is defense in depth for PATH lookup
- a seatbelt profile, which is the filesystem and process containment Nightshift controls
- an isolated `HOME` and `GROK_HOME`, which keep the operator's Grok extensions and credentials out of the child
- an independent clone, which keeps source refs and git metadata separate
- a source-integrity snapshot, which detects concurrent source changes and refuses `SUCCEEDED`
- human review, which is the only import path

`safety probe` exercises the clone, the seatbelt, the integrity gate, and the extension audit on a temporary repository. `safety probe --provider grok` adds one sacrificial Grok session. The provider seatbelt allows network so the model API can be reached, so a successful real probe is `PASS_WITH_LIMITATIONS`.

## Layout

Runtime directories `state/`, `state/grok-profile/`, `runs/`, and `worktrees/` are gitignored. Authoritative state is SQLite at `state/nightshift.db`, mode 0600. Each run writes logs under `runs/<run-id>/`, mode 0700 for the directory and 0600 for the logs.

Design notes live in `docs/architecture.md`. The v0.1 audit lives in `docs/security-audit-v0.1.md`. Contributor rules live in `AGENTS.md`.
