# AGENTS.md

Nightshift is a local control plane. Future changes keep it small, deterministic, and inspectable.

## Operating rules

Prefer reliability over cleverness. Add a test for a behavior before treating that behavior as done.

- Keep the standard library. Add no unnecessary dependencies.
- Keep provider logic behind an interface. `nightshift.providers.base` owns process lifetime. `FakeProvider` and `GrokProvider` are adapters. Providers do not contain planning or reasoning.
- Use FakeProvider for tests. Tests set `NIGHTSHIFT_FORBID_GROK=1`. A test that launches Grok, pushes, or contacts a network is a broken test.
- Tests create temporary git repositories, with no mutation of real target working trees and no remote side effects in tests.
- State changes go through `LEGAL_TRANSITIONS` in `src/nightshift/models.py`. Add tests for state transitions when the table changes. Illegal transitions must fail.
- Isolation is `git worktree add` under `worktrees/`. Leave the source HEAD, index, and working tree alone, including when that tree is dirty.
- Take no destructive actions against a source checkout: no clean, reset, stash, or checkout there. Failed worktrees stay on disk unless `destroy_failed_worktrees` is set.
- `recover` classifies and reconciles. It does not relaunch a provider. Retry is only `recover --retry`, and it stops at `max_attempts`.
- Grok argv stays on `dontAsk` with explicit `--allow` and `--deny`. Do not emit `--always-approve`, `--permission-mode bypassPermissions`, `--worktree`, or `grok agent headless`.
- Logs and host info omit secrets, home directories, and hostnames. Scrub is best-effort; do not log credentials in the first place.
- Do not install launchd or any other persistent system service.

## Where behavior lives

| Question | Read |
| --- | --- |
| Legal run states | `src/nightshift/models.py` |
| Job schema | `docs/job-format.md`, `src/nightshift/job.py` |
| What is enforced vs advisory | `docs/threat-model.md`, `src/nightshift/policy.py`, `src/nightshift/guard.py` |
| Process model and recovery | `docs/architecture.md`, `src/nightshift/supervisor.py` |
| Morning report sections | `src/nightshift/report.py` |

Run the suite with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Use `discover -s tests`. `python3 -m unittest tests.<module>` imports a different top-level `tests` package when one is installed for that interpreter.

## Review bar

A change is ready when the suite passes, `nightshift doctor` passes, and a FakeProvider `nightshift run` against a dirty temporary repository leaves that repository's HEAD and `git status --porcelain` unchanged.
