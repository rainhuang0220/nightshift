# Job format

A job is a directory containing `job.toml` and `prompt.md`. Both are meant to be committed and reviewed. `nightshift job validate <dir>` or `nightshift job validate <dir>/job.toml` checks the manifest. Validation does not require the target repository to exist.

Schema version is 1. Unknown keys are rejected. `prompt.md` must exist beside the manifest and must not be empty.

## Fields

| Field | Required | Meaning |
| --- | --- | --- |
| `schema_version` | yes | Integer `1` |
| `id` | yes | `[A-Za-z0-9][A-Za-z0-9._-]{0,80}` |
| `description` | yes | One non-empty string shown in the report |
| `type` | yes | Free label such as `audit` or `fix`. Not dispatched |
| `repository` | yes | Target checkout. Relative paths resolve against the job directory |
| `base_ref` | yes | Git ref resolved to a commit before the workspace is created. `HEAD` is allowed |
| `provider` | yes | `grok` or `fake`. `cursor` is rejected |
| `model` | no | Passed to Grok as `--model` when set |
| `max_runtime_seconds` | yes | Integer from 1 to 86400. The supervisor stops the child at this limit |
| `max_attempts` | yes | Integer from 1 to 10. Counts explicit retries |
| `concurrency_group` | yes | Non-empty name. Two active runs in the same group do not overlap |
| `network` | yes | Boolean. `false` passes `--disable-web-search` and withholds web tools |
| `write_scope` | yes | `none` or `workspace` |
| `isolation` | no | `clone` (default) or `worktree` |
| `allow_legacy_shell_verification` | no | Boolean, default false. Opts a Grok job into shell-string verification |
| `expected_artifacts` | yes | List of paths relative to the workspace. May be empty. Reported, not required for success |
| `verification` | yes | Commands run after the provider, in the workspace. May be empty |
| `success_criteria` | yes | At least one sentence. Recorded in the report. Not executed |
| `allow_bash` | no | Extra Grok `Bash(...)` patterns. Cannot authorize the forbidden snippets below |

`base_ref` is resolved once and the workspace checks out that exact revision.

`isolation = "clone"` builds an independent local clone. `isolation = "worktree"` adds a linked worktree. The worktree backend shares the source git directory, is labeled weaker isolation, and is not the default for unattended jobs.

`network = false` disables Grok web search. The provider seatbelt still allows network so the model API can be reached. See `docs/threat-model.md`.

## Verification

`verification` is either a list of strings or a list of argv tables. Mixing the two is rejected.

The unattended form is structured:

```toml
[[verification]]
argv = ["python3", "-m", "unittest", "discover", "-s", "tests"]
timeout_seconds = 300
```

`argv` is a non-empty list of non-empty strings. `timeout_seconds` is optional and, when set, is an integer from 1 to 86400. Unknown keys on a step are rejected. Nightshift runs the argv directly in the isolated workspace under the verification seatbelt. There is no shell expansion.

A list of strings is legacy shell verification:

```toml
verification = ["python3 -m unittest discover -s tests"]
```

Each string is run as `/bin/sh -c` under the same seatbelt. That form is unsafe relative to the argv form because the shell parses it. Grok jobs that use it are blocked unless `allow_legacy_shell_verification = true`. FakeProvider jobs still accept it so existing dry runs keep working. Do not use the legacy form for unattended Grok jobs.

An empty list runs no commands and counts as verification exit 0.

Verification commands are trusted operator input. Nightshift snapshots them into SQLite before the provider starts. The model cannot replace them.

## Write scope

`none` withholds Edit/Write in Grok's permission rules. `workspace` allows edits and local git commits inside the isolated workspace through those rules. Neither scope permits push, publishing, or edits to the original checkout.

The write scope is also recorded as the label `read-only` or `workspace`. Nightshift does not pass `--sandbox` with that label. Nested sandbox setup fails inside the seatbelt, and Grok then refuses to start. Filesystem containment is the seatbelt. See `docs/threat-model.md`.

## Forbidden `allow_bash` snippets

Validation rejects an entry that contains any of:

```text
git push
sudo
rm -rf
gh pr create
gh pr merge
--global
kaggle
```

Grok deny rules still name those actions. The PATH shim still refuses the PATH lookup. The seatbelt still confines filesystem writes. None of those three is a substitute for the others.

## Example

`jobs/examples/repo_audit/` is a harmless read-only audit skeleton. Its provider is `fake` and its repository is the placeholder `REPLACE_WITH_REPOSITORY_PATH`. `job validate` accepts it. Queueing it without editing `repository` blocks, because that placeholder is not a git checkout.

```toml
schema_version = 1
id = "repo-audit-example"
description = "Harmless read-only audit skeleton. Replace repository before queueing."
type = "audit"
repository = "REPLACE_WITH_REPOSITORY_PATH"
base_ref = "HEAD"
provider = "fake"
max_runtime_seconds = 600
max_attempts = 1
concurrency_group = "repo-audit-example"
network = false
write_scope = "none"
isolation = "clone"
expected_artifacts = []
verification = []
success_criteria = [
  "The source checkout is unchanged.",
  "The morning report names the original revision.",
]
```

## What the provider sees

Nightshift writes `runs/<run-id>/prompt.final.md` by prefixing the job prompt with a policy preamble. The preamble tells the model to stay in the isolated workspace and that the original checkout is off limits. It is an instruction, not a security boundary.

The child process sees a private `HOME` and a per-run `GROK_HOME` (`runs/<id>/grok-home`, absolute only inside the child), `GROK_MEMORY=0`, and `GROK_WORKFLOWS=0`. It does not see the operator's normal Grok skills, plugins, hooks, or MCP credentials. The original source checkout is not a read root.
