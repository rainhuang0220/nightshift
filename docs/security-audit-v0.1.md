# Security audit of Nightshift v0.1

This note records an external source review of v0.1 and what v0.2 changed after the findings were checked against the code. The v0.1 commits reviewed were `4e7f614` (the control plane) and `3b88ee8` (the missing-workspace recovery class). The working tree was clean at the start of the review. The suite at that point was 34 tests, all passing.

Each finding below was confirmed in the v0.1 sources before any v0.2 edit.

## Finding A — a worktree is not full isolation

Confirmed in `src/nightshift/workspace.py`. v0.1's only workspace was `git worktree add --detach`. That command leaves the source working tree and index alone and writes a registration under the source `.git/worktrees`. Source git metadata was still mutated. The v0.1 threat model said that registry update was the only source-side git mutation, which was accurate and also the defect: the default unattended backend shared the source git directory, including config and hooks.

v0.2 makes an independent local clone the default. The clone uses `--no-hardlinks` and does not use `--shared`. `origin` is removed. A commit in the clone does not update source refs and does not create `.git/worktrees` in the source. The worktree backend remains as `isolation = "worktree"` and is labeled weaker isolation that shares source git metadata. It is not the default.

Layer: **prevention** of shared git metadata. The clone is not, by itself, filesystem containment.

## Finding B — the source snapshot was too narrow

Confirmed. v0.1's runner compared source `HEAD` and `git status --porcelain` after the run. That misses a new ref, a `packed-refs` rewrite, a local config edit, a hook change, an index change that porcelain does not always surface the same way, and a worktree registration.

v0.2 stores a `SourceIntegrity` snapshot after workspace prepare. The categories are HEAD, porcelain, current branch, all refs and their object ids, `packed-refs`, local git config, the index, the hooks tree, and the worktree registry. The object database is not hashed. A difference is `SOURCE_INTEGRITY_VIOLATION`. Nightshift does not restore the source.

Layer: **detection**.

## Finding C — recovery could skip the integrity check

Confirmed, and treated as P0. v0.1's normal path compared HEAD and porcelain after verification. `run_verification_only` and recorded-state finalization could mark a run `SUCCEEDED` from exit codes alone. A crash, a successful verification on recover, and a source that changed in between could succeed without the check the normal path performed.

v0.2 has one function, `decide_final`. Normal execution, verification recovery, and recorded-state finalization all call it. `SUCCEEDED` requires a provider exit of exactly 0, verification that ran and exited 0, a workspace on disk, a passing integrity check, and a passing safety audit. A source repository with no stored snapshot fails closed. `recover` still never launches a provider.

The regression is: provider exit 0, run left `RUNNING` or `VERIFYING`, source config mutated, recover runs, verification exits 0. The result is not `SUCCEEDED`, and the reason contains `SOURCE_INTEGRITY_VIOLATION`.

Layer: **prevention** of a success that bypasses detection.

## Finding D — verification ran outside any sandbox

Confirmed. v0.1 ran the job's verification strings with `shell=True` in the workspace. Those processes were Nightshift children, not Grok tool calls, so Grok's `--sandbox`, `--allow`, `--deny`, and `--permission-mode` did not apply. The PATH shim did not apply to absolute executables.

v0.2's unattended form is an argv table. The process runs under a verification seatbelt that denies network, uses the sanitized environment and a private `HOME`, and cannot write the source repository. A timeout kills the process group. Legacy shell strings remain for compatibility, are labeled unsafe, and are blocked for Grok jobs unless `allow_legacy_shell_verification` is set.

Layer: **containment** for the verification process, and **prevention** of legacy shell as the Grok default.

## Finding E — the child inherited the operator's Grok extensions

Confirmed. v0.1 kept the operator's `HOME` and did not set `GROK_HOME`. On the machine where v0.1 was built, an operator `grok inspect` showed a large user-level surface: many skills, several plugins, MCP servers, hooks, and a global agents file. An unattended run would have discovered that surface.

v0.2 gives each run a private `HOME` and points `GROK_HOME` at `state/grok-profile`. The profile starts empty of skills, plugins, hooks, MCP definitions, memory, and sessions. Authentication is an explicit copy of `auth.json` only. `grok inspect --json` must pass before a real Grok launch. Operator hooks, plugins, MCP servers, and imported Claude/Cursor/Codex items fail the run as `BLOCKED_EXTENSION_SURFACE`.

Layer: **containment** of the extension surface, and **prevention** of a launch when the surface is not clean.

## Finding F — shell allow rules were treated as containment

Confirmed as an architecture error. v0.1 allowed broad interpreter patterns such as `Bash(python*)`, `Bash(make*)`, and `Bash(cargo test*)`. An allowed interpreter can run `/usr/bin/git`, `/bin/sh`, open sockets, and call filesystem APIs. The PATH shim covers PATH lookup only. The prompt covers nothing the model chooses to ignore.

v0.2 keeps the deny rules and the shim, and states what they are. Filesystem and process containment is the seatbelt. The clone plus the integrity gate cover the source repository. Human review is the import. The safety probe replays absolute Python, `/bin/sh`, `/usr/bin/git push`, and a Python subprocess that calls `/usr/bin/git push`, and classifies each result from the effect and the denial, not from the prompt.

Layer: the shim and the permission rules are **defense in depth**. The seatbelt is **containment**.

## Finding G — provider logs were raw

Confirmed. v0.1 streamed provider stdout and stderr to disk and scrubbed only some event messages. The report could include raw stderr fragments. Nothing in v0.1 made the run directory mode 0700 or the logs mode 0600.

v0.2 creates private runtime directories (0700) and writes logs, the database, and seatbelt profiles as 0600. Provider output is scrubbed as it is written. The report summarizes stdout and omits raw stderr. Scrubbing is best-effort. Repository content can contain secret formats the patterns do not recognize, so this is not a claim of complete redaction. Logs are kept. Auth material and the Grok profile are gitignored.

Layer: **containment** of the files, with best-effort redaction. Not a cryptographic guarantee.

## How the layers sit together

| Layer | v0.2 mechanism |
| --- | --- |
| Advisory | Policy preamble and any other prompt text |
| Defense in depth | Grok allow/deny rules; PATH shim |
| Containment | Seatbelt; private HOME and GROK_HOME; private log files |
| Prevention | Clone instead of a shared git dir; extension preflight; refuse `SUCCEEDED` when the gate fails; block legacy shell for Grok |
| Detection | Source integrity snapshot, retained as evidence, no automatic restore |
| Human approval | Morning report and hand import of the clone |

## Limits that remain on purpose

The Grok provider seatbelt allows network so the model API can be reached. A network `git push` is not an OS hard block. A local bare-repo push is a filesystem write and is a hard block. The safety gate for a clean real probe is `PASS_WITH_LIMITATIONS` while that network allowance stands.

Grok's own `--sandbox` flag is not passed. Inside the seatbelt it fails to initialize and Grok refuses to start. The seatbelt is the containment Nightshift claims.
