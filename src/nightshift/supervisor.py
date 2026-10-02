"""Single-threaded supervisor, cancellation, and crash reconciliation.

`recover` never launches a provider and never increments the attempt counter
unless the operator passes `--retry`. Retry stops at max_attempts.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from nightshift.config import Config
from nightshift.db import Database
from nightshift.locks import LockManager, phase_process_alive, process_matches
from nightshift.models import (
    RETRYABLE_STATES,
    TERMINAL_STATES,
    IllegalTransition,
    NightshiftError,
    RunRecord,
    RunState,
    exit_code_for_state,
    utc_now,
)
from nightshift.providers.base import terminate_process
from nightshift.queue import claim_next
from nightshift.finalize import check_stored_integrity, decide_final, extension_audit_ok
from nightshift.runner import execute_run, publish_report, run_verification_only
from nightshift.runtime import scrub_per_run_auth

TERMINAL_VALUES = {state.value for state in TERMINAL_STATES}
RETRYABLE_VALUES = {state.value for state in RETRYABLE_STATES}


@dataclass(frozen=True)
class ProcessProbe:
    alive: bool
    workspace_exists: bool


@dataclass(frozen=True)
class RecoveryDecision:
    classification: str
    action: str
    new_state: str | None
    reason: str
    launch_provider: bool = False


def classify_recovery(run: RunRecord, probe: ProcessProbe) -> RecoveryDecision:
    """Decide how to reconcile one run. `launch_provider` is always false."""
    if run.state in TERMINAL_VALUES:
        return RecoveryDecision("already_terminal", "leave", None, "run is already terminal", False)
    if run.state == RunState.QUEUED.value:
        return RecoveryDecision("queued_not_started", "leave", None, "queued; not started", False)
    if run.state == RunState.PREPARING.value:
        if probe.alive:
            return RecoveryDecision("process_still_alive", "leave", None, "preparing process still alive", False)
        return RecoveryDecision(
            "interrupted_before_launch",
            "mark",
            RunState.INTERRUPTED.value,
            "interrupted before provider launch",
            False,
        )
    if run.state == RunState.RUNNING.value:
        if probe.alive:
            return RecoveryDecision("process_still_alive", "leave", None, "process still alive", False)
        if run.provider_exit_code is None:
            if probe.workspace_exists:
                return RecoveryDecision(
                    "process_gone_workspace_intact",
                    "mark",
                    RunState.INTERRUPTED.value,
                    "process gone; workspace intact; provider not relaunched",
                    False,
                )
            return RecoveryDecision(
                "process_gone_workspace_missing",
                "mark",
                RunState.FAILED.value,
                "process gone and workspace missing",
                False,
            )
        if not run.verification_ran:
            return RecoveryDecision(
                "verification_never_ran",
                "verify",
                None,
                "provider exited; verification never ran",
                False,
            )
        return RecoveryDecision(
            "provider_exited",
            "finalize",
            None,
            "provider exited and verification was already recorded",
            False,
        )
    if run.state == RunState.VERIFYING.value:
        if probe.alive:
            return RecoveryDecision(
                "verification_still_alive",
                "leave",
                None,
                "verification process still alive",
                False,
            )
        if not run.verification_ran:
            return RecoveryDecision(
                "verification_never_ran",
                "verify",
                None,
                "left in VERIFYING before verification finished",
                False,
            )
        return RecoveryDecision("provider_exited", "finalize", None, "verification recorded; finalize", False)
    return RecoveryDecision("already_terminal", "leave", None, f"unhandled state {run.state}", False)


def _scrub_recovered_auth(config: Config, run: RunRecord) -> None:
    """Drop a per-run auth copy left behind by a crash, cancel, or interrupt."""
    if run.run_dir:
        scrub_per_run_auth(Path(run.run_dir))
        return
    candidate = config.runs_dir / run.run_id
    if candidate.exists():
        scrub_per_run_auth(candidate)


def probe_run(run: RunRecord) -> ProcessProbe:
    """Alive means the recorded phase process is still the same process.

    PREPARING, verification, and inspect match the pid and its start time.
    The provider also requires its session id on the command line. A recycled
    pid with a different start time is not alive. Rows written before phase
    metadata existed still use pid plus token.
    """
    meta = run.process_meta if isinstance(run.process_meta, dict) else {}
    alive = phase_process_alive(meta, fallback_pid=run.pid)
    exists = bool(run.workspace_path) and Path(run.workspace_path).is_dir()
    return ProcessProbe(alive=alive, workspace_exists=exists)


def recover_run(config: Config, db: Database, locks: LockManager, run_id: str) -> tuple[RunRecord, RecoveryDecision]:
    run = db.require_run(run_id)
    _scrub_recovered_auth(config, run)
    decision = classify_recovery(run, probe_run(run))
    if decision.launch_provider:
        raise NightshiftError("recover refused to launch a provider")
    if decision.action == "leave":
        db.update_run(run_id, recovery_class=decision.classification)
        return db.require_run(run_id), decision
    if decision.action == "mark" and decision.new_state:
        current = db.require_run(run_id)
        if current.state not in TERMINAL_VALUES:
            ended = utc_now()
            db.transition(
                run_id,
                decision.new_state,
                decision.reason,
                failure_reason=decision.reason,
                recovery_class=decision.classification,
                ended_at=ended,
                exit_code=exit_code_for_state(decision.new_state),
                duration_seconds=current.duration_seconds,
            )
        else:
            db.update_run(run_id, recovery_class=decision.classification)
        locks.release(run_id)
        publish_report(config, db, run_id, extra_uncertainty=[decision.reason])
        return db.require_run(run_id), decision
    if decision.action == "verify":
        db.update_run(run_id, recovery_class=decision.classification)
        updated = run_verification_only(config, db, run_id)
        locks.release(run_id)
        return updated, decision
    if decision.action == "finalize":
        _finalize_recorded(db, run_id, decision)
        locks.release(run_id)
        publish_report(config, db, run_id, extra_uncertainty=[decision.reason])
        return db.require_run(run_id), decision
    return db.require_run(run_id), decision


def recover_all(config: Config, db: Database, locks: LockManager, *, retry: bool = False) -> tuple[list[str], bool]:
    lines: list[str] = []
    refused = False
    for run in list(db.active_runs()):
        attempt_before = run.attempt
        updated, decision = recover_run(config, db, locks, run.run_id)
        if updated.attempt != attempt_before:
            raise NightshiftError("recover changed the attempt count without an explicit retry")
        lines.append(
            f"{updated.run_id} {updated.state} {decision.classification} attempt={updated.attempt}"
        )
        if retry and updated.state in RETRYABLE_VALUES:
            try:
                retried = retry_run(db, updated.run_id)
                lines.append(f"{retried.run_id} QUEUED retry attempt={retried.attempt}")
            except NightshiftError as exc:
                refused = True
                lines.append(f"{updated.run_id} retry refused: {exc}")
    if not lines:
        lines.append("nothing to recover")
    return lines, refused


def retry_run(db: Database, run_id: str) -> RunRecord:
    run = db.require_run(run_id)
    if run.state not in RETRYABLE_VALUES:
        raise NightshiftError(f"cannot retry run in state {run.state}")
    if run.attempt >= run.max_attempts:
        raise NightshiftError("maximum attempts reached")
    return db.transition(
        run_id,
        RunState.QUEUED.value,
        "explicit retry",
        attempt=run.attempt + 1,
        failure_reason="",
        ended_at="",
        started_at="",
        session_id="",
        pid=None,
        process_meta={},
        exit_code=None,
        provider_exit_code=None,
        verification_exit_code=None,
        verification_ran=False,
        recovery_class="",
        invocation={},
        provider_argv=[],
    )


def cancel_run(config: Config, db: Database, locks: LockManager, run_id: str) -> RunRecord:
    run = db.require_run(run_id)
    if run.state == RunState.CANCELLED.value:
        return run
    if run.state in TERMINAL_VALUES:
        raise NightshiftError(f"cannot cancel terminal run in state {run.state}")
    # Record CANCELLED before signalling. The worker observes the dead child
    # and would otherwise settle FAILED or SUCCEEDED while this function is
    # still inside terminate_process.
    cancelled = _record_cancellation(db, run_id)
    if cancelled.state != RunState.CANCELLED.value:
        return cancelled
    _signal_cancelled_child(cancelled)
    _scrub_recovered_auth(config, cancelled)
    if not probe_run(db.require_run(run_id)).alive:
        locks.release(run_id)
    if cancelled.run_dir or (config.runs_dir / run_id).exists():
        if not cancelled.run_dir:
            db.update_run(run_id, run_dir=str(config.runs_dir / run_id))
        publish_report(config, db, run_id, extra_uncertainty=["Cancelled by operator."])
    return db.require_run(run_id)


def _record_cancellation(db: Database, run_id: str) -> RunRecord:
    """Move a live run to CANCELLED, or return the terminal row if it already finished."""
    cancellable = {
        RunState.QUEUED.value,
        RunState.PREPARING.value,
        RunState.RUNNING.value,
        RunState.VERIFYING.value,
    }
    for _ in range(3):
        current = db.require_run(run_id)
        if current.state == RunState.CANCELLED.value or current.state not in cancellable:
            return current
        fields: dict[str, object] = {"failure_reason": "cancelled"}
        if current.state != RunState.QUEUED.value:
            fields["ended_at"] = utc_now()
            fields["exit_code"] = exit_code_for_state(RunState.CANCELLED.value)
        try:
            return db.transition(run_id, RunState.CANCELLED.value, "cancelled", **fields)
        except IllegalTransition:
            continue
    return db.require_run(run_id)


def _signal_cancelled_child(run: RunRecord) -> None:
    """Terminate the provider, inspect, or verification child. Never the preparing controller."""
    meta = run.process_meta if isinstance(run.process_meta, dict) else {}
    phase = str(meta.get("phase") or "")
    if phase == "preparing":
        return
    token = str(meta.get("match") or "")
    raw_pgid = meta.get("pgid")
    pgid = int(raw_pgid) if isinstance(raw_pgid, int) else None
    raw_pid = meta.get("pid") if meta.get("pid") is not None else run.pid
    try:
        pid = int(raw_pid) if raw_pid is not None else None
    except (TypeError, ValueError):
        pid = None
    child_alive = probe_run(run).alive and pid is not None and pid != os.getpid()
    if not child_alive:
        return
    if phase in {"provider", "verifying", "inspect"} or process_matches(pid, token):
        terminate_process(pid, pgid)


def serve(config: Config, db: Database, locks: LockManager, stop: threading.Event) -> int:
    """Claim queued work until `stop` is set. Startup recover does not relaunch providers."""
    recover_all(config, db, locks, retry=False)
    while not stop.is_set():
        active = db.count_states(
            [RunState.PREPARING.value, RunState.RUNNING.value, RunState.VERIFYING.value]
        )
        if active >= config.concurrency:
            stop.wait(config.poll_interval_seconds)
            continue
        run = claim_next(db)
        if run is None:
            stop.wait(config.poll_interval_seconds)
            continue
        execute_run(config, db, locks, run.run_id, stop_event=stop)
    return 0


def _finalize_recorded(db: Database, run_id: str, decision: RecoveryDecision) -> None:
    """Apply the same success gate as a normal run. Does not launch a provider."""
    run = db.require_run(run_id)
    if run.state == RunState.RUNNING.value:
        db.transition(run_id, RunState.VERIFYING.value, "recover: provider already exited")
        run = db.require_run(run_id)
    if run.state in TERMINAL_VALUES:
        db.update_run(run_id, recovery_class=decision.classification)
        return
    workspace_exists = bool(run.workspace_path) and Path(run.workspace_path).is_dir()
    integrity = check_stored_integrity(run.source_repo, run.source_integrity)
    final = decide_final(
        provider_exit_code=run.provider_exit_code,
        verification_ran=run.verification_ran,
        verification_exit_code=run.verification_exit_code,
        workspace_exists=workspace_exists,
        integrity_ok=integrity.ok,
        safety_audit_ok=extension_audit_ok(run.provider, run.invocation),
        integrity_detail=integrity.detail,
    )
    ended = utc_now()
    db.transition(
        run_id,
        final.state,
        final.reason or "finalized",
        failure_reason=final.reason,
        recovery_class=decision.classification,
        ended_at=ended,
        exit_code=exit_code_for_state(final.state),
        duration_seconds=run.duration_seconds,
    )


def sleep_until(stop: threading.Event, seconds: float) -> None:
    """Test helper. Production code waits via `Event.wait`."""
    stop.wait(seconds)
    time.sleep(0)
