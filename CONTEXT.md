# Unattended engineering execution

Nightshift owns the local lifecycle of a bounded engineering task and records evidence for review.

## Language

**Work order**: A generic external task declaration; intake validates it before creating a Nightshift job. _Avoid_: Foreshadow object.

**Job snapshot**: Nightshift's retained execution plan, including source revision, provider policy and declared validation. _Avoid_: live mutable manifest.

**Run**: A durable identity and lifecycle record for one queued job. _Avoid_: model session.

**Attempt**: One explicit execution with its own logs, private runtime and workspace. Retry creates the next attempt. _Avoid_: session continuation.

**Execution recovery**: Reconciliation of persisted phases with matching processes; it never launches a provider. _Avoid_: model resume.

**Owned workspace**: A clone or worktree whose private receipt matches source, revision, isolation and destination; only this proof permits cleanup.

**Result manifest**: Structured outcome evidence and per-check results retained beside the human report. _Avoid_: success inferred from terminal text.
