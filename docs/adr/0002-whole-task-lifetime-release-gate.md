# Withhold unattended release until a whole-task lifetime boundary is proved

On 2026-10-04, the native execution path at `a8fe10f` fails the whole-task lifetime gate on macOS 26.5. Keep 0.3.0 unreleased and retain the failure tests. Do not extend process-group cleanup into a claimed descendant boundary: `setsid` and `setpgid` change the cleanup set while preserving filesystem access. Controller death separately removes the only deadline owner.

The [primitive evaluation](../macos-task-lifetime-research.md) answers ten deployment and correctness questions for each of eight candidates, with Apple sources and source-age caveats. This is Case B for the current product constraints and evidence, not a claim that a stronger native design is impossible.

## Required invariant

Terminal state must mean no attempt process retains workspace write capability. Controller death must leave independent finite enforcement or automatic system-owned reconciliation. A PID, a PGID, a start-time comparison followed by a signal, a lease, and a retained journal do not together prove these properties. The success gate currently proves recorded provider/check outcomes and integrity; it cannot prove that the task has stopped writing.

```text
controller (lease + durable state + timeout loop)
  └─ provider leader (new session / recorded PGID)
       ├─ child → grandchild in that PGID
       └─ child → setsid / setpgid → escaped writer

leader exit / timeout / cancel → kill recorded group → escaped writer remains
controller SIGKILL → lease released + timeout loop gone → no automatic recovery
```

Cancellation currently records terminal CANCELLED before signalling the phase. This prevents a competing success transition, but also means terminal state can precede even ordinary group cleanup. A future boundary must reconcile that ordering without losing cancellation intent. No state transition is changed by this research-only slice.

## Evidence and rejected shortcuts

The [retained result summary](../task-lifetime-strike-evidence.json) records 11 full gate cases: 4 pass, 7 fail the desired invariant, and 0 have harness errors. The ordinary 131-case suite and independent package/Work Order smoke pass. These results describe different properties; the release gate remains false. Adversarial review also reproduced and eliminated diagnostic false greens from partial runs and natural payload expiry, and verified cleanup after real bootstrap/attach followed by injected command timeout.

The explicit [macOS gate](../../tests/macos/task_lifetime_gate.py) asserts the desired invariant and exits nonzero on current native behavior. It has no expected failures or skips. The existing CI also runs it as a separate job without continue-on-error and retains its JSON even on failure; ordinary unit-suite green cannot hide this gate. It runs separately from the existing unit suite because it is an unsatisfied release gate, not a claim that six or seven defects are acceptable. No existing test is removed, skipped or weakened. The harness uses finite local payloads, actual Seatbelt and production process/attempt execution; only provider selection is substituted for the full-attempt software probe. It does not call a model or use network access. Original detached-child evidence remains untouched.

Real probes establish:

- Normal and nested children in the recorded group stop writing; unrelated direct-child sentinels survive.
- New-session descendants keep writing after normal leader exit, timeout and cancellation. A full attempt can record SUCCEEDED, then its workspace changes again.
- SIGKILL of the deadline-owning controller releases its lease while the provider keeps writing beyond that deadline; persisted state remains RUNNING. Manual recovery is idempotent and never relaunches the provider or repeats verification. This is not automatic supervision.
- An ephemeral launchd job with `AbandonProcessGroup=false` exits zero and is no longer running while its new-session descendant continues writing. The unique test job is booted out afterward; no persistent service is installed.
- Local kqueue NOTE_TRACK registration returns ENOTSUP. Current SDK headers mark recursive tracking unsupported since 10.5. Notifications supply no atomic kill-all boundary.
- Forced detach of an owned 64 MiB APFS image makes a held writable file descriptor fail with EIO. The existing static subpath grant nevertheless allows the child to recreate/write the host directory exposed at that mountpoint. A new volume alone is insufficient.

No name/cwd/environment/PID sweep, extra killpg, cooperative daemonization rule, or delay is selected as a fix. Existing start-time/token checks reduce mistaken targeting but their check-then-signal sequence is not a race-free PID capability. The probes signal only unreaped direct child handles owned by the harness; escaped test descendants self-expire. Existing manual recovery is tested as its current bounded mechanism, not promoted to a safety proof.

## Boundary that would solve the problem

A credible proposed architecture is an exclusively owned VM whose task filesystem and runtime writes are guest-owned, with no writable host share, and a system-supervised owner of its durable deadline. Terminal publication and artifact extraction would wait for confirmed VM hard stop. Guest setsid cannot leave that boundary through session/group changes. This remains an architectural inference from the VM contract, not a demonstrated Nightshift execution mode. Helper SIGKILL, ownership across restart, safe result extraction, guest tools/Grok/network and exactly-once launch/verification all need real adversarial proof before adoption.

A stricter native owned-volume design could prove the narrower workspace-write invariant if mountpoint recreation, alternative aliases, raw image/device access, all writable runtime roots and crash-independent revocation are closed. It still does not inherently stop detached computation or model/network work, and has not met this task's stronger Case A requirement. Retain it as a candidate, rather than asserting impossibility or shipping an unproved mode.

Estimated migration: three meaningful slices—explicit native helper/deployment contract; guest filesystem/toolchain and bounded artifact transfer; independent durable deadline and crash reconciliation. Expect weeks of engineering and platform validation, not a process-group patch. No root requirement, runtime dependency, service, execution mode or framework is introduced here.

A fixed-capacity guest filesystem could bound guest writes. It does not automatically bound host snapshots, logs or exports. Current Nightshift has no workspace quota; its 64 MiB free-space preflight remains only an availability check.

## Truthful current claim

Nightshift provides isolated clones, static filesystem containment, bounded captured output, controller-owned timeouts, audit records and explicit recovery. It does not guarantee no writes after terminal state or independently enforced deadlines after controller death. Green unit tests, packaging or the prior real Grok canary do not override the failed macOS lifetime gate. No new tag, release or hardened-mode label is justified.
