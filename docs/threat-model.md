# Threat model

Nightshift v0.2 assumes a trusted operator and an untrusted model process. The operator writes the job. The model may try to push, publish, read credentials, edit the original checkout, or pick up the operator's Grok extensions. Another user on the machine is outside this model.

A hostile or mistaken unattended session may work inside its clone. It must not become a mutation channel for the operator's repository, credentials, extensions, or remote services.

Layers:

- **Prevention.** Nightshift refuses to start, or refuses to call the action a success.
- **Containment.** The operating system or a private directory stops the child from reaching the protected resource.
- **Detection.** Nightshift records that something changed and withholds `SUCCEEDED`. It does not undo the change.
- **Advisory.** Text or a convention. The model can ignore it.
- **Defense in depth.** A real check on one path. A bypass of that path is expected and is stopped by a later layer, or is an accepted limit.
- **Human approval.** The operator reads the report and the clone and imports by hand.

## Controls

### 1. Policy preamble

**Advisory.** `prompt.final.md` tells the model what to refuse. The text says it is an instruction and that permission rules are not filesystem containment. A model can ignore it. Prompt compliance is not a hard block.

### 2. Grok permission rules

**Defense in depth, inside Grok's tool-call engine.** The default mode is `dontAsk`. The argv carries `policy.DENY_RULES` (push, sudo, `rm -rf`, pull-request and issue mutations, releases, Kaggle, package publish, global git config, clean, hard reset, and reads or edits of credential paths) plus the job's allow list. Deny wins over allow inside Grok when Grok honors its engine. Nightshift refuses `--always-approve` and `bypassPermissions`. `job.allow_bash` can add Bash patterns. It cannot delete a deny. Validation rejects the forbidden snippets in `docs/job-format.md`.

An allowed interpreter can still invoke an absolute binary, open a socket, or call filesystem APIs. Command-name allowlists are not filesystem containment.

### 3. Grok `--sandbox` flag

**Not applied.** The installed CLI accepts `--sandbox workspace` and `--sandbox read-only`. On this machine the flag applies when Grok is the outer process: a one-turn prompt returned the model's reply and an empty stderr. The same flag inside Nightshift's seatbelt fails before the model starts:

```text
sandbox initialization failed: Operation not permitted
Refusing to start with its protections missing.
```

A wide outer profile still gets that EPERM, so this is nested `sandbox_init`, not a missing file allow. Grok fails closed, which would turn every contained run into an immediate provider exit 1. Nightshift omits the flag. The seatbelt in control 5 is the filesystem and process containment. The write-scope name stays on the invocation as a label for the permission rules.

### 4. Independent clone

**Prevention of shared git metadata.** The default workspace is `git clone --no-hardlinks --no-checkout` of the source, then a detached checkout of the recorded revision. `origin` is removed. The clone has its own refs, config, index, and hooks path. A commit in the clone does not update source refs and does not register a source worktree. Untracked dirty source files are not copied. Nightshift does not clean, reset, stash, or checkout the source.

**Accepted weaker option.** `isolation = "worktree"` shares the source git directory. It is labeled weaker isolation and is not the unattended default. The only source change that prepare is allowed to make on that backend is the worktree registry.

**Not a jail by itself.** A process in the clone can still open an absolute path. Containment for that path is the seatbelt. Detection of a source change is the integrity gate.

### 5. Seatbelt

**Containment.** Provider and verification processes are started under `/usr/bin/sandbox-exec` with a deny-default profile. There is no global `(allow file-read*)`. The profile allows required system and toolchain reads, denies the operator home and the original source checkout, then re-allows only the isolated workspace, the run directory, the private runtime `HOME`, the per-run `GROK_HOME`, the resolved Grok or interpreter path, and roots listed in Nightshift config as `[containment] read_roots`. A job prompt cannot add roots. The operator home is refused as a read root. Writable roots are the workspace and the run directory. The persistent auth store is not writable. `/dev/null` is readable and writable. The last matching rule wins, so the re-allow is written after the home and source denies. A path is emitted both as named and symlink-resolved, so `/var` and `/private/var` match. Traversal anchors such as `/var` and `/opt` are literals. A `subpath` rule on those symlink roots would expose the whole target tree. Ancestors of a re-allowed root, including `/private/var/folders`, are `file-read-metadata` literals. That lets a process stat the path. It does not list the directory or read files there.

If `sandbox-exec` is missing, the run is blocked. Nightshift does not fall through to an unsandboxed child and does not fall through to Grok's `--sandbox` flag.

Verification denies `network*`. The Grok provider profile allows `network*` so the model API can be reached. Child processes inherit that network. An https or ssh `git push` is not an OS hard block on the provider profile. Blocking `curl` or `python` by name would not close it. A push to a local bare repository is a filesystem write outside the writable roots and is a hard block. The honest safety-gate result while this limit stands is `PASS_WITH_LIMITATIONS`, not `PASS`.

The provider profile's network allowance is independent of `job.network`. `job.network = false` disables Grok web search. It does not close the provider's sockets.

### 6. PATH shim

**Defense in depth for PATH lookup** of `git`, `gh`, `sudo`, `kaggle`, `npm`, and `twine`. The shim directory is prepended to the child `PATH`. Denied with exit 126: `sudo`; all `kaggle`; `npm publish`; `twine upload`; `gh` commands that create or mutate pull requests, issues, or releases; `git push`, `clean`, `worktree`, `daemon`, fetch/pull/clone, remote changes, submodule, `reset --hard`, and config writes. Mutating git is allowed only when the effective cwd is inside the workspace and is not the source repo. Allowed git invocations also force `core.hooksPath` to an empty directory in the run dir and `commit.gpgsign=false`.

An absolute binary such as `/usr/bin/git` never hits the shim. Python, `/bin/sh`, and other interpreters can call those binaries directly. Those bypasses are the seatbelt's job. The shim is not the principal security boundary.

### 7. Source integrity gate

**Detection.** Before `SUCCEEDED`, Nightshift compares a stored snapshot with the source. The snapshot covers HEAD, porcelain status, the current branch, every ref and its object id, `packed-refs`, the local git config, the index, the hooks tree, and the worktree registry. It does not hash the object database. Hooks and worktree entries record type, relative path, symlink target, and a full content digest. A protected tree file larger than 1_000_000 bytes, or an unreadable protected path, fails the snapshot closed. The index, local config, and `packed-refs` are always content-hashed. Same-size replacement of a protected hook or worktree file changes the digest.

A difference is `SOURCE_INTEGRITY_VIOLATION`. The run is not `SUCCEEDED`. Nightshift does not restore the source. The baseline is the snapshot taken after workspace prepare, so a clone's lack of source mutation is the expected start, and a worktree's registry update is the only allowed prepare difference.

The same gate runs after a normal verification and after recovery. A recovered run cannot succeed on a weaker check than a normal run.

### 8. Extension preflight

**Prevention.** Before a real Grok process starts, Nightshift runs `grok inspect --json` with the sanitized environment and parses the JSON. The default unattended policy rejects user hooks, project hooks, plugin hooks, external MCP servers, user plugins, project plugins, Claude/Cursor/Codex discovered items, unexpected permission grants, non-builtin agents, LSP servers, marketplaces, and operator-global instruction files.

Built-in agents may remain. A bundled skill may remain only when its path is under the Nightshift `GROK_HOME`. Project instruction files are counted as untrusted instructions and are not, by themselves, a violation. Repository content may influence reasoning. It must not silently grant tools or executable hooks.

The installed CLI leaves Claude, Cursor, and Codex compatibility flags enabled even on an empty profile. Those flags are product defaults. They are not treated as imported servers. A discovered server, hook, or plugin from those vendors is a violation.

A failed audit is `BLOCKED_EXTENSION_SURFACE`. The model is not launched.

### 9. Project extension files

**Prevention inside the clone.** `.grok`, `.mcp.json`, `.cursor/mcp.json`, `.cursor/hooks.json`, `.cursor/hooks`, and `.claude` are moved into the run's neutralize record for the provider process, or unlinked when they are symlinks. A symlink whose target is outside the workspace is unlinked without moving that target. After the provider exits, Nightshift puts the entries back, so a read-only job does not keep those deletions. The source repository is not modified. `AGENTS.md` and `CLAUDE.md` stay in the clone as untrusted text.

### 10. Isolated HOME and GROK_HOME

**Containment of extensions and credentials.** The child `HOME` is `runs/<run-id>/runtime-home`, mode 0700. `GROK_HOME` is `runs/<run-id>/grok-home`, created for that run and not reused. The persistent auth file is `state/credentials/grok/auth.json` (parent mode 0700, file mode 0600). A provider launch copies only that file into the per-run home. The copy is removed before verification. Verification recovery does not copy it. Exit, cancel, interrupt, and recovery delete the copy and leave the store. `TMPDIR` is a private directory under the run. The operator's `~/.agents`, `~/.claude`, `~/.cursor`, skills, plugins, hooks, MCP definitions, memory, and sessions are not copied. Details are in `docs/grok-runtime-isolation.md`.

### 11. Environment minimization

**Prevention of inherited tokens.** The child environment drops names matching `KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `CREDENTIAL`, `PASSWD`, or `SESSION`, and drops `AWS_`, `AZURE_`, `GOOGLE_`, `KAGGLE_`, `NPM_`, `PYPI_`, `GH_`, and `GITHUB_` prefixes. `SSH_AUTH_SOCK` is removed. `GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_NOSYSTEM=1`, and `GIT_CONFIG_GLOBAL` points at an empty file in the run directory.

`minimal_env` still copies `HOME` from the parent as a placeholder. The runner then replaces `HOME` with the runtime home. The placeholder is not the boundary. Grok authentication lives in the persistent auth store, not in a shell variable.

### 12. Logs

**Containment of secret-bearing files, with best-effort redaction.** Runtime directories are mode 0700. Logs, the database, and seatbelt profiles are mode 0600. Provider output is scrubbed as it is written. The morning report summarizes stdout and omits raw stderr. Scrubbing recognizes common assignment shapes. It does not recognize every secret format that repository content might contain. Redaction is not a cryptographic guarantee. Logs are not deleted automatically. Secret material is gitignored and must not be committed.

### 13. Verification

**Prevention that the command list is the operator's, and containment of the process that runs it.** The snapshot is taken before the provider starts. The model cannot replace it.

The unattended form is an argv table. Nightshift runs that argv inside the clone, under the verification seatbelt, with the sanitized environment, a bounded timeout, and a process-group kill on timeout. The verification profile cannot write the source repository and denies network.

A shell string is legacy. It is labeled unsafe, wrapped as `/bin/sh -c` under the same seatbelt, and blocked for Grok jobs unless `allow_legacy_shell_verification` is set. It is not the default unattended mode.

### 14. Locks and concurrency

**Prevention for the scheduler.** A live owner of a concurrency group or repository lock is not stolen. Stale locks (missing holder, terminal holder, or dead pid) are cleared. This stops two Nightshift runs from sharing one mutable workspace. It does not confine a process that ignores the database.

### 15. Process termination

The supervisor starts the child in its own session and signals that process group on timeout, cancel, or interrupt. It does not signal its own group. `recover` will not kill a PID whose command line lacks the session token, so a recycled PID is left alone.

### 16. Human approval

**Final approval.** Every report requires human review. Nightshift does not merge the clone back and does not push. The operator reads the report and imports commits by hand.

### 17. `NIGHTSHIFT_FORBID_GROK`

**Operator circuit breaker.** When set to `1`, a Grok launch is refused before `inspect` and before the model starts. Tests set it. It is not a sandbox.

## What the current tree claims

- The default workspace is an independent clone. Source refs, the source index, the source worktree registry, and untracked dirty files stay as they were.
- A changed protected source category withholds `SUCCEEDED` on every finalization path, including recovery. Protected hook and worktree metadata is content evidence, not a size.
- The provider can read the clone, its run directory, and required system files. It cannot read the original checkout or an unrelated file in the operator home. That boundary is the seatbelt, not the prompt.
- The provider and verification children do not see the operator's normal Grok extension surface. One run's `GROK_HOME` is not the next run's profile.
- Absolute interpreters and absolute git are confined by the seatbelt for filesystem writes outside the profile's writable roots, including a push to a local bare repository.
- Verification of a Grok job is an argv, unless the operator explicitly opts into legacy shell.

## What the current tree does not claim

- The provider seatbelt blocks network git push. It allows network so the model API works, and child processes inherit that network. That is why a clean real probe is `PASS_WITH_LIMITATIONS`.
- Grok's `--sandbox` flag is not the filesystem jail. Nightshift omits it because nested sandbox setup fails inside the seatbelt. The seatbelt is the jail.
- Command allowlists, the PATH shim, or the prompt are filesystem or network containment.
- Redaction finds every secret.
- The morning report is a complete audit of every side effect. It is a summary plus the integrity result.
- Unattended execution is safe for every job an operator can write. Verification argv is trusted operator input.

## Operator practice

Bootstrap the Nightshift Grok profile before the first real job. Run the daemon yourself under `caffeinate`. Review the clone and the report before importing a commit. Do not point a job at a repository you cannot afford to re-read. Nightshift does not push, open pull requests, or install a system service.
