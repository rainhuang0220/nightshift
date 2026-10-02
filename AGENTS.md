# AGENTS.md

Nightshift is a local control plane. Future changes keep it small, deterministic, and inspectable.

## Operating rules

Prefer reliability over cleverness. Add a test for a behavior before treating that behavior as done.

- Keep the standard library. Add no unnecessary dependencies.
- Keep provider logic behind an interface. `nightshift.providers.base` owns process lifetime. `FakeProvider` and `GrokProvider` are adapters. Providers do not contain planning or reasoning.
- Use FakeProvider for tests. Tests set `NIGHTSHIFT_FORBID_GROK=1`. A test that launches Grok, pushes, or contacts a network is a broken test.
- Tests create temporary git repositories, with no mutation of real target working trees and no remote side effects in tests.
- State changes go through `LEGAL_TRANSITIONS` in `src/nightshift/models.py`. Add tests for state transitions when the table changes. Illegal transitions must fail.
- Default isolation is an independent local clone: `git clone --no-hardlinks --no-checkout`, then `checkout --detach` of the recorded revision, then remove `origin`. The clone has its own refs, config, hooks path, and index. Do not use `--shared`.
- `isolation = "worktree"` remains available and is labeled weaker isolation because it shares the source git directory. It is not the default for unattended jobs. `git worktree add` updates the source `.git/worktrees` registry.
- Leave the source HEAD, index, working tree, refs, config, hooks, and packed-refs alone, including when that tree is dirty. Nightshift does not clean, reset, stash, or checkout the source.
- Failed workspaces stay on disk unless `destroy_failed_worktrees` is set. Clone cleanup deletes only the clone directory and refuses a path that overlaps the source.
- Success goes through `decide_final` in `src/nightshift/finalize.py`. Normal execution, verification recovery, and recorded-state finalization all use it. `SUCCEEDED` requires provider exit 0, verification ran with exit 0, a workspace on disk, a passing source-integrity check, and a passing safety audit when the provider is Grok. A missing integrity baseline fails closed when a source repository is set.
- `SOURCE_INTEGRITY_VIOLATION` fails the run. Nightshift does not restore the source. A human may have changed it on purpose.
- `recover` classifies and reconciles. It does not relaunch a provider and does not resume a Grok session. Retry is only `recover --retry`, and it stops at `max_attempts`.
- Grok argv stays on `dontAsk` with explicit `--allow` and `--deny`, plus `--no-subagents`, `--no-memory`, and `--disable-web-search` when `network` is false. Do not emit `--always-approve`, `--permission-mode bypassPermissions`, `--worktree`, or `grok agent headless`.
- Before a real Grok launch, `grok inspect --json` must pass `ExtensionSurfaceAudit`. User, project, and plugin hooks, external MCP servers, user and project plugins, and Claude/Cursor/Codex imports block the run as `BLOCKED_EXTENSION_SURFACE`. Project instruction files such as `AGENTS.md` are untrusted reasoning and are not, by themselves, a block.
- Neutralize project extension config only inside the isolated clone. Never edit those files in the source repository.
- Provider children get a per-attempt `HOME` (`runs/<id>/attempt-<n>/runtime-home`, mode 0700) and a per-attempt `GROK_HOME` (`runs/<id>/attempt-<n>/grok-home`). Verification gets a fresh runtime under `runs/<id>/attempt-<n>/verify-runtime/` after the provider exits. Do not inherit the operator's skills, plugins, hooks, MCP credentials, memory, or sessions, and do not reuse one attempt's `GROK_HOME` for the next attempt.
- `nightshift auth grok bootstrap` may copy `auth.json` into `state/credentials/grok/`. Never print auth contents, never commit them, and never symlink the Nightshift copy onto the operator's live auth file. The store directory is mode 0700. The auth file is mode 0600. Remove only the per-attempt copy after a finished or dead attempt. A live phase keeps its copy. Do not delete the persistent store.
- Filesystem containment is the seatbelt profile written beside the run. Reads are deny-by-default: system and toolchain roots, then a deny of the operator home and the original checkout, then a re-allow of the workspace, run directory, runtime home, per-attempt `GROK_HOME`, and explicit config `read_roots`. A job prompt cannot add roots. The PATH shim and Grok allow rules are defense in depth. If `sandbox-exec` is missing, refuse the run. Do not restore a global file-read allow.
- The Grok provider profile allows network so the model API can be reached. Verification denies network. Do not describe that provider network allowance as a closed network jail.
- Structured `[[verification]] argv` is the unattended default. A shell-string verification step is legacy. Grok jobs with legacy shell verification are blocked unless `allow_legacy_shell_verification` is set.
- Logs and host info omit secrets, home directories, and hostnames. Runtime directories are mode 0700 and runtime logs are mode 0600. Scrub is best-effort. Arbitrary repository content can contain unknown secret formats, so redaction is not a cryptographic guarantee. Do not delete diagnostic logs automatically.
- Do not install launchd or any other persistent system service. Do not add a remote, push, or open a pull request from this control plane.

## Where behavior lives

| Question | Read |
| --- | --- |
| Legal run states | `src/nightshift/models.py` |
| The only success gate | `src/nightshift/finalize.py` |
| Job schema | `docs/job-format.md`, `src/nightshift/job.py` |
| Clone and worktree backends | `src/nightshift/workspace.py` |
| Source integrity snapshot | `src/nightshift/integrity.py` |
| Seatbelt profiles | `src/nightshift/containment.py` |
| Grok HOME, GROK_HOME, auth copy | `docs/grok-runtime-isolation.md`, `src/nightshift/runtime.py` |
| Extension preflight | `src/nightshift/extensions.py` |
| What each layer enforces | `docs/threat-model.md`, `docs/security-audit-v0.1.md` |
| PATH shim | `src/nightshift/guard.py` |
| Process model and recovery | `docs/architecture.md`, `src/nightshift/supervisor.py` |
| Safety probe | `src/nightshift/probe.py` |
| Morning report sections | `src/nightshift/report.py` |

Run the suite with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Use `discover -s tests`. `python3 -m unittest tests.<module>` imports a different top-level `tests` package when one is installed for that interpreter.

## Review bar

A change is ready when the suite passes, `nightshift doctor` passes, and a FakeProvider `nightshift run` against a dirty temporary repository leaves that repository's integrity snapshot unchanged. A Grok change also needs `nightshift safety probe` (the fake probe) to pass. A real Grok session is a separate, explicit `nightshift safety probe --provider grok` on a sacrificial repository.
