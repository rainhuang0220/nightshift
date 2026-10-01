# Nightshift

Nightshift is a local control plane for unattended overnight work. It queues jobs, isolates each job in its own git worktree, runs a provider, records the result, and writes a morning report. The model does the reasoning. Nightshift does the bookkeeping.

This repository is the control plane. It is not tied to any target project. Point a job at a repository; Nightshift does not vendor that repository.

v0.1 runs on macOS with Python 3.11+ and the standard library. It does not need Docker, a server, or a cloud account.

## Install

From this repository:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/nightshift doctor
```

Without installing, the same entry point is:

```bash
PYTHONPATH=src python3 -m nightshift doctor
```

Copy `config/nightshift.example.toml` to `config/nightshift.toml` only when you want to override the defaults. The example file is not loaded automatically.

## Commands

```text
nightshift doctor
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
```

`run` and `queue add` accept `--provider fake` or `--provider grok`. `--root` puts `state/`, `runs/`, and `worktrees/` somewhere other than the current directory. Exit codes: 0 success, 1 failed or cancelled, 2 invalid usage or job, 3 unknown run, 4 blocked, 5 interrupted.

`recover` reconciles active rows with real processes. It does not relaunch a provider. `recover --retry` requeues a `FAILED` or `INTERRUPTED` run only when attempts remain.

## Overnight daemon

Keep the machine awake and run one job at a time:

```bash
caffeinate -i nightshift daemon
```

Stop it with Ctrl-C or SIGTERM. Queued jobs stay queued. Nightshift does not install a launchd job or any other persistent system service. You start it when you want it.

## Jobs

A job is a directory with `job.toml` and `prompt.md`. See `docs/job-format.md` and `jobs/examples/repo_audit/`. The example uses `provider = "fake"` and a placeholder repository path so validation cannot silently target a real checkout. Replace `repository` before you queue it.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Discover the tests by directory. A third-party package named `tests` may exist on the interpreter path; importing `tests.*` as a module name loads that package instead of this suite.

Tests use `FakeProvider` and temporary git repositories. They do not call Grok.

## Safety

The default Grok invocation is `dontAsk` plus explicit allow and deny rules, a sandbox profile, a PATH policy for `git` and a few other tools, and a post-run check that the source checkout did not change. Read `docs/threat-model.md` before trusting a control. The prompt text is an instruction, not a security boundary.

## Layout

Runtime directories `state/`, `runs/`, and `worktrees/` are gitignored. Authoritative state is SQLite at `state/nightshift.db`. Each run also writes logs under `runs/<run-id>/`.

Design notes live in `docs/architecture.md`. Contributor rules live in `AGENTS.md`.
