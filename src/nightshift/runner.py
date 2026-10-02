"""Execute one run: workspace, provider, verification, report, locks."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from nightshift.config import Config
from nightshift.containment import contained_run, operator_read_denies, sandbox_available, write_profile
from nightshift.db import Database
from nightshift.extensions import neutralize_project_extensions, project_instruction_names, run_inspect
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
)
from nightshift.guard import write_shims
from nightshift.integrity import capture, compare, dumps
from nightshift.job import Job, resolved_repository, verification_plan
from nightshift.locks import LockManager, group_key, repo_key, workspace_key
from nightshift.models import (
    TERMINAL_STATES,
    Job as JobModel,
    RunRecord,
    RunState,
    exit_code_for_state,
    utc_now,
)
from nightshift.policy import build_policy, minimal_env, package_pythonpath, scrub_text
from nightshift.priv import ensure_private_dir, write_private_text
from nightshift.providers import get_provider
from nightshift.providers.base import ProviderRequest
from nightshift.report import ReportInputs, extract_findings, extract_metrics, render_report, summarize_stream
from nightshift.runtime import apply_runtime_env, prepare_runtime_dirs
from nightshift.workspace import WorkspaceError, cleanup_workspace, import_instructions, inspect_source, prepare_workspace

TERMINAL_VALUES = {state.value for state in TERMINAL_STATES}
REQUIRED_FILES = (
    "metadata.json",
    "prompt.final.md",
    "provider.stdout.log",
    "provider.stderr.log",
    "events.jsonl",
    "verification.log",
    "report.md",
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


def execute_run(
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
    job = _job_from_run(run)
    run_dir = config.runs_dir / run_id
    _ensure_run_files(run_dir)
    host = _host_info()
    run = db.update_run(run_id, run_dir=str(run_dir), host_info=host, report_path=str(run_dir / "report.md"))
    _emit(db, run_dir, run_id, "prepare", "run directory ready", RunState.PREPARING.value)
    try:
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
        prompt_path = run_dir / "prompt.final.md"
        write_private_text(prompt_path, policy.preamble + job.prompt.rstrip() + "\n")
        session_id = run.session_id or _new_session()
        env = _child_env(config, run_dir, dest, repo, run_id)
        audit_record = {"ok": True, "violations": [], "counts": {}, "untrusted_instructions": len(instructions)}
        if job.provider == "grok" and os.environ.get("NIGHTSHIFT_FORBID_GROK") != "1":
            audit = run_inspect(env, dest)
            audit_record = audit.to_dict()
            if not audit.ok:
                raise RunBlocked(BLOCKED_EXTENSION_SURFACE + ": " + ", ".join(audit.violations))
        elif job.provider == "grok":
            raise RunBlocked("refusing to launch grok because NIGHTSHIFT_FORBID_GROK=1")
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
        db.update_run(run_id, invocation=invocation)
        profile = _provider_profile(config, run_dir, dest, network=job.provider == "grok")
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
            stdout_path=run_dir / "provider.stdout.log",
            stderr_path=run_dir / "provider.stderr.log",
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
            meta = {"pid": pid, "pgid": pgid, "match": session_id}
            db.update_run(run_id, pid=pid, process_meta=meta)
            _emit(db, run_dir, run_id, "process", f"pid {pid}", RunState.RUNNING.value)

        def poll_stop() -> str | None:
            if stop_event is not None and stop_event.is_set():
                return "interrupt"
            current = db.get_run(run_id)
            if current is not None and current.state == RunState.CANCELLED.value:
                return "cancel"
            return None

        result = provider.execute(
            request,
            **({"policy": policy} if run.provider == "grok" else {}),
            on_pid=on_pid,
            poll_stop=poll_stop,
            heartbeat=lambda: db.heartbeat(run_id),
        )
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
        db.transition(run_id, RunState.VERIFYING.value, "verifying")
        ver_code, ver_text = run_verification(
            steps,
            cwd=dest,
            env=env,
            log_path=run_dir / "verification.log",
            timeout=float(job.max_runtime_seconds),
            profile=_verification_profile(config, run_dir, dest),
        )
        del ver_text
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
        ended = utc_now()
        started = current.started_at
        db.transition(
            run_id,
            decision.state,
            decision.reason or "finished",
            failure_reason=decision.reason,
            exit_code=exit_code_for_state(decision.state),
            ended_at=ended,
            duration_seconds=_duration(started, ended),
        )
        publish_report(config, db, run_id, extra_uncertainty=uncertainty)
        _maybe_remove_worktree(config, repo, dest, decision.state, job.isolation or "clone")
        return db.require_run(run_id)
    except RunBlocked as exc:
        _settle(db, run_id, RunState.BLOCKED.value, str(exc))
        publish_report(config, db, run_id, extra_uncertainty=[str(exc)])
        return db.require_run(run_id)
    except (RunFailed, WorkspaceError, OSError) as exc:
        _settle(db, run_id, RunState.FAILED.value, str(exc))
        publish_report(config, db, run_id, extra_uncertainty=[str(exc)])
        return db.require_run(run_id)
    except Exception as exc:
        try:
            _settle(db, run_id, RunState.FAILED.value, f"internal error: {exc}")
            publish_report(config, db, run_id, extra_uncertainty=[f"internal error: {exc}"])
        except Exception:
            pass
        return db.require_run(run_id)
    finally:
        locks.release(run_id)


def run_verification_only(config: Config, db: Database, run_id: str) -> RunRecord:
    """Run snapshotted verification commands. Does not launch a provider."""
    run = db.require_run(run_id)
    job = _job_from_run(run)
    run_dir = Path(run.run_dir) if run.run_dir else config.runs_dir / run_id
    _ensure_run_files(run_dir)
    workspace = Path(run.workspace_path) if run.workspace_path else None
    if workspace is None or not workspace.exists():
        _settle(db, run_id, RunState.FAILED.value, "verification never ran and the workspace is missing")
        publish_report(config, db, run_id)
        return db.require_run(run_id)
    if run.state == RunState.RUNNING.value:
        db.transition(run_id, RunState.VERIFYING.value, "recover: verification never ran")
    source = Path(run.source_repo) if run.source_repo else workspace
    env = _child_env(config, run_dir, workspace, source, run_id)
    if not sandbox_available():
        _settle(db, run_id, RunState.FAILED.value, "sandbox-exec is not available; refusing to run unsandboxed")
        publish_report(config, db, run_id)
        return db.require_run(run_id)
    ver_code, _text = run_verification(
        verification_plan(job),
        cwd=workspace,
        env=env,
        log_path=run_dir / "verification.log",
        timeout=float(job.max_runtime_seconds or run.max_runtime_seconds or 600),
        profile=_verification_profile(config, run_dir, workspace),
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
    job = _job_from_run(run)
    run_dir = Path(run.run_dir) if run.run_dir else None
    if run_dir is None:
        return run
    _ensure_run_files(run_dir)
    stdout = _read(run_dir / "provider.stdout.log")
    stderr = _read(run_dir / "provider.stderr.log")
    summary = summarize_stream(stdout)
    findings = extract_findings(summary or stdout)
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
        verification_commands=list(job.verification),
        verification_output=_read(run_dir / "verification.log"),
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
    )
    text = render_report(info)
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
    for step in steps:
        argv = [str(item) for item in step.get("argv") or []]
        display = str(step.get("display") or " ".join(argv))
        step_timeout = float(step.get("timeout_seconds") or timeout)
        label = "legacy shell verification; confined by seatbelt" if step.get("shell") else "argv verification"
        code, output = contained_run(
            argv,
            cwd=cwd,
            env=env,
            profile=profile,
            timeout=step_timeout,
        )
        block = f"$ {display}\n# {label}\n{output}exit {code}\n"
        parts.append(block)
        chunks.append(output)
        if code != 0 and rc == 0:
            rc = code
    write_private_text(log_path, "".join(parts))
    return rc, "".join(chunks)


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
        if not path.exists():
            write_private_text(path, "{}\n" if name == "metadata.json" else "")


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


def _child_env(config: Config, run_dir: Path, workspace: Path, source: Path, run_id: str) -> dict[str, str]:
    del run_id
    bin_dir = run_dir / "bin"
    hooks = run_dir / "empty-hooks"
    gitconfig = run_dir / "empty-gitconfig"
    write_private_text(gitconfig, "")
    profile = config.grok_profile()
    runtime_home, private_tmp = prepare_runtime_dirs(run_dir, profile)
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
        pythonpath=package_pythonpath(),
    )
    parent = dict(os.environ)
    path = str(bin_dir) + os.pathsep + parent.get("PATH", "")
    pythonpath = package_pythonpath()
    if parent.get("PYTHONPATH"):
        pythonpath = pythonpath + os.pathsep + parent["PYTHONPATH"]
    extra = {
        "PATH": path,
        "PYTHONPATH": pythonpath,
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
    return apply_runtime_env(env, runtime_home=runtime_home, grok_home=profile, private_tmp=private_tmp)


def _provider_profile(config: Config, run_dir: Path, workspace: Path, *, network: bool) -> Path:
    writable = [workspace, run_dir, config.grok_profile()]
    return write_profile(
        run_dir / "provider.sb",
        writable=writable,
        network=network,
        read_deny=operator_read_denies(Path.home()),
    )


def _verification_profile(config: Config, run_dir: Path, workspace: Path) -> Path:
    del config
    return write_profile(
        run_dir / "verification.sb",
        writable=[workspace, run_dir],
        network=False,
        read_deny=operator_read_denies(Path.home()),
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
    if job.provider == "grok" and os.environ.get("NIGHTSHIFT_FORBID_GROK") != "1":
        version = _grok_version()
    return {
        "binary_version": version,
        "model": job.model or "",
        "session_id": session_id,
        "permission_mode": policy.permission_mode,
        "sandbox_profile": policy.sandbox_profile,
        "seatbelt": "deny-default",
        "max_turns": config.max_turns,
        "memory_disabled": True,
        "subagents_disabled": True,
        "web_search_disabled": bool(policy.disable_web_search),
        "deny_count": len(policy.deny),
        "allow_count": len(policy.allow),
        "grok_home": "state/grok-profile",
        "isolation": isolation,
        "neutralized": list(neutralized),
        "untrusted_instructions": list(instructions),
        "extension_audit": audit,
        "legacy_shell": bool(job.allow_legacy_shell),
    }


def _grok_version() -> str:
    import subprocess

    try:
        proc = subprocess.run(["grok", "--version"], check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or proc.stderr or "").splitlines()[0] if proc.returncode == 0 else ""


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
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")
