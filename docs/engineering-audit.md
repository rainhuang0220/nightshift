# Engineering audit — 2026-10-03

Baseline: main and origin/main 36b2cf1, package/tag 0.2.1; source checkout clean. Work happens on engineering/recovery-handoff. Fifteen historical commits already harden clone isolation, runtime credentials, source-integrity checks, cancellation and recovery. CI is macOS unittest on Python 3.11–3.14; no release workflow exists.

Keep standard library, Job/RunRecord, legal transition table, independent clone default, per-attempt HOME, Seatbelt, extension preflight, and the single decide_final success gate. Recovery is bookkeeping and verification recovery, never model continuation. Retry is explicit and bounded.

Confirmed source-level gaps to reproduce: cleanup only checks overlap and can remove an unrelated clone; clones attribute commits to Nightshift rather than the configured human; logs and contained verification output are unbounded; leader exit can leave process-group children; same-run lock refresh can admit concurrent controllers; reports list declared checks as executed even when preparation fails; intake is private TOML only. Unknown manifest types may throw TypeError before structured validation. Config accepts lossy coercion. These affect unattended reliability and audit truth, unlike adding more providers or a daemon service.

Chosen slice: ownership-proven cleanup and operator identity, per-run controller lease, bounded shared process supervision/redaction, accurate per-check result records, strict generic JSON work-order intake with idempotent task identity and pinned revision. No shared Python modules, database, remote service, outcome-training loop, or persistent system service.

Provider network remains an accepted containment limitation; verification runs offline in a mutable isolated workspace and human review remains required. Unit tests must use temporary sources and FakeProvider. A separate explicitly authorized real bounded engineering canary will exercise the export/intake boundary.

Implementation and real canary findings: [engineering dogfood](engineering-dogfood.md). Domain vocabulary: [CONTEXT](../CONTEXT.md). The release candidates retain the limitations stated there; no production release is claimed.

Validation: 131 unittest cases pass on macOS/Python 3.14. Compileall, 0.3.0 wheel/sdist, doctor, fake safety probe and independent install boundary checks pass. Baseline was 97 tests. The existing CI now also builds and smokes installed packages on its supported Python matrix.
