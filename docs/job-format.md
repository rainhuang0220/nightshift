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
| `base_ref` | yes | Git ref resolved to a commit before the worktree is created. `HEAD` is allowed |
| `provider` | yes | `grok` or `fake`. `cursor` is rejected in v0.1 |
| `model` | no | Passed to Grok as `--model` when set |
| `max_runtime_seconds` | yes | Integer from 1 to 86400. The supervisor stops the child at this limit |
| `max_attempts` | yes | Integer from 1 to 10. Counts explicit retries |
| `concurrency_group` | yes | Non-empty name. Two active runs in the same group do not overlap |
| `network` | yes | Boolean. `true` allows Grok web search tools and leaves web search enabled |
| `write_scope` | yes | `none` or `workspace` |
| `expected_artifacts` | yes | List of paths relative to the workspace. May be empty. Reported, not required for success |
| `verification` | yes | List of shell commands run after the provider, in the worktree. May be empty |
| `success_criteria` | yes | At least one sentence. Recorded in the report. Not executed |
| `allow_bash` | no | Extra Grok `Bash(...)` patterns. Cannot authorize the forbidden snippets below |

`verification` commands are trusted operator input. Nightshift snapshots them into SQLite before the provider starts and runs them with the shell. Write them as carefully as you would write a script you intend to run unattended.

## Write scope

`none` asks for a read-only sandbox profile and does not allow Edit/Write. `workspace` asks for the `workspace` sandbox profile and allows edits and local git commits inside the isolated worktree. Neither scope permits push, publishing, or edits to the original checkout. See `docs/threat-model.md` for which of those limits are enforced.

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

Deny rules and the PATH shim still block those actions when a job tries to reach them another way.

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
expected_artifacts = []
verification = []
success_criteria = [
  "The source checkout is unchanged.",
  "The morning report names the original revision.",
]
```

## What the provider sees

Nightshift writes `runs/<run-id>/prompt.final.md` by prefixing the job prompt with a policy preamble. The preamble tells the model to stay in the worktree. It is an instruction, not a security boundary.
