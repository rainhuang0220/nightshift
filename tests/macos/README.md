# macOS whole-task lifetime release gate

These scripts use actual macOS processes and Seatbelt. They do not launch Grok, use network access, or mutate real source repositories. Ordinary unittest discovery remains the existing 131-case suite; this separate gate deliberately asserts the stronger, currently unmet release requirement. It is not skipped or marked expectedFailure.

From the repository root:

```sh
PYTHONPATH=src NIGHTSHIFT_FORBID_GROK=1 python3 tests/macos/task_lifetime_gate.py --evidence /absolute/path/lifetime.json
PYTHONPATH=src NIGHTSHIFT_FORBID_GROK=1 python3 tests/macos/native_primitive_probes.py --evidence /absolute/path/primitives.json
```

The lifetime gate currently exits **1**. CI runs it as a separate job that must pass for a green workflow on pull requests/main and manual dispatch; it has no continue-on-error or expected-failure wrapper. The primitive script records observations; exit 0 means the probe completed, not that release is safe. Keep both distinctions visible in release decisions. The native primitive script creates a UUID-labelled temporary per-user launchd job and a private disk image, then removes only its owned resources. It installs no persistent service. A failed image cleanup preserves the owned directory for diagnosis.

`--case test_name` selects a diagnostic subset; its success never sets `release_gate_passed` without full coverage. Finite payload expiry before measurement is rejected as inconclusive, so natural expiry cannot masquerade as cleanup. `--probe launchd|kqueue|volume` selects a primitive. Finite payload expiry and the wait in test teardown are cleanup hygiene after measurement, never the proposed supervision mechanism. Only unreaped direct children are signalled by the harness; it does not scan for escaped descendants. Manual recovery uses the existing production path and is not an atomic process-tree ownership proof.

See [the release decision](../../docs/adr/0002-whole-task-lifetime-release-gate.md) and [primary-source evaluation](../../docs/macos-task-lifetime-research.md). Run the explicit gate before any future unattended-release claim; a green ordinary unit suite is insufficient.
