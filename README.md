# Nightshift

Nightshift is a local control plane for unattended overnight work. You write a job. Nightshift queues it, copies one recorded revision into an independent Git clone, runs a provider inside a seatbelt and a private runtime profile, checks that the original repository did not change, and writes a morning report. You import any result yourself.

It exists so a long model session can work while you are away without becoming a silent channel into your real checkout, your credentials, or your normal editor extensions. The model does the reasoning. Nightshift does the bookkeeping and the checks.

This repository is the control plane. It does not vendor a target project. Point a job at a repository when you are ready.

**Status: 0.2.1, alpha.** The supported host is macOS with Python 3.11+ and `/usr/bin/sandbox-exec`. The sacrificial Grok canary is `PASS_WITH_LIMITATIONS`. Nightshift does not merge, push, or install a system service.

## Architecture

```text
job snapshot
    |
    v
read-only source
    |
    v
independent local clone
    |
    v
private HOME and GROK_HOME
    |
    +--------+--------+
    |                 |
permission rules   seatbelt
    |                 |
    +--------+--------+
             |
             v
      verification
             |
             v
   source-integrity gate
             |
             v
      morning report
             |
             v
      human import
```

The default workspace is an independent clone (`git clone --no-hardlinks`, detached checkout of the recorded revision, `origin` removed). `isolation = "worktree"` still exists. It shares the source Git directory, is labeled weaker isolation, and is not the default.

Design notes: `docs/architecture.md`, `docs/threat-model.md`, `docs/grok-runtime-isolation.md`.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/nightshift doctor
```

From a checkout, without installing:

```bash
PYTHONPATH=src python3 -m nightshift doctor
```

Copy `config/nightshift.example.toml` to `config/nightshift.toml` only when you want to override paths or concurrency. The example file is not loaded automatically. `config/nightshift.toml` is gitignored.

## Doctor

```bash
nightshift doctor
```

Doctor checks Python, a writable state directory, SQLite, Git, and whether the Grok CLI is on `PATH`. It does not launch a model.

## Fake safety probe

```bash
nightshift safety probe
```

The default probe is deterministic. It builds a temporary repository and does not call Grok. Exit 0 means `PASS`. Exit 1 means `FAIL`.

## Grok auth bootstrap

Unattended Grok keeps auth at `state/credentials/grok/auth.json` and uses a new `GROK_HOME` per attempt, not your normal Grok home.

```bash
nightshift auth grok bootstrap
nightshift auth grok status
```

Bootstrap copies only the Grok auth file into that store. The directory is mode 0700 and the file is mode 0600. The command does not print the file. The store is gitignored. Each attempt copies it into `runs/<id>/attempt-<n>/grok-home` and removes that copy when the attempt ends.

## Real safety probe

```bash
nightshift safety probe --provider grok
```

This runs one sacrificial Grok session in a temporary repository, with an outside sentinel and a local bare remote. It does not use your project and it does not push to GitHub. Run it only when you mean to. The recorded v0.2 result is `PASS_WITH_LIMITATIONS`.

## Jobs

A job is a directory with `job.toml` and `prompt.md`. See `docs/job-format.md` and `jobs/examples/repo_audit/`.

```bash
nightshift job validate jobs/examples/repo_audit
nightshift run path/to/job --provider fake
nightshift queue add path/to/job
nightshift queue list
```

The example job uses `provider = "fake"` and the placeholder repository `REPLACE_WITH_REPOSITORY_PATH`. Validation accepts it. Queueing it unchanged blocks, because that placeholder is not a Git checkout. Replace `repository` before you queue a real job.

`run` and `queue add` accept `--provider fake` or `--provider grok`. `--root` relocates `state/`, `runs/`, and `worktrees/`.

## Daemon

Keep the machine awake and run one job at a time:

```bash
caffeinate -i nightshift daemon
```

Stop it with Ctrl-C or SIGTERM. Queued jobs stay queued. Nightshift does not install a launchd job. You start the daemon when you want it.

Other commands: `status`, `logs`, `report`, `recover`, `cancel`. `recover` reconciles active rows with real processes and does not relaunch a provider. `recover --retry` requeues a failed or interrupted run only when attempts remain.

Exit codes: 0 success, 1 failed or cancelled, 2 invalid usage or job, 3 unknown run, 4 blocked, 5 interrupted. The safety probe uses 0 for `PASS` and `PASS_WITH_LIMITATIONS`, and 1 for `FAIL`.

## Safety model

Nightshift relies on these layers, in this order:

- model instructions, which are advisory
- Grok permission rules, which filter Grok tool calls
- a PATH shim, which is defense in depth for PATH lookup
- a seatbelt profile, which is the filesystem and process containment Nightshift controls
- an isolated `HOME` and `GROK_HOME`, which keep your normal Grok extensions and credentials out of the child
- an independent clone, which keeps source refs and Git metadata separate
- a source-integrity snapshot, which detects concurrent source changes and refuses `SUCCEEDED`
- human review, which is the only import path

`grok inspect --json` must pass before a real Grok launch. User hooks, plugins, external MCP servers, and imported Claude, Cursor, or Codex extensions block the launch. Project instruction files can still influence reasoning. They do not, by themselves, grant tools.

## Limitations

The provider needs network connectivity to reach the model API. Arbitrary external network mutation, including an https or ssh `git push`, is not currently an operating-system hard block. Verification runs with network denied. A push to a local path outside the writable roots is a filesystem hard block.

A verification exit of 0 means the declared check passed inside the isolated mutable workspace. It does not mean a model that can edit that workspace was unable to influence the check. Source integrity, filesystem containment, credential isolation, and human review remain the security boundaries.

Provider writes are the workspace, when the job allows it, and that attempt's private runtime directories. Nightshift control files, profiles, shims, and the verification runtime are not writable by the provider. Trusted log and report writes do not follow symlinks.

Grok's own `--sandbox` flag is not passed. Nested sandbox setup fails inside the seatbelt, and Grok then refuses to start.

Redaction of logs is best-effort. It is not a guarantee over arbitrary repository content.

The morning report is a summary plus the integrity result. It is not a complete audit of every side effect. Human review is required on every report, including `SUCCEEDED`.

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Discover the tests by directory. Tests use temporary Git repositories and do not call Grok.

## Project

- Changelog: `CHANGELOG.md`
- License: Apache-2.0, `LICENSE`. Copyright 2026 rainhuang0220.
- Contributing: `CONTRIBUTING.md`
- Security reports: `SECURITY.md`
- Version: `src/nightshift/__init__.py` (`__version__`)

Repository: <https://github.com/rainhuang0220/nightshift>
