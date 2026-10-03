# macOS task lifetime research — 2026-10-04

No evaluated candidate has demonstrated both required invariants for Nightshift's current Python CLI on the tested macOS release. This is a bounded engineering result, not a claim that macOS cannot implement such a product. Forced unmount of an owned per-attempt volume has a documented file-access revocation property, now confirmed for a held writable descriptor; the existing path grant still permits new writes after unmount. A separate VM with guest-owned storage is another credible boundary. Both need independently supervised ownership; neither is an implemented or certified Nightshift mode.

The required properties are (1) terminal attempt state implies that no attempt process retains workspace write capability, and (2) controller death leaves an independently enforced finite lifetime or system-owned automatic reconciliation. Observing descendants, killing a process group, and recovering a row do not imply either property.

## Evidence scope and freshness

Research inspected the clean engineering baseline `a8fe10f2fe05c54e9b551823dc55aef3ada32e18`. This note changes documentation only. It neither launches a model nor installs services. Existing detached-child evidence must remain intact. The primary-source research itself ran no VM, EndpointSecurity client, XPC service or mounted-volume probe. The strike separately performed the real process, temporary-job and mounted-volume experiments reported below; no model or persistent service was launched.

Local facts: macOS 26.5, build 25F71, Darwin 25.5.0; Command Line Tools SDK is macOS 26.4. `launchctl version` identifies `libxpc_executables-3102.120.13~112/launchd/RELEASE_ARM64E`. Local `launchd.plist(5)`, `launchctl(1)`, `sandbox-exec(1)`, `sandbox_init(3)` and SDK headers were read. The SDK can lag the running OS. Apple's published XNU source below is pinned to `f6217f891ac0bb64f3d375211650a4c1ff8ca1ea`, whose commit date is 2025-10-16; it is explanatory source, not a claim that it exactly matches Darwin 25.5.0. The published launchd repository contains much older code, so current local manuals take precedence over it.

## Process model and ownership

```text
Nightshift controller (SQLite state, lease, timeout loop)
  └─ provider / sandbox-exec (new session, recorded PGID)
       ├─ tools in the recorded group
       └─ descendant
            └─ setsid() or setpgid() → another group/session
                 └─ inherited workspace access survives leader exit

controller SIGKILL → timeout loop disappears
                    lease unlocks; provider/descendants may remain
                    recovery runs only when someone invokes it
```

The current profile allows `process*` and workspace writes. Session/group identity and inherited filesystem authority are different boundaries. A child does not lose workspace access merely because the process that launched it has exited. The durable journal and lease prevent particular duplicate actions; they are not kernel task membership or an independently running deadline owner.

## Evaluation criteria

Every candidate below answers the same ten questions: **1** setsid/setpgid escape; **2** controller SIGKILL; **3** deadline owner; **4** unrelated-process safety; **5** PID reuse; **6** administrator privilege; **7** bundle/signing/entitlements; **8** Python CLI fit; **9** Grok/network/tools; **10** deterministic tests. “Not established” means the cited contract does not justify the required guarantee, not that an undocumented behavior has been disproved.

### 1. launchd / launchctl job supervision

The current local manual documents cleanup of the job's *same* process group. `AbandonProcessGroup=true` disables that cleanup; leaving it false does not broaden membership. `TimeOut` is unimplemented, while `ExitTimeOut` sets the TERM-to-KILL grace when a job is stopped, not an absolute task runtime deadline. `KeepAlive` can restart a supervisor, but does not encode an attempt's deadline or prevent replay of expensive work. These semantics agree with the older [Apple launchd manual source](https://github.com/apple-oss-distributions/launchd/blob/main/man/launchd.plist.5), whose age prevents using it as proof of current implementation details.

| Criterion | Answer |
| --- | --- |
| 1 | A new PGID is outside the documented default cleanup set. No setsid containment guarantee is established. |
| 2 | A separately registered supervisor can outlive the CLI controller. Directly managing the provider still leaves detached descendants. |
| 3 | Application supervisor code must enforce a durable deadline; launchd manages that supervisor's availability. |
| 4 | Job-label operations avoid executable-name sweeps, but do not identify every attempt descendant. Domain-wide teardown would affect unrelated jobs. |
| 5 | Service identity avoids choosing a job solely by a stale PID. It does not supply safe arbitrary descendant signalling. |
| 6 | A per-user job need not require root; modifying the system domain does. |
| 7 | A legacy Python LaunchAgent can use a plist; an app bundle is not intrinsically required for this variant. |
| 8 | A possible supervision component, but persistent installation/configuration is a new product contract, not a hidden CLI change. |
| 9 | Existing executable launch can remain; launchd environment/TCC differences require validation. |
| 10 | Unique temporary jobs can test restarts and controller death. A passing PGID test cannot certify escaped-child cleanup. |

The local `launchctl(1)` also warns that human-readable output is not a stable programmatic API. A future supervisor must not base correctness on parsing undocumented `print`/`procinfo` output. No launchd service was installed by this research.

### 2. Resource / jetsam coalitions

Apple's [coalition design note](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/doc/observability/coalitions.md) documents inherited membership and non-reused IDs, but reserves create/manage/spawn interfaces for launchd or XNU testing. A loaded service can retain its coalition across restarts. Crucially, [sys_coalition.c](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/kern/sys_coalition.c#L95) describes terminate as an empty-notification request and says existing members may still fork. [coalition_request_terminate_internal](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/osfmk/kern/coalition.c#L2179) does not kill members. The syscall checks privileged-coalition membership. The name `coalition_terminate` must not be interpreted as a public kill-all capability.

| Criterion | Answer |
| --- | --- |
| 1 | Published membership survives process creation; changing a session is not a documented membership exit. That is useful identity, not teardown. |
| 2 | Membership/accounting survives controller exit; a deadline does not follow from it. |
| 3 | No job deadline owner is supplied. |
| 4 | A service coalition may span restarts; treating it as one attempt risks mixing attempts. |
| 5 | Coalition IDs do not reuse, but enumerated member PIDs still need safe target identity. |
| 6 | Management is privileged/restricted; merely becoming root is not documented as sufficient for every operation. |
| 7 | No ordinary public entitlement/package workflow for Nightshift was established. |
| 8 | Unsupported management interfaces are unsuitable for the current Python package. |
| 9 | Accounting itself need not impede tools; restricted management cannot be assumed deployable. |
| 10 | XNU tests are not proof of an available production API on this host. |

The same source's root-only logical-write ledger limit is not a supported per-workspace capacity quota. It is not implemented here.

### 3. XPC service lifecycle

Apple [XPC documentation](https://developer.apple.com/documentation/xpc) distinguishes per-client bundled services, whose service process follows the client lifetime, from agents/daemons that can continue work after the client exits. The contract speaks about the service process. It does not establish recursive termination of arbitrary forked processes in new sessions or revoke their already-granted filesystem access. These are deliberately separate claims.

| Criterion | Answer |
| --- | --- |
| 1 | The service-lifetime contract does not prove containment of a descendant that creates another session/group. |
| 2 | A bundled per-client service exits with its client; an independent agent can continue. Neither alone proves the attempt tree is gone. |
| 3 | An independent service/agent needs its own durable timeout implementation. |
| 4 | Connection/service identity narrows the service target; it is not a complete attempt-descendant target. |
| 5 | XPC connection identity avoids PID-only peer selection; arbitrary child cleanup remains separate. |
| 6 | A user-level service/agent need not be root; a system daemon differs. |
| 7 | Bundled XPC services belong in an app/framework; production distribution needs a native packaging/signing design. |
| 8 | A native helper layer is possible, but exceeds the stdlib CLI's present deployment contract. |
| 9 | Grok/tools require a separately tested subprocess and sandbox configuration; XPC does not automatically authorize them. |
| 10 | Service death is testable. Detached descendants and retained writes still require real failure injection. |

### 4. Dedicated helper / watchdog and kqueue EVFILT_PROC

A per-attempt helper can remain alive after the controller dies and own its timer. If launchd restarts that helper, durable reconciliation could re-establish supervision without model retry. This solves availability only if correctly designed; it supplies no new containment set.

The current SDK `usr/include/sys/event.h:356` explicitly marks `NOTE_TRACK`, `NOTE_TRACKERR`, and `NOTE_CHILD` unsupported since 10.5. Its EVFILT_PROC comment also says a fork event does not expose the child PID through the event. Apple's [published event.h](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/sys/event.h) contains the same limitations. kqueue is suitable for watching a registered process exit, not for inventing reliable recursive process membership by combining notifications with later scans.

| Criterion | Answer |
| --- | --- |
| 1 | A timer/helper plus killpg still permits escape. Fork notifications plus PID scans do not close creation/registration races. |
| 2 | A separate helper can survive controller SIGKILL; an unsupervised helper can itself die and lose enforcement. |
| 3 | The helper owns a timer; launchd could re-establish it from durable state. This is proposed, not implemented. |
| 4 | A process-sweep cleanup cannot be certified safe; a helper must operate on a stronger owned boundary. |
| 5 | Event registration is not an atomic identity-aware kill capability. Start-time comparison followed by kill still has a race. |
| 6 | Basic kqueue/helper use does not require root. |
| 7 | Basic helper use needs no app bundle or special entitlement; launchd deployment is a separate decision. |
| 8 | Fits a small Python component for supervision, but is insufficient as the chosen lifetime boundary. |
| 9 | Existing Grok/tools can remain unchanged; that also retains the existing escape path. |
| 10 | Timer/controller-death behavior is deterministic to inject; unit mocks cannot prove tree containment. |

### 5. Seatbelt / App Sandbox / revocable workspace access

The existing profile constrains resources, not finite lifetime. Both `sandbox-exec(1)` and `sandbox_init(3)` are deprecated locally; the latter explains that restrictions are generally checked at resource acquisition. Apple documents [App Sandbox inheritance](https://developer.apple.com/library/archive/documentation/Miscellaneous/Reference/EntitlementKeyReference/Chapters/EnablingAppSandbox.html) and a [signed helper-tool workflow](https://developer.apple.com/documentation/xcode/embedding-a-helper-tool-in-a-sandboxed-app). Neither establishes a parent-owned, terminal-state revocation of this profile's static write allowance in every descendant.

| Criterion | Answer |
| --- | --- |
| 1 | Existing sandbox inheritance preserves the workspace write grant even if a child changes session/group; reproduced behavior is consistent with that model. |
| 2 | A static grant has no controller-death timer. |
| 3 | No independent deadline owner is supplied. |
| 4 | Static path rules avoid unrelated-process kills, but do not revoke an attempt's existing authority. |
| 5 | Resource policy is not PID-based. That advantage does not establish revocation. |
| 6 | Existing sandbox-exec use is unprivileged. |
| 7 | Existing mode needs no new entitlement; a supported App Sandbox package requires explicit signed executables/entitlements. |
| 8 | Present mechanism fits the CLI, but a public dynamic revocation contract was not established. |
| 9 | Existing profile preserves required tools/network; denying all process creation would break the product. |
| 10 | Inherited writes and held-open descriptors/mappings are testable. A revoke-all-writes claim would need their explicit coverage. |

Changing permissions, moving a path, or marking a directory read-only is not assumed to revoke every retained file descriptor/mapping. Private sandbox-extension symbols are not an acceptable package contract; this research found no supported public replacement for the current static grant that proves the required lifecycle property. This does not rule out a fundamentally different mediated-filesystem design, which would be a much larger compatibility change.

### 6. EndpointSecurity (including new descendants client)

The released [client API](https://developer.apple.com/documentation/endpointsecurity/client) requires entitlement, root, and user Full Disk Access approval. Apple must approve the [restricted entitlement](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.endpoint-security.client); its [daemon signing workflow](https://developer.apple.com/documentation/xcode/signing-a-daemon-with-a-restricted-entitlement) adds native distribution requirements. The framework delivers policy events; for example [NOTIFY_WRITE](https://developer.apple.com/documentation/endpointsecurity/es_event_type_notify_write) is a notification, not a general write-revocation API.

The official Markdown metadata for [es_new_descendants_client](https://developer.apple.com/documentation/endpointsecurity/es_new_descendants_client(_:_:)) declares **macOS 27.0** availability. It recursively scopes events to descendants, drops root/TCC requirements, and still requires the restricted entitlement. It is absent from this host's 26.4 SDK and documented as beta on Apple's rendered page. It cannot be silently treated as a released 26.5 solution. Its [authorization deadline configuration](https://developer.apple.com/documentation/endpointsecurity/es_set_deadline_min_milliseconds(_:_:_:_:)) concerns event-response deadlines, not an attempt's wall-clock lifetime.

| Criterion | Answer |
| --- | --- |
| 1 | The future descendants API documents recursive visibility; no complete task teardown / retained-write revocation guarantee was established. |
| 2 | A client process/service must remain available; its loss is not documented as killing every monitored process. |
| 3 | A separate supervisor still needs an attempt deadline. ES event response deadlines are different. |
| 4 | Audit-token/scoped event identity can narrow observations; any signal enforcement needs its own race-safe target mechanism. |
| 5 | Audit tokens improve identity over numeric PID alone; conversion to PID plus ordinary kill loses that protection. |
| 6 | Released es_new_client needs root/FDA; future descendants client says neither root nor TCC, but is unavailable here. |
| 7 | Apple-restricted entitlement and native signing/provisioning are required; an ES system extension is one distribution option, not universally mandatory. |
| 8 | A substantial optional security-service dependency, not a reasonable invisible stdlib CLI implementation. |
| 9 | Tool/network compatibility and auth policy would require validation; observing exec does not establish compatibility. |
| 10 | Cannot deterministically certify this candidate on the current host without an entitled executable; future API also needs its supported OS. |

### 7. Dedicated VM / VM-backed container with guest-owned storage

Apple's [Virtualization framework](https://developer.apple.com/documentation/virtualization) runs a guest OS. [VZVirtualMachine](https://developer.apple.com/documentation/virtualization/vzvirtualmachine) requires `com.apple.security.virtualization`. Its [hard stop](https://developer.apple.com/documentation/virtualization/vzvirtualmachine/stop(completionhandler:)) stops the VM without guest cooperation and reports completion or error; `requestStop` only requests a guest shutdown. [NAT networking](https://developer.apple.com/documentation/virtualization/vznatnetworkdeviceattachment) preserves guest network access without the extra VM networking entitlement. [Disk-image attachments](https://developer.apple.com/documentation/virtualization/vzdiskimagestoragedeviceattachment) provide a finite virtual block device.

Apple's [Containerization](https://github.com/apple/containerization) executes each Linux container in its own lightweight VM. The separate [container tool](https://github.com/apple/container) documents an installed system service and an installer that asks for administrator permission. Neither is a current Nightshift dependency. No runtime was installed, and no default mode was renamed “hardened.”

| Criterion | Answer |
| --- | --- |
| 1 | Guest setsid/setpgid cannot leave the VM through those syscalls. Architectural inference: hard-stopping its owned VM stops guest execution, independent of guest PGID. |
| 2 | A separate durable supervisor must retain VM ownership or establish a documented bounded shutdown on owner death. This composite path remains unproven. |
| 3 | System-supervised helper owns a durable deadline and the VM handle; CLI death alone must not lose either. |
| 4 | Operate only on a private VM object/attempt identity, never host PID sweeps or all-VM shutdown. This must be tested. |
| 5 | VM object identity avoids guest PID reuse in host teardown; durable identity across helper restart remains a design requirement. |
| 6 | Virtualization does not document a general root requirement; Apple container's packaged installer does request admin permission. NAT avoids bridged-network privilege requirements. |
| 7 | A signed native helper needs virtualization entitlement; a bare pure-Python wheel is not the complete deployment artifact. |
| 8 | Python can keep intake/state/reporting and call an explicit optional helper. Guest image/runtime/tool provisioning is a material new product dependency. |
| 9 | NAT permits model traffic. Grok binaries, authentication, platform-specific checks and toolchains must be provisioned and validated in the guest. Current host execution is not automatically preserved. |
| 10 | Pin a guest image and run the real failure matrix. No VM integration proof exists from this research, so this is not READY. |

The candidate needs **no writable host directory sharing** for the attempt: stage an input snapshot into guest storage, then hard-stop and confirm the VM is stopped before extracting results into a controller-owned output area. This conclusion assumes the normal hypervisor security boundary, not resistance to a guest kernel/hypervisor exploit. A fixed-capacity guest disk naturally bounds guest writes, provided auxiliary disks, snapshots, logs, exported artifacts and host shares are also bounded. A guest disk's capacity alone is not a bound on all host storage consumed by the product.

### 8. Fixed-capacity disk image / owned APFS volume with forced unmount

This candidate targets write-capability revocation instead of killing all descendants. Apple's [unmount manual](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/unmount.2.html), confirmed by local `unmount(2)` and `umount(8)`, states that `MNT_FORCE` causes subsequent accesses to active ordinary files to fail even if the filesystem is remounted. Special devices are an explicit exception. The [kernel unmount-by-fsid contract](https://developer.apple.com/documentation/kernel/1523503-vfs_unmountbyfsid) says open files are invalidated but already-running I/O may finish. The API itself is kernel-side; user-space enforcement uses unmount or Disk Arbitration, not calling a kernel symbol from Python.

Apple's [DADiskUnmount options](https://developer.apple.com/documentation/diskarbitration/dadiskunmountoptions) provide force-unmount even with active files. Its [Disk Arbitration guide](https://developer.apple.com/library/archive/documentation/DriversKernelHardware/Conceptual/DiskArbitrationProgGuide/ManipulatingDisks/ManipulatingDisks.html) requires checking the completion callback and dissenter/error result. Current `hdiutil(1)` separates image attachment from filesystem mounting; `detach -force` ignores active files. “Associated process” termination in that manual describes the image machinery, not every process using a file on the volume. `diskutil(8)` permits failures and potentially lengthy unmount operations. Starting unmount is not a terminal-state proof.

| Criterion | Answer |
| --- | --- |
| 1 | Changing session/group cannot preserve ordinary open-file access after successful forced unmount under the documented contract. The process itself can remain alive. |
| 2 | Controller SIGKILL does not inherently detach its image. Independent supervision is still required. |
| 3 | A separate helper must enforce the durable deadline and confirm revocation; system disk arbitration performs a requested unmount, not the attempt timer. |
| 4 | An exclusively owned per-attempt volume narrows impact. Never force-unmount a shared/source/system volume. Cached device names or paths must be revalidated before destructive actions. |
| 5 | Revocation does not signal PIDs; device-name reuse and path substitution are the analogous identity hazards. Bind image/volume identity to an ownership receipt and validate it. |
| 6 | The local syscall contract allows unmount by the mounting user, subject to system policy. Owned-image CLI behavior requires a real non-root probe. Creating a volume on a shared physical APFS container is a different and riskier operation. |
| 7 | hdiutil/diskutil subprocess use needs no new application entitlement in the current unsandboxed controller; a Disk Arbitration native helper is an optional integration choice. |
| 8 | An explicit owned-image backend could fit a Python CLI. Image lifecycle, safe mountpoint layout, result extraction and durable supervisor are real additions, not just a finally block. |
| 9 | A normal mounted filesystem can run current host Grok/tools; network policy can remain. Mount restrictions, space exhaustion and verification/result extraction require compatibility tests. |
| 10 | Actual held-FD revocation passed; actual pathname-recreation revocation failed with the existing policy. Still test dirfds, mmap/msync, remount/alternate aliases, controller/helper death, identity substitution and unrelated-volume sentinels. |

A crucial distinction: after unmount the previously covered mountpoint directory becomes visible again. A static Seatbelt subpath grant could still authorize new writes there. Underlying-directory permissions alone cannot be assumed immutable against a same-UID descendant. The image file, raw device nodes, mount helpers, Disk Arbitration IPC, other permitted runtime write roots and alternative mount aliases also need a tested access policy. These are proof obligations, not asserted exploits in this research. `revoke(2)` is not a fallback for regular workspace files: the local manual supports only block/character special files.

Published [Disk Arbitration mount authorization](https://github.com/apple-oss-distributions/DiskArbitration/blob/a542bda934211dc3c301bfdcc7f21349c4164a85/diskarbitrationd/DAServer.c#L2418) checks the requesting audit token's `file-mount` policy. Thus remount denial is a plausible policy component; IPC should not simply be assumed to bypass it. That source is dated 2025-10-16 and is not a complete current-host guarantee. Published [VFS forced-unmount cleanup](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/vfs/vfs_subr.c#L3130) explicitly handles mapped-file UBC references, supporting a real mmap revocation probe without substituting source inspection for the result.

An owned fixed-size image imposes a real storage capacity for that filesystem; local `diskutil(8)` and Apple's [APFS volume guide](https://support.apple.com/en-asia/guide/disk-utility/dskua9e6a110/mac) also document a per-volume maximum user-data quota. Neither bounds unrelated writable runtime/log roots, backing-image metadata, snapshots or exported results. Do not call a free-space check a quota. Force-unmount can discard pending writes, so extraction/recovery must report errors honestly and preserve evidence rather than silently mark success.

This is a technically meaningful **native file-access revocation substrate**, with a real held-FD proof and a demonstrated current-policy pathname gap. A complete authority/ownership design is still required. It does not by itself meet Case A's stronger demand that no setsid descendant survive, nor the independent deadline requirement. If a future product accepts surviving compute with revoked filesystem access, it must state that narrower contract explicitly; this round must not silently substitute it for the user’s release gate.

## Local evidence collected by the strike

The separate macOS integration runner executed eleven release-gate cases on baseline `a8fe10f`: seven fail the required invariants, with no harness errors. Ordinary and nested same-group cleanup, unrelated-process survival, and repeated explicit recovery pass. Normal leader exit with a setsid child, a setsid grandchild, setpgid escape, timeout, cancellation, controller SIGKILL past deadline, and terminal-state write revocation fail. The full production runner can record `SUCCEEDED` while the detached writer continues growing its counter. The evidence is `engineering-evidence/task-lifetime-gate-final-reviewed.json` on the operator host; those negative gate cases must remain genuine failures, not expected-success limitations.

The strike also submitted an ephemeral per-user launchd job with `AbandonProcessGroup=false` under the actual Seatbelt profile. The managed leader finished with exit 0; the detached child continued writing. The job was removed afterward; no persistent service remains. Actual kqueue registration with `NOTE_TRACK` returned errno 45 (`ENOTSUP`). These observations are retained in `engineering-evidence/native-primitive-probes-verified.json` and agree with the local documentation. They falsify those particular proposed fixes, not every possible macOS architecture.

The initial image-creation syntax error and later `/var` versus `/private/var` path-comparison error were inconclusive and are not evidence against volume revocation. The corrected macOS 26.5 experiment used an owned 64 MiB APFS image without an administrator prompt. Forced detach returned 0; writing through the held ordinary-file descriptor afterward returned errno 5 (`EIO`). However, a child under the existing static Seatbelt subpath grant successfully reopened/recreated the uncovered host mountpoint path and wrote there after detach. The child was finite and exited 0; the owned image was detached and its temporary directory removed. The conclusive artifact is `engineering-evidence/native-primitive-probes-verified.json (volume section)`, distinct from earlier inconclusive attempts.

That result recognizes a real revocation primitive and falsifies the simple “same profile plus force-detach” fix. A different backend with immutable or sandbox-denied lower mountpoint access, inaccessible backing images/devices, every writable root mediated, and an independent watchdog could potentially establish the narrower workspace-write invariant. It remains a design to prove; this note neither rejects it universally nor claims that it stops detached CPU/network/model activity.

## Current conclusion and migration scope

The native candidates supply pieces of supervision, membership, policy or file-access revocation. No complete supported combination has demonstrated both invariants within the current CLI deployment contract. Actual probes show the launchd process-group escape, unsupported kqueue tracking, genuine forced-unmount FD revocation, and continued same-path writes under the current policy. The result is **Case B, bounded to this product and tested release**. Keep the unattended release decision **NOT READY** until the composite lifecycle and the user's stronger release tests pass. Retain the current containment and explicit recovery semantics; do not describe manual `recover` as automatic deadline enforcement. No new execution mode, runtime dependency or production process change is introduced by this research.

A VM boundary is a proposed research direction, not an accepted mode or a completed fix. Engineering estimate: three separately reviewed slices—(1) native signed helper and explicit capability/deployment contract; (2) guest provisioning, finite disk and artifact import/export preserving Work Order/report semantics; (3) system-owned durable deadline/reconciliation with exactly-once launch and interrupted-verification handling. Each needs real macOS integration evidence. Effort is likely measured in weeks rather than a killpg patch; this is an estimate, not a delivery promise.

The final adversarial gate must include ordinary and nested children, setsid, normal leader exit, timeout, cancellation, controller and helper SIGKILL, held-open file/mmap writes, unrelated sentinel processes, repeated reconciliation, no duplicate provider/verification launch, and immutable terminal output. A helper crash while a VM exists is a separate unproven edge; citing hard-stop documentation is not a substitute for demonstrating how a restarted supervisor safely reconciles it. Do not publish 0.3.0 on the strength of this note.
