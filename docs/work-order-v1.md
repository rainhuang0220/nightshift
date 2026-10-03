# Engineering Work Order v1

The interchange is UTF-8 JSON, `schema: engineering.work-order`, `version: 1`, at most 1 MiB. Unknown fields, duplicate keys, nonfinite numbers and incompatible versions are rejected. Each project implements its own stdlib validator; neither imports the other package. No database, scoring model or executor details cross this boundary.

| Field | Meaning |
| --- | --- |
| task_id | Stable identity for this decision and scope; changed content requires a new ID |
| origin | Planner name and original decision reference |
| repository | Identity, optional absolute local path/URL and pinned full Git revision |
| title / objective / rationale | Task, intended behavior and reason for choosing it |
| evidence | Distinct observation or inference records with IDs, summary, source, expiration and observation lineage |
| constraints | Scope, expected behavior, acceptance conditions and additional prohibitions |
| validation | Nonempty argv checks, expected result and timeout of 1–86400 seconds |
| actions | Local allowed actions and mandatory forbidden external actions |
| references | Optional human review links |
| created_at | Timezone-aware creation time |
| provenance | Opportunity ID, decision time and exact evidence IDs |

Observation timestamps must be real and no later than the decision. An inference has no observation timestamp and points to exported observations. Evidence must expire after creation; intake and launch also reject evidence that is stale now. Future-created work orders are rejected at intake. Confidence remains a planner inference and is not exported as a fact.

Allowed actions are a subset of `read`, `edit`, `commit` and include read. `push`, `publish`, `deploy`, `credentials` and `external-write` are mandatory denials. Validation runs argv directly, without a shell. V1 intake requires a local Git checkout root and pinned revision; it never downloads a URL. Editing and committing occur in an independent clone. The action list and prompts describe scope; OS containment and manual review remain the enforcement and approval boundaries.

Canonical serialization sorts object keys, preserves array order, emits UTF-8 with two-space indentation and one final newline. Re-exporting the same retained decision is byte-identical. A new local observation is a new decision and may receive a new ID. Nightshift retains the original manifest in its job snapshot, returns the same run on repeated identical import, and rejects the same task ID with different content or execution settings. A repeated `import --run` never restarts a terminal attempt.

`result.json` is Nightshift's separate version-1 outcome artifact. It records starting revision, state, workspace, commits/files, integrity and containment findings, per-check timing/exit results, and the original work order. No automatic feedback, merge or push follows it. Recovery reconciles execution records; retry starts a new model session explicitly.
