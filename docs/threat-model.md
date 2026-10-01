# Threat model

Nightshift v0.1 assumes a trusted operator and an untrusted model process. The operator writes the job, including verification commands. The model may try to push, publish, read credentials, or edit the original checkout. Another user on the machine is outside this model.

Labels mean:

- **Hard.** Nightshift or a tool it wraps refuses the action for the path it actually controls. A bypass is listed beside it.
- **Advisory.** Text, a convention, or a check that does not stop the action. An advisory control is not a security boundary.
- **Unverified.** The installed documentation describes the control, and v0.1 passes the flag, but this bootstrap did not prove the OS enforcement engaged.

## Controls

### 1. Policy preamble

**Advisory.** `prompt.final.md` tells the model what to refuse. The text itself says it is not a security boundary. A model can ignore it.

### 2. Grok permission mode, allow, and deny

**Hard inside Grok's process, when Grok is the provider and it honors its own engine.** The installed permission guide says deny wins over allow, including over an approval bypass. Nightshift's default mode is `dontAsk`: Grok runs pre-approved tools plus its built-in read-only set, and does not pop a prompt. The argv always carries the deny list in `policy.DENY_RULES` (push, sudo, `rm -rf`, pull-request and issue mutations, releases, Kaggle, package publish, global git config, clean, hard reset, and reads/edits of credential paths).

**Not an OS boundary.** A binary that is not Grok, a Grok build that fails open, or a command that never goes through Grok's Bash/Read/Edit tools is unaffected. Nightshift refuses to put `--always-approve` or `bypassPermissions` on the command line, and refuses those modes in config. `job.allow_bash` can add Bash patterns; it cannot delete a deny, and validation rejects the forbidden snippets in `docs/job-format.md`.

### 3. Grok sandbox profile

**Unverified.** `--sandbox workspace` is passed when `write_scope` is `workspace`. `--sandbox read-only` is passed when `write_scope` is `none`. Profile names come from the installed Grok user guide (`off`, `workspace`, `devbox`, `read-only`, `strict`). `grok --help` does not list them, and v0.1 did not start Grok to watch Seatbelt engage. The same guide says a profile that fails to apply produces a warning and the process continues without that enforcement. Treat the flag as requested isolation, not as a proven jail.

On macOS the guide describes child-network blocking as a Linux seccomp feature and a no-op. Network policy in v0.1 is therefore `--disable-web-search` plus withholding `WebSearch` and `WebFetch` when `network` is false. That is hard only for Grok's own web tools. `curl`, `wget`, and `npx` are not wrapped.

### 4. Working directory and worktree

**Hard for Nightshift's own git operations.** The source repo is inspected with rev-parse and porcelain. The only source-side git mutation Nightshift performs is the worktree registry entry created by `git worktree add --detach`. Nightshift does not clean, reset, stash, or checkout the source. The provider cwd is the new worktree. Two runs do not share that directory: the path is per run id, and a workspace lock is held for the run.

**Not a jail by itself.** A process can still open an absolute path. The post-run audit detects a changed source HEAD or porcelain and fails the run. Detection does not revert the source. If the model damages the original tree, a human restores it.

### 5. PATH shims

**Hard for PATH lookup** of `git`, `gh`, `sudo`, `kaggle`, `npm`, and `twine`. The shim directory is prepended to the child `PATH`. Denied with exit 126: `sudo`; all `kaggle`; `npm publish`; `twine upload`; `gh` commands that create or mutate pull requests, issues, or releases; `git push`, `clean`, `worktree`, `daemon`, fetch/pull/clone, remote changes, submodule, `reset --hard`, and config writes. Mutating git (`add`, `commit`, `checkout`, `switch`, `branch`, `merge`, `rebase`, `stash`, and reset without `--hard`) is allowed only when the effective cwd is inside the worktree and is not the source repo. Allowed git invocations also force `core.hooksPath` to an empty directory in the run dir and `commit.gpgsign=false`.

**Bypass.** An absolute binary such as `/usr/bin/git` never hits the shim. `pnpm`, `yarn`, `cargo`, `npx`, `curl`, and `wget` are not wrapped. Python or a compiled program can call any of those directly.

### 6. Git config and hooks

**Hard for git's default config files in the child environment.** `GIT_CONFIG_GLOBAL` points at an empty file in the run directory. `GIT_CONFIG_NOSYSTEM=1`. Combined with the shim's refusal of `git config` writes, a PATH-resolved git cannot change the operator's `~/.gitconfig`.

**Residual coupling.** A linked worktree shares the source repository's config and hook configuration. `git config --local` in the worktree writes that shared config file. The shim denies config writes, and the forced `core.hooksPath` keeps source hooks from running for shimmed git. An absolute git binary, or an editor writing `.git/config`, still can. Nightshift does not rewrite the source config to "fix" this.

### 7. Environment minimization

**Hard for the child environment Nightshift constructs.** Names matching `KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `CREDENTIAL`, `PASSWD`, or `SESSION` are dropped. `HOME` is kept because git and Grok need it, and it is not copied into host info or the report. Log lines that look like `token=...` or `password=...` are redacted. Redaction is best-effort and misses other shapes. Files on disk (`~/.ssh`, `~/.grok`, `.env`) are not deleted. Grok deny rules name several of those paths; that enforcement is control 2, not this one.

### 8. Post-run audit

**Detection, not prevention.** After the provider and verification, Nightshift compares source HEAD and porcelain to the pre-run snapshot. A difference fails the run and is written into the report. The source is not restored. Suspicious log lines (`git push`, `gh pr create`, `sudo`, `kaggle competitions submit`) become uncertainty notes. They are not claimed as blocks.

### 9. Locks and concurrency

**Hard for the scheduler.** A live owner of a concurrency group or repository lock is not stolen. Stale locks (missing holder, terminal holder, or dead pid) are cleared. This stops two Nightshift runs from sharing one mutable workspace. It does not confine a hostile process that ignores the database.

### 10. Verification commands

**Hard that the command list is the operator's.** The snapshot is taken before the provider starts. The model cannot replace it. The commands themselves run with the shell as the operator wrote them, inside the worktree, with the child environment. A verification line of `git push` is operator error. Keep verification to checks.

### 11. `NIGHTSHIFT_FORBID_GROK`

**Operator circuit breaker.** When set to `1`, the Grok adapter returns without spawning. Tests set it. It is not a sandbox.

### 12. Process termination

The supervisor starts the child in its own session and signals that process group on timeout, cancel, or interrupt. It does not signal its own group. `recover` will not kill a PID whose command line lacks the session token, so a recycled PID is left alone.

## What v0.1 does not claim

- Seatbelt or any other sandbox profile actually confined the filesystem on this machine.
- Network access is blocked at the OS level on macOS.
- Absolute paths, interpreters, and unwrapped package managers stay inside the workspace.
- The morning report is a complete audit of every side effect. It is a summary plus a source-tree comparison.
- Unattended execution is safe for every job an operator can write. It is safe relative to the controls above, for jobs whose verification commands are themselves safe.

## Operator practice

Run the daemon yourself under `caffeinate`. Review the worktree and the report before keeping a commit. Do not point a job at a repository you cannot afford to re-read. Nightshift does not push, open pull requests, or install a system service.
