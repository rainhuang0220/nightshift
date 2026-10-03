"""Execute one run: workspace, provider, verification, report, locks."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from nightshift.config import Config
from nightshift.containment import build_read_policy, contained_run, sandbox_available, write_profile
from nightshift.db import Database
from nightshift.extensions import (
    neutralize_project_extensions,
    project_instruction_names,
    restore_neutralized_extensions,
    run_inspect,
)
from nightshift.finalize import (
    BLOCKED_EXTENSION_SURFACE,
    check_stored_integrity,
    decide_final,
    extension_audit_ok,
)
from nightshift.gitutil import (
    GIT_BIN,
    changed_files,
    commits_since,
    is_git_repo,
    porcelain,
    run_git,
)
from nightshift.guard import write_shims
from nightshift.integrity import capture, compare, dumps
from nightshift.job import Job, resolved_repository, verification_plan
from nightshift.locks import (
    LockManager,
    group_key,
    phase_process_alive,
    process_start_token,
    repo_key,
    workspace_key,
)
from nightshift.models import (
    TERMINAL_STATES,
    Job as JobModel,
    RunRecord,
    NightshiftError,
    RunState,
    exit_code_for_state,
    utc_now,
)
from nightshift.policy import build_policy, minimal_env, scrub_text
from nightshift.priv import ensure_private_dir, write_private_text, read_private_bytes
from nightshift.providers import get_provider
from nightshift.providers.base import ProviderRequest
from nightshift.report import (
    ReportInputs,
    extract_findings,
    extract_metrics,
    provider_conclusion,
    render_report,
    summarize_stream,
)
from nightshift.runtime import (
    apply_runtime_env,
    attempt_dir,
    prepare_run_grok_home,
    prepare_runtime_dirs,
    scrub_per_run_auth,
    stage_pythonpath,
)
from nightshift.workspace import WorkspaceError, cleanup_workspace, import_instructions, inspect_source, prepare_workspace

TERMINAL_VALUES = {state.value for state in TERMINAL_STATES}
REQUIRED_FILES = (
    "metadata.json",
    "events.jsonl",
    "report.md",
)
ATTEMPT_FILES = (
    "prompt.final.md",
    "provider.stdout.log",
    "provider.stderr.log",
    "verification.log",
)
SUSPICIOUS = (
    "git push",
    "gh pr create",
    "gh pr merge",
    "sudo ",
    "kaggle competitions submit",
)


class RunBlocked(Exception):
    pass


class RunFailed(Exception):
    pass


class RunCancelled(Exception):
    pass


def execute_run(config: Config, db: Database, locks: LockManager, run_id: str, stop_event=None) -> RunRecord:
    from nightshift.locks import execution_lease
    from nightshift.models import BlockedError

    with execution_lease(config.state_dir, run_id) as owned:
        if not owned:
            raise BlockedError("another controller owns this run; inspect or recover after it exits")
        return _execute_run(config, db, locks, run_id, stop_event)


def _execute_run(
    config: Config,
    db: Database,
    locks: LockManager,
    run_id: str,
    stop_event=None,
) -> RunRecord:
    run = db.require_run(run_id)
    if run.state == RunState.QUEUED.value:
        run = db.transition(run_id, RunState.PREPARING.value, "preparing", started_at=utc_now())
    elif run.state != RunState.PREPARING.value:
        raise RunFailed(f"run {run_id} is {run.state}, not runnable")
    if not run.started_at:
        run = db.update_run(run_id, started_at=utc_now())
    _mark_phase(db, run_id, "preparing", os.getpid(), match=run_id)
    job = _job_from_run(run)
    run_dir = config.runs_dir / run_id
    _ensure_run_files(run_dir)
    current_attempt = attempt_dir(run_dir, run.attempt)
    _ensure_attempt_files(current_attempt)
    host = _host_info()
    run = db.update_run(run_id, run_dir=str(run_dir), host_info=host, report_path=str(run_dir / "report.md"))
    _emit(db, run_dir, run_id, "prepare", "run directory ready", RunState.PREPARING.value)
    dest: Path | None = None
    try:
        if job.work_order:
            from nightshift.intake import check_freshness
            from nightshift.work_order import validate
            validate(job.work_order)
            check_freshness(job.work_order)
        repo = resolved_repository(job)
        run = db.update_run(run_id, source_repo=str(repo), base_ref=job.base_ref)
        if not is_git_repo(repo):
            raise RunBlocked(f"repository is not available: {repo}")
        dest = _workspace_dest(config, run)
        keys = [group_key(job.concurrency_group), repo_key(str(repo)), workspace_key(str(dest))]
        if not locks.acquire(keys, run_id):
            raise RunBlocked(f"lock held for concurrency group {job.concurrency_group} or repository {repo}")
        try:
            snapshot = inspect_source(repo, job.base_ref)
        except WorkspaceError as exc:
            raise RunBlocked(str(exc)) from exc
        _execution_preflight(config, job, repo, dest, revision=snapshot.revision)
        before_prepare = capture(repo)
        run = db.update_run(
            run_id,
            source_revision=snapshot.revision,
            source_head=snapshot.head,
            source_porcelain=snapshot.porcelain,
            workspace_path=str(dest),
        )
        try:
            prepare_workspace(snapshot, dest, job.isolation or "clone")
        except WorkspaceError as exc:
            raise RunBlocked(str(exc)) from exc
        prepared = compare(before_prepare, capture(repo))
        allowed = {"worktrees"} if (job.isolation or "clone") == "worktree" else set()
        unexpected = [name for name in prepared.changed_categories if name not in allowed]
        if unexpected:
            detail = "SOURCE_INTEGRITY_VIOLATION during workspace prepare: " + ",".join(unexpected)
            raise RunFailed(detail)
        baseline = capture(repo)
        neutralized = neutralize_project_extensions(dest, run_dir)
        instructions = project_instruction_names(dest)
        run = db.update_run(
            run_id,
            source_porcelain_after=porcelain(repo),
            source_integrity=dumps(baseline),
        )
        job.repository = str(repo)
        policy = build_policy(job, permission_mode=config.permission_mode)
        steps = verification_plan(job)
        if job.provider == "grok" and any(step.get("shell") for step in steps) and not job.allow_legacy_shell:
            raise RunBlocked(
                "legacy shell verification is unsafe and is not the default unattended mode"
            )
        if not sandbox_available():
            raise RunBlocked("sandbox-exec is not available; refusing to run unsandboxed")
        prompt_path = current_attempt / "prompt.final.md"
        write_private_text(prompt_path, policy.preamble + job.prompt.rstrip() + "\n")
        session_id = run.session_id or _new_session()
        env = _provider_env(config, current_attempt, dest, repo, run_id)

        def poll_stop() -> str | None:
            if stop_event is not None and stop_event.is_set():
                return "interrupt"
            current_stop = db.get_run(run_id)
            if current_stop is not None and current_stop.state == RunState.CANCELLED.value:
                return "cancel"
            return None

        def on_inspect_pid(pid: int, pgid: int | None) -> None:
            _mark_phase(db, run_id, "inspect", pid, pgid=pgid, match="grok")

        _checkpoint_cancel(db, run_id)
        audit_record = {"ok": True, "violations": [], "counts": {}, "untrusted_instructions": len(instructions)}
        inspect_containment = "not-run"
        if job.provider == "grok" and os.environ.get("NIGHTSHIFT_FORBID_GROK") != "1":
            preflight = _preflight_profile(config, current_attempt, dest, repo)
            audit = run_inspect(
                env,
                dest,
                profile=preflight,
                timeout=60,
                poll_stop=poll_stop,
                on_pid=on_inspect_pid,
            )
            _mark_phase(db, run_id, "preparing", os.getpid(), match=run_id)
            if audit.ok:
                inspect_containment = "seatbelt"
            else:
                inspect_containment = "outside-seatbelt"
                audit = run_inspect(
                    env,
                    dest,
                    timeout=60,
                    poll_stop=poll_stop,
                    on_pid=on_inspect_pid,
                )
                _mark_phase(db, run_id, "preparing", os.getpid(), match=run_id)
            audit_record = audit.to_dict()
            _checkpoint_cancel(db, run_id)
            if not audit.ok:
                raise RunBlocked(BLOCKED_EXTENSION_SURFACE + ": " + ", ".join(audit.violations))
        elif job.provider == "grok":
            raise RunBlocked("refusing to launch grok because NIGHTSHIFT_FORBID_GROK=1")
        _checkpoint_cancel(db, run_id)
        invocation = _invocation(
            job,
            policy,
            session_id=session_id,
            config=config,
            isolation=job.isolation or "clone",
            neutralized=neutralized,
            audit=audit_record,
            instructions=instructions,
        )
        invocation["inspect_containment"] = inspect_containment
        invocation["attempt_dir"] = str(current_attempt)
        db.update_run(run_id, invocation=invocation)
        profile = _provider_profile(
            config,
            current_attempt,
            dest,
            source=repo,
            network=job.provider == "grok",
            write_scope=job.write_scope,
        )
        boundary = _read_boundary(profile, dest, env, repo)
        invocation["trust_boundary"] = boundary
        db.update_run(run_id, invocation=invocation)
        if boundary["source_read_isolation"] == "fail" or boundary["operator_home_read_isolation"] == "fail":
            raise RunFailed("provider seatbelt allowed a read outside the isolated workspace")
        _write_metadata(run_dir, db.require_run(run_id))
        run = db.transition(
            run_id,
            RunState.RUNNING.value,
            "provider starting",
            session_id=session_id,
            workspace_path=str(dest),
        )
        provider = get_provider(run.provider)
        request = ProviderRequest(
            run_id=run_id,
            workspace=dest,
            prompt_path=prompt_path,
            stdout_path=current_attempt / "provider.stdout.log",
            stderr_path=current_attempt / "provider.stderr.log",
            env=env,
            timeout=float(job.max_runtime_seconds),
            write_scope=job.write_scope,
            session_id=session_id,
            model=job.model,
            max_turns=config.max_turns,
            containment_profile=profile,
        )
        if run.provider == "grok":
            argv = provider.build_argv(request, policy)
            db.update_run(run_id, provider_argv=argv)

        def on_pid(pid: int, pgid: int | None) -> None:
            _mark_phase(db, run_id, "provider", pid, pgid=pgid, match=session_id)
            _emit(db, run_dir, run_id, "process", f"pid {pid}", RunState.RUNNING.value)

        result = provider.execute(
            request,
            **({"policy": policy} if run.provider == "grok" else {}),
            on_pid=on_pid,
            poll_stop=poll_stop,
            heartbeat=lambda: db.heartbeat(run_id),
        )
        # Neutralization hides executable project config from the provider.
        # Put those entries back before verification and the report, so a
        # read-only job does not keep clone deletions.
        restore_neutralized_extensions(dest, run_dir)
        # Drop the auth copy before verification. That child gets a fresh
        # runtime and can still execute code in the mutable workspace.
        # The finally block scrubs again on cancel, interrupt, and failure.
        scrub_per_run_auth(run_dir)
        current = db.require_run(run_id)
        if current.state == RunState.CANCELLED.value:
            _finish_times(db, run_id, exit_code=exit_code_for_state(RunState.CANCELLED.value))
            publish_report(config, db, run_id)
            return db.require_run(run_id)
        if result.failure_reason == "interrupt":
            db.transition(
                run_id,
                RunState.INTERRUPTED.value,
                "supervisor interrupted",
                failure_reason="supervisor interrupted",
                provider_exit_code=result.exit_code,
                exit_code=exit_code_for_state(RunState.INTERRUPTED.value),
                pid=result.pid,
            )
            _finish_times(db, run_id, exit_code=exit_code_for_state(RunState.INTERRUPTED.value))
            publish_report(config, db, run_id, extra_uncertainty=["The supervisor stopped before verification."])
            return db.require_run(run_id)
        db.update_run(
            run_id,
            provider_exit_code=result.exit_code,
            pid=result.pid if result.pid is not None else current.pid,
        )
        _checkpoint_cancel(db, run_id)
        verify_env = _verification_env(config, current_attempt, dest, repo, run_id)
        db.transition(run_id, RunState.VERIFYING.value, "verifying")

        def on_verify_pid(pid: int, pgid: int | None) -> None:
            _mark_phase(db, run_id, "verifying", pid, pgid=pgid, match=run_id)

        ver_code, ver_text = run_verification(
            steps,
            cwd=dest,
            env=verify_env,
            log_path=current_attempt / "verification.log",
            timeout=float(job.max_runtime_seconds),
            profile=_verification_profile(config, current_attempt, dest, repo),
            poll_stop=poll_stop,
            on_pid=on_verify_pid,
        )
        del ver_text
        if db.require_run(run_id).state == RunState.CANCELLED.value:
            _finish_times(db, run_id, exit_code=exit_code_for_state(RunState.CANCELLED.value))
            publish_report(config, db, run_id)
            return db.require_run(run_id)
        db.update_run(run_id, verification_ran=True, verification_exit_code=ver_code)
        try:
            db.update_run(run_id, source_porcelain_after=porcelain(repo))
        except Exception:
            pass
        current = db.require_run(run_id)
        integrity = check_stored_integrity(current.source_repo, current.source_integrity)
        decision = decide_final(
            provider_exit_code=result.exit_code,
            verification_ran=True,
            verification_exit_code=ver_code,
            workspace_exists=dest.exists(),
            integrity_ok=integrity.ok,
            safety_audit_ok=extension_audit_ok(current.provider, current.invocation),
            integrity_detail=integrity.detail,
        )
        uncertainty = []
        if not integrity.ok and integrity.detail:
            uncertainty.append(integrity.detail + "; Nightshift did not restore the source")
        if result.failure_reason and result.failure_reason not in decision.reason:
            uncertainty.append(result.failure_reason)
        if db.require_run(run_id).state == RunState.CANCELLED.value:
            _finish_times(db, run_id, exit_code=exit_code_for_state(RunState.CANCELLED.value))
            publish_report(config, db, run_id, extra_uncertainty=uncertainty)
            return db.require_run(run_id)
        ended = utc_now()
        started = current.started_at
        try:
            db.transition(
                run_id,
                decision.state,
                decision.reason or "finished",
                failure_reason=decision.reason,
                exit_code=exit_code_for_state(decision.state),
                ended_at=ended,
                duration_seconds=_duration(started, ended),
            )
        except Exception:
            latest = db.require_run(run_id)
            if latest.state != RunState.CANCELLED.value:
                raise
            publish_report(config, db, run_id, extra_uncertainty=uncertainty)
            return latest
        publish_report(config, db, run_id, extra_uncertainty=uncertainty)
        _maybe_remove_worktree(config, repo, dest, decision.state, job.isolation or "clone")
        return db.require_run(run_id)
    except RunCancelled:
        publish_report(config, db, run_id, extra_uncertainty=["Cancelled by operator."])
        return db.require_run(run_id)
    except Exception as exc:
        current = db.get_run(run_id)
        if current is not None and current.state == RunState.CANCELLED.value:
            publish_report(config, db, run_id, extra_uncertainty=["Cancelled by operator."])
            return current
        if isinstance(exc, RunBlocked):
            _settle(db, run_id, RunState.BLOCKED.value, str(exc))
            publish_report(config, db, run_id, extra_uncertainty=[str(exc)])
            return db.require_run(run_id)
        if isinstance(exc, (RunFailed, WorkspaceError, OSError)):
            _settle(db, run_id, RunState.FAILED.value, str(exc))
            publish_report(config, db, run_id, extra_uncertainty=[str(exc)])
            return db.require_run(run_id)
        try:
            _settle(db, run_id, RunState.FAILED.value, f"internal error: {exc}")
            publish_report(config, db, run_id, extra_uncertainty=[f"internal error: {exc}"])
        except Exception:
            pass
        return db.require_run(run_id)
    finally:
        try:
            if dest is not None:
                restore_neutralized_extensions(dest, run_dir)
            scrub_per_run_auth(run_dir)
        except Exception:
            pass
        if not _foreign_phase_alive(db, run_id):
            locks.release(run_id)


def run_verification_only(config: Config, db: Database, run_id: str) -> RunRecord:
    """Run snapshotted verification commands. Does not launch a provider.

    Verification does not receive a copy of the auth file. Recovery scrubs
    any copy first; this function scrubs again on the way out so a later
    environment setup cannot leave one behind.
    """
    run = db.require_run(run_id)
    job = _job_from_run(run)
    run_dir = Path(run.run_dir) if run.run_dir else config.runs_dir / run_id
    scrub_per_run_auth(run_dir)
    try:
        return _run_verification_only(config, db, run_id, run_dir, run, job)
    finally:
        scrub_per_run_auth(run_dir)


def _run_verification_only(
    config: Config,
    db: Database,
    run_id: str,
    run_dir: Path,
    run: RunRecord,
    job: JobModel,
) -> RunRecord:
    _ensure_run_files(run_dir)
    workspace = Path(run.workspace_path) if run.workspace_path else None
    if workspace is None or not workspace.exists():
        _settle(db, run_id, RunState.FAILED.value, "verification never ran and the workspace is missing")
        publish_report(config, db, run_id)
        return db.require_run(run_id)
    if run.state == RunState.RUNNING.value:
        db.transition(run_id, RunState.VERIFYING.value, "recover: verification never ran")
    source = Path(run.source_repo) if run.source_repo else workspace
    current_attempt = attempt_dir(run_dir, run.attempt)
    _ensure_attempt_files(current_attempt)
    env = _verification_env(config, current_attempt, workspace, source, run_id)
    if not sandbox_available():
        _settle(db, run_id, RunState.FAILED.value, "sandbox-exec is not available; refusing to run unsandboxed")
        publish_report(config, db, run_id)
        return db.require_run(run_id)
    ver_code, _text = run_verification(
        verification_plan(job),
        cwd=workspace,
        env=env,
        log_path=current_attempt / "verification.log",
        timeout=float(job.max_runtime_seconds or run.max_runtime_seconds or 600),
        profile=_verification_profile(config, current_attempt, workspace, source),
        poll_stop=lambda: "cancel" if db.require_run(run_id).state == RunState.CANCELLED.value else None,
        on_pid=lambda pid, pgid: _mark_phase(db, run_id, "verifying", pid, pgid=pgid, match=run_id),
    )
    db.update_run(run_id, verification_ran=True, verification_exit_code=ver_code)
    current = db.require_run(run_id)
    integrity = check_stored_integrity(current.source_repo, current.source_integrity)
    decision = decide_final(
        provider_exit_code=current.provider_exit_code,
        verification_ran=True,
        verification_exit_code=ver_code,
        workspace_exists=workspace.exists(),
        integrity_ok=integrity.ok,
        safety_audit_ok=extension_audit_ok(current.provider, current.invocation),
        integrity_detail=integrity.detail,
    )
    if current.state not in TERMINAL_VALUES:
        ended = utc_now()
        db.transition(
            run_id,
            decision.state,
            decision.reason or "verification finished during recover",
            failure_reason=decision.reason,
            exit_code=exit_code_for_state(decision.state),
            ended_at=ended,
            duration_seconds=_duration(current.started_at, ended),
            recovery_class=current.recovery_class or "verification_never_ran",
        )
    uncertainty = [integrity.detail] if not integrity.ok and integrity.detail else None
    publish_report(config, db, run_id, extra_uncertainty=uncertainty)
    return db.require_run(run_id)


def publish_report(
    config: Config,
    db: Database,
    run_id: str,
    *,
    extra_uncertainty: list[str] | None = None,
) -> RunRecord:
    del config
    run = db.require_run(run_id)
    run_dir = Path(run.run_dir) if run.run_dir else None
    if run_dir is None:
        return run
    job = _job_from_run(run)
    _ensure_run_files(run_dir)
    stdout = _read(_resolve_run_file(run_dir, run.attempt, "provider.stdout.log"))
    stderr = _read(_resolve_run_file(run_dir, run.attempt, "provider.stderr.log"))
    conclusion = provider_conclusion(stdout)
    summary = summarize_stream(stdout)
    findings = extract_findings(conclusion or summary or stdout)
    if run.state != RunState.SUCCEEDED.value and stderr.strip():
        uncertainty_note = "Provider stderr was captured in the private log and omitted from this report."
    else:
        uncertainty_note = ""
    commits: list[str] = []
    files: list[str] = []
    workspace = Path(run.workspace_path) if run.workspace_path else None
    if workspace is not None and workspace.exists() and run.source_revision:
        commits = commits_since(workspace, run.source_revision)
        files = changed_files(workspace, run.source_revision)
    found = []
    missing = []
    if workspace is not None and workspace.exists():
        for relative in job.expected_artifacts:
            if (workspace / relative).exists():
                found.append(relative)
            else:
                missing.append(relative)
    uncertainty = list(extra_uncertainty or [])
    if uncertainty_note:
        uncertainty.append(uncertainty_note)
    blob = summary + "\n" + stdout
    for needle in SUSPICIOUS:
        if needle in blob:
            uncertainty.append(
                f"Provider output mentioned {needle!r}. Command-name guards are not containment."
            )
    if missing:
        uncertainty.append("Expected artifacts missing: " + ", ".join(missing))
    results_file = _resolve_run_file(run_dir, run.attempt, "verification-results.json")
    verification_results = []
    if results_file.is_file():
        try:
            verification_results = json.loads(_read(results_file))
        except ValueError:
            uncertainty.append("Verification journal is incomplete; inspect private logs.")
    executed = [r["display"] for r in verification_results if r.get("exit_code") is not None]
    if not results_file.is_file() and run.verification_ran:
        executed = list(job.verification)  # Legacy attempts have no per-check journal.
    boundary = (run.invocation or {}).get("trust_boundary") or {}
    info = ReportInputs(
        run_id=run.run_id,
        job_id=run.job_id,
        description=run.description or job.description,
        job_type=run.job_type or job.type,
        state=run.state,
        provider=run.provider,
        attempt=run.attempt,
        max_attempts=run.max_attempts,
        source_repo=run.source_repo,
        source_revision=run.source_revision,
        source_head=run.source_head,
        base_ref=run.base_ref or job.base_ref,
        workspace=run.workspace_path,
        session_id=run.session_id,
        started_at=run.started_at,
        ended_at=run.ended_at,
        duration_seconds=run.duration_seconds,
        exit_code=run.exit_code,
        provider_exit_code=run.provider_exit_code,
        verification_exit_code=run.verification_exit_code,
        failure_reason=run.failure_reason,
        findings=findings,
        commits=commits,
        files_changed=files,
        verification_commands=executed,
        verification_results=verification_results,
        verification_output=_read(_resolve_run_file(run_dir, run.attempt, "verification.log")),
        success_criteria=list(job.success_criteria),
        measurements=extract_metrics(stdout),
        host_info={str(k): str(v) for k, v in (run.host_info or {}).items()},
        uncertainty=uncertainty,
        artifacts_expected=list(job.expected_artifacts),
        artifacts_found=found,
        human_review_required=True,
        recovery_class=run.recovery_class,
        isolation=str((run.invocation or {}).get("isolation") or job.isolation or "clone"),
        import_note=import_instructions(str((run.invocation or {}).get("isolation") or job.isolation or "clone")),
        grok_home_scope=str(boundary.get("grok_home_scope") or "per-run"),
        source_read_isolation=str(boundary.get("source_read_isolation") or "not-probed"),
        operator_home_read_isolation=str(boundary.get("operator_home_read_isolation") or "not-probed"),
        source_integrity=_source_integrity_label(run),
        extension_audit=_extension_audit_label(run),
        network_containment=str(boundary.get("network_containment") or "accepted limitation"),
        inspect_containment=str((run.invocation or {}).get("inspect_containment") or "not-probed"),
        conclusion=conclusion,
    )
    from nightshift.policy import scrub_data
    text = scrub_text(render_report(info))
    result = {"schema": "nightshift.result", "version": 1, **asdict(info),
              "work_order": job.work_order}
    write_private_text(run_dir / "result.json", json.dumps(scrub_data(result), sort_keys=True, indent=2) + "\n")
    report_path = run_dir / "report.md"
    write_private_text(report_path, text)
    db.update_run(run_id, report_path=str(report_path), artifact_paths=found, run_dir=str(run_dir))
    _write_metadata(run_dir, db.require_run(run_id))
    _emit(db, run_dir, run_id, "report", f"report {run.state}", run.state)
    return db.require_run(run_id)


def run_verification(
    steps: list[dict],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    timeout: float,
    profile: Path | None = None,
    poll_stop=None,
    on_pid=None,
) -> tuple[int, str]:
    """Run structured argv steps inside the workspace seatbelt.

    Legacy shell steps are labeled and still confined. They are not the
    default unattended mode for Grok jobs.
    """
    ensure_private_dir(log_path.parent)
    if not steps:
        write_private_text(log_path, "no verification commands\n")
        return 0, "no verification commands\n"
    if profile is None:
        return 127, "verification refused without a seatbelt profile\n"
    rc = 0
    chunks: list[str] = []
    parts: list[str] = []
    results: list[dict] = []
    journal = log_path.with_name("verification-results.json")
    write_private_text(journal, "[]\n")
    for index, step in enumerate(steps):
        argv = [str(item) for item in step.get("argv") or []]
        display = str(step.get("display") or " ".join(argv))
        step_timeout = float(step.get("timeout_seconds") or timeout)
        label = "legacy shell verification; confined by seatbelt" if step.get("shell") else "argv verification"
        started = time.monotonic()
        record = {"index": index, "argv": argv, "display": display, "started_at": utc_now(),
                  "ended_at": None, "exit_code": None, "duration_seconds": 0.0}
        results.append(record)
        from nightshift.policy import scrub_data
        write_private_text(journal, json.dumps(scrub_data(results), sort_keys=True) + "\n")
        code, output = contained_run(
            argv,
            cwd=cwd,
            env=env,
            profile=profile,
            timeout=step_timeout,
            poll_stop=poll_stop,
            on_pid=on_pid,
        )
        record.update(exit_code=code, duration_seconds=time.monotonic() - started, ended_at=utc_now())
        write_private_text(journal, json.dumps(scrub_data(results), sort_keys=True) + "\n")
        block = f"$ {display}\n# {label}\n{output}exit {code}\n"
        parts.append(block)
        chunks.append(output)
        if code != 0 and rc == 0:
            rc = code
    write_private_text(log_path, scrub_text("".join(parts))[:8 * 1024 * 1024])
    return rc, "".join(chunks)[:8 * 1024 * 1024]


def _settle(db: Database, run_id: str, state: str, reason: str) -> RunRecord:
    current = db.require_run(run_id)
    if current.state in TERMINAL_VALUES:
        return db.update_run(run_id, failure_reason=current.failure_reason or reason)
    target = state
    if current.state == RunState.RUNNING.value and target == RunState.BLOCKED.value:
        target = RunState.FAILED.value
    if current.state == RunState.VERIFYING.value and target in {RunState.BLOCKED.value, RunState.PREPARING.value}:
        target = RunState.FAILED.value
    if current.state == RunState.QUEUED.value:
        db.transition(run_id, RunState.PREPARING.value, "preparing")
        current = db.require_run(run_id)
    ended = utc_now()
    return db.transition(
        run_id,
        target,
        reason,
        failure_reason=reason,
        ended_at=ended,
        exit_code=exit_code_for_state(target),
        duration_seconds=_duration(current.started_at, ended),
    )


def _finish_times(db: Database, run_id: str, *, exit_code: int) -> None:
    run = db.require_run(run_id)
    ended = run.ended_at or utc_now()
    db.update_run(
        run_id,
        ended_at=ended,
        exit_code=run.exit_code if run.exit_code is not None else exit_code,
        duration_seconds=run.duration_seconds if run.duration_seconds is not None else _duration(run.started_at, ended),
    )


def _job_from_run(run: RunRecord) -> JobModel:
    if run.job_snapshot:
        return Job.from_dict(json.loads(run.job_snapshot))
    from nightshift.job import load_job

    return load_job(Path(run.job_path))


def _workspace_dest(config: Config, run: RunRecord) -> Path:
    name = run.run_id if run.attempt <= 1 else f"{run.run_id}-a{run.attempt}"
    return (config.worktrees_dir / name).resolve()


def _ensure_run_files(run_dir: Path) -> None:
    ensure_private_dir(run_dir)
    for name in REQUIRED_FILES:
        path = run_dir / name
        if path.is_symlink():
            raise RunFailed(f"refusing to follow a non-regular control file: {name}")
        if not path.exists():
            write_private_text(path, "{}\n" if name == "metadata.json" else "")


def _ensure_attempt_files(path: Path) -> None:
    ensure_private_dir(path)
    for name in ATTEMPT_FILES:
        target = path / name
        if target.is_symlink():
            raise RunFailed(f"refusing to follow a non-regular control file: {name}")
        if not target.exists():
            write_private_text(target, "")


def _resolve_run_file(run_dir: Path, attempt: int, name: str) -> Path:
    current = attempt_dir(run_dir, attempt) / name
    if current.exists():
        return current
    legacy = run_dir / name
    if legacy.exists():
        return legacy
    return current


def _execution_preflight(config: Config, job: Job, repo: Path, dest: Path, *, revision: str) -> None:
    """Check prerequisites before allocating an isolated checkout."""
    from nightshift.intake import reject_control_plane
    from nightshift.work_order import WorkOrderError
    try:
        reject_control_plane(repo)
    except WorkOrderError as exc:
        raise RunBlocked(str(exc)) from exc
    source = repo.resolve()
    for control in (config.state_dir, config.runs_dir, config.worktrees_dir, dest):
        path = control.resolve()
        if source == path or source in path.parents or path in source.parents:
            raise RunBlocked('source repository and control/workspace paths must not overlap')
    if dest.exists() or dest.is_symlink():
        raise RunBlocked(f'workspace destination already exists: {dest}')
    if not sandbox_available():
        raise RunBlocked('sandbox-exec is not available; refusing to run unsandboxed')
    disk_root = config.worktrees_dir
    while not disk_root.exists():
        disk_root = disk_root.parent
    if shutil.disk_usage(disk_root).free < 64 * 1024 * 1024:
        raise RunBlocked('less than 64 MiB free for an isolated workspace')
    if job.provider == 'grok' and shutil.which('grok') is None:
        raise RunBlocked('grok CLI is not available')
    for step in verification_plan(job):
        argv = step.get('argv')
        if not argv:
            continue
        executable = argv[0]
        if '/' not in executable:
            available = shutil.which(executable) is not None
        else:
            path = Path(executable)
            if path.is_absolute():
                available = path.is_file() and os.access(path, os.X_OK)
            elif '..' in path.parts:
                available = False
            else:
                entry = run_git(repo, ['ls-tree', revision, '--', path.as_posix()], check=False)
                available = entry.returncode == 0 and entry.stdout.startswith('100755 blob ')
        if not available:
            raise RunBlocked(f'verification executable is unavailable: {executable}')


def _mark_phase(
    db: Database,
    run_id: str,
    phase: str,
    pid: int,
    *,
    pgid: int | None = None,
    match: str = "",
) -> None:
    identity = process_start_token(pid)
    db.heartbeat(run_id)
    db.update_run(
        run_id,
        pid=pid,
        process_meta={
            "phase": phase,
            "pid": pid,
            "pgid": pgid,
            "match": match,
            "identity": identity,
            "controller_pid": os.getpid(),
            "controller_identity": process_start_token(os.getpid()),
        },
    )


def _foreign_phase_alive(db: Database, run_id: str) -> bool:
    """True when a provider, inspect, or verification child is still that child."""
    run = db.get_run(run_id)
    if run is None:
        return False
    meta = run.process_meta if isinstance(run.process_meta, dict) else {}
    phase = str(meta.get("phase") or "")
    if phase not in {"provider", "verifying", "inspect"}:
        return False
    try:
        pid = int(meta.get("pid"))
    except (TypeError, ValueError):
        return False
    if pid == os.getpid():
        return False
    return phase_process_alive(meta, fallback_pid=run.pid)


def _checkpoint_cancel(db: Database, run_id: str) -> None:
    current = db.get_run(run_id)
    if current is not None and current.state == RunState.CANCELLED.value:
        raise RunCancelled()


def _emit(db: Database, run_dir: Path, run_id: str, kind: str, message: str, state: str | None) -> None:
    safe = scrub_text(message)
    db.add_event(run_id, kind, safe, state)
    path = run_dir / "events.jsonl"
    line = json.dumps({"ts": utc_now(), "kind": kind, "state": state, "message": safe})
    from nightshift.priv import open_private

    with open_private(path, append=True) as handle:
        handle.write(line + "\n")
        handle.flush()


def _write_metadata(run_dir: Path, run: RunRecord) -> None:
    payload = {
        "run_id": run.run_id,
        "job_id": run.job_id,
        "state": run.state,
        "provider": run.provider,
        "session_id": run.session_id,
        "source_repo": run.source_repo,
        "source_revision": run.source_revision,
        "source_head": run.source_head,
        "base_ref": run.base_ref,
        "workspace_path": run.workspace_path,
        "attempt": run.attempt,
        "max_attempts": run.max_attempts,
        "pid": run.pid,
        "exit_code": run.exit_code,
        "provider_exit_code": run.provider_exit_code,
        "verification_exit_code": run.verification_exit_code,
        "failure_reason": run.failure_reason,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "duration_seconds": run.duration_seconds,
        "host": run.host_info,
        "recovery_class": run.recovery_class,
        "report_path": run.report_path,
        "invocation": run.invocation or {},
    }
    write_private_text(
        run_dir / "metadata.json",
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def _runtime_env(
    config: Config,
    root: Path,
    workspace: Path,
    source: Path,
    run_id: str,
    *,
    copy_auth: bool,
) -> dict[str, str]:
    del run_id
    bin_dir = root / "bin"
    hooks = root / "empty-hooks"
    gitconfig = root / "empty-gitconfig"
    write_private_text(gitconfig, "")
    grok_home = prepare_run_grok_home(root, config.auth_store(), copy_auth=copy_auth)
    runtime_home, private_tmp = prepare_runtime_dirs(root)
    staged = stage_pythonpath(root)
    real = {
        "git": shutil.which(GIT_BIN) or shutil.which("git") or "",
        "gh": shutil.which("gh") or "",
        "sudo": shutil.which("sudo") or "",
        "kaggle": shutil.which("kaggle") or "",
        "npm": shutil.which("npm") or "",
        "twine": shutil.which("twine") or "",
    }
    write_shims(
        bin_dir,
        workspace=workspace,
        source=source,
        hooks_path=hooks,
        pythonpath=str(staged),
    )
    parent = dict(os.environ)
    path = str(bin_dir) + os.pathsep + parent.get("PATH", "")
    extra = {
        "PATH": path,
        "PYTHONPATH": str(staged),
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "NIGHTSHIFT_GUARD_WORKSPACE": str(workspace),
        "NIGHTSHIFT_GUARD_SOURCE": str(source),
        "NIGHTSHIFT_GUARD_HOOKS": str(hooks),
    }
    for tool, binary in real.items():
        if binary:
            extra[f"NIGHTSHIFT_REAL_{tool.upper()}"] = binary
    env = minimal_env(parent, extra=extra)
    return apply_runtime_env(env, runtime_home=runtime_home, grok_home=grok_home, private_tmp=private_tmp)


def _provider_env(config: Config, attempt: Path, workspace: Path, source: Path, run_id: str) -> dict[str, str]:
    """Provider runtime. Its control files are not the verification launcher."""
    return _runtime_env(config, attempt, workspace, source, run_id, copy_auth=True)


def _verification_env(config: Config, attempt: Path, workspace: Path, source: Path, run_id: str) -> dict[str, str]:
    """Fresh verification runtime built after the provider exits.

    The directory is created here, so the provider profile never included it.
    No auth file is copied. A passing check means the declared command passed
    inside the mutable workspace. It does not prove the model could not edit
    that command's inputs.
    """
    root = ensure_private_dir(attempt / "verify-runtime")
    return _runtime_env(config, root, workspace, source, run_id, copy_auth=False)


def provider_write_roots(workspace: Path, attempt: Path, *, write_scope: str) -> list[Path]:
    """Directories the provider may write. The run's control files are absent."""
    roots = [
        attempt / "grok-home",
        attempt / "runtime-home",
        attempt / "tmp",
    ]
    if write_scope != "none":
        roots.insert(0, workspace)
    return roots


def _provider_profile(
    config: Config,
    attempt: Path,
    workspace: Path,
    *,
    source: Path,
    network: bool,
    write_scope: str,
) -> Path:
    return write_profile(
        attempt / "provider.sb",
        writable=provider_write_roots(workspace, attempt, write_scope=write_scope),
        network=network,
        read_policy=_read_policy(config, attempt, workspace, source),
    )


def _preflight_profile(config: Config, attempt: Path, workspace: Path, source: Path) -> Path:
    """Inspect profile. Network is denied. Writable roots match the provider runtime."""
    return write_profile(
        attempt / "inspect.sb",
        writable=provider_write_roots(workspace, attempt, write_scope="none"),
        network=False,
        read_policy=_read_policy(config, attempt, workspace, source),
    )


def _verification_profile(config: Config, attempt: Path, workspace: Path, source: Path) -> Path:
    verify_root = attempt / "verify-runtime"
    return write_profile(
        attempt / "verification.sb",
        writable=[workspace, verify_root / "runtime-home", verify_root / "tmp"],
        network=False,
        read_policy=_read_policy(config, verify_root, workspace, source, extra=[workspace, verify_root]),
    )


def _source_integrity_label(run: RunRecord) -> str:
    if not run.source_repo or not run.source_integrity:
        if "SOURCE_INTEGRITY_VIOLATION" in (run.failure_reason or ""):
            return "fail"
        return "not-probed"
    from nightshift.finalize import check_stored_integrity

    return "pass" if check_stored_integrity(run.source_repo, run.source_integrity).ok else "fail"


def _extension_audit_label(run: RunRecord) -> str:
    from nightshift.finalize import extension_audit_ok

    return "pass" if extension_audit_ok(run.provider, run.invocation) else "fail"


def _read_boundary(profile: Path, workspace: Path, env: dict[str, str], source: Path) -> dict[str, str]:
    """Enforcement evidence for the provider profile. Does not ask the model."""
    return {
        "grok_home_scope": "per-run",
        "source_read_isolation": _source_read_result(profile, workspace, env, source),
        "operator_home_read_isolation": _operator_home_read_result(profile, workspace, env),
        "network_containment": "accepted limitation",
    }


def _source_read_result(profile: Path, workspace: Path, env: dict[str, str], source: Path) -> str:
    target = source / ".git" / "HEAD"
    if not target.is_file():
        return "not-probed"
    try:
        needle = target.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "not-probed"
    if not needle:
        return "not-probed"
    if _readers_saw(profile, workspace, env, target, needle):
        return "fail"
    return "pass"


def _operator_home_read_result(profile: Path, workspace: Path, env: dict[str, str]) -> str:
    import uuid

    canary = Path.home() / f"nightshift-read-canary-{uuid.uuid4().hex}"
    needle = f"synthetic-operator-home-canary-{canary.name}"
    try:
        ensure_private_dir(canary)
        target = canary / "outside-private.txt"
        write_private_text(target, needle + "\n")
        if _readers_saw(profile, workspace, env, target, needle):
            return "fail"
        return "pass"
    except OSError:
        return "not-probed"
    finally:
        shutil.rmtree(canary, ignore_errors=True)


def _readers_saw(profile: Path, workspace: Path, env: dict[str, str], target: Path, needle: str) -> bool:
    quoted = str(target)
    commands = [
        ["cat", quoted],
        ["/bin/cat", quoted],
        ["/usr/bin/python3", "-c", f"print(open({quoted!r}).read())"],
        ["/bin/sh", "-c", f"cat {quoted}"],
    ]
    child = dict(env)
    child.setdefault("PATH", "/usr/bin:/bin")
    for argv in commands:
        try:
            _code, output = contained_run(argv, cwd=workspace, env=child, profile=profile, timeout=20)
        except OSError:
            continue
        if needle in output:
            return True
    return False


def _read_policy(config: Config, root: Path, workspace: Path, source: Path, extra: list[Path] | None = None):
    runtime_roots = [
        workspace,
        root,
        root / "runtime-home",
        root / "grok-home",
        root / "tmp",
        root / "pythonpath",
        root / "bin",
    ]
    for item in extra or []:
        runtime_roots.append(item)
    return build_read_policy(
        runtime_roots=runtime_roots,
        source_root=source,
        explicit_read_roots=list(config.explicit_read_roots),
    )


def _invocation(
    job: JobModel,
    policy,
    *,
    session_id: str,
    config: Config,
    isolation: str,
    neutralized: list[str],
    audit: dict,
    instructions: list[str],
) -> dict:
    version = ""
    version_containment = "not-run"
    if job.provider == "grok" and os.environ.get("NIGHTSHIFT_FORBID_GROK") != "1":
        version, version_containment = _grok_version()
    return {
        "binary_version": version,
        "binary_version_containment": version_containment,
        "model": job.model or "",
        "session_id": session_id,
        "permission_mode": policy.permission_mode,
        "sandbox_profile": policy.sandbox_profile,
        "grok_sandbox_flag": "omitted",
        "grok_sandbox_reason": "nested sandbox_init returns EPERM inside the seatbelt",
        "seatbelt": "deny-default",
        "max_turns": config.max_turns,
        "memory_disabled": True,
        "subagents_disabled": True,
        "web_search_disabled": bool(policy.disable_web_search),
        "deny_count": len(policy.deny),
        "allow_count": len(policy.allow),
        "grok_home": "per-run",
        "grok_home_scope": "per-run",
        "isolation": isolation,
        "neutralized": list(neutralized),
        "untrusted_instructions": list(instructions),
        "extension_audit": audit,
        "legacy_shell": bool(job.allow_legacy_shell),
    }


def _grok_version() -> tuple[str, str]:
    import shutil

    from nightshift.doctor import read_grok_version

    binary = shutil.which("grok") or "grok"
    return read_grok_version(binary)


def _maybe_remove_worktree(config: Config, source: Path, dest: Path, state: str, isolation: str) -> None:
    if not config.destroy_failed_worktrees:
        return
    if state not in {RunState.FAILED.value, RunState.CANCELLED.value}:
        return
    if not dest.exists():
        return
    cleanup_workspace(isolation, source, dest)


def _new_session() -> str:
    from nightshift.models import new_session_id

    return new_session_id()


def _host_info() -> dict[str, str]:
    import platform

    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "arch": platform.machine(),
        "python": platform.python_version(),
    }


def _duration(started: str, ended: str) -> float | None:
    if not started or not ended:
        return None
    return max(0.0, (_parse_utc(ended) - _parse_utc(started)).total_seconds())


def _parse_utc(value: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unrecognized timestamp {value}")


def _read(path: Path) -> str:
    try:
        raw = read_private_bytes(path, max_bytes=8 * 1024 * 1024 + 1)
    except FileNotFoundError:
        return ""
    except (OSError, NightshiftError):
        return '[nightshift: report input is non-regular or unavailable]'
    suffix = "\n[nightshift: report input truncated]\n" if len(raw) > 8 * 1024 * 1024 else ""
    return raw[:8 * 1024 * 1024].decode("utf-8", "replace") + suffix
