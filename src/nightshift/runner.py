"""Execute one run: workspace, provider, verification, report, locks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from nightshift.config import Config
from nightshift.db import Database
from nightshift.gitutil import (
    GIT_BIN,
    changed_files,
    commits_since,
    is_git_repo,
    porcelain,
    rev_parse,
)
from nightshift.guard import write_shims
from nightshift.job import Job, resolved_repository
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
from nightshift.providers import get_provider
from nightshift.providers.base import ProviderRequest
from nightshift.report import ReportInputs, extract_findings, extract_metrics, render_report
from nightshift.workspace import WorkspaceError, create_worktree, inspect_source

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
        run = db.update_run(
            run_id,
            source_revision=snapshot.revision,
            source_head=snapshot.head,
            source_porcelain=snapshot.porcelain,
            workspace_path=str(dest),
        )
        create_worktree(snapshot, dest)
        checked = porcelain(repo)
        run = db.update_run(run_id, source_porcelain_after=checked)
        job.repository = str(repo)
        policy = build_policy(job, permission_mode=config.permission_mode)
        prompt_path = run_dir / "prompt.final.md"
        prompt_path.write_text(policy.preamble + job.prompt.rstrip() + "\n", encoding="utf-8")
        session_id = run.session_id or _new_session()
        env = _child_env(config, run_dir, dest, repo, run_id)
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
            job.verification,
            cwd=dest,
            env=env,
            log_path=run_dir / "verification.log",
            timeout=float(job.max_runtime_seconds),
        )
        db.update_run(run_id, verification_ran=True, verification_exit_code=ver_code)
        source_changed, source_detail = _source_changed(repo, snapshot.head, snapshot.porcelain)
        if source_changed:
            db.update_run(run_id, source_porcelain_after=porcelain(repo))
        reason = ""
        final = RunState.SUCCEEDED.value
        if source_changed:
            final = RunState.FAILED.value
            reason = "source repository changed during the run; Nightshift did not try to revert it"
        elif result.exit_code != 0:
            final = RunState.FAILED.value
            reason = result.failure_reason or f"provider exited {result.exit_code}"
        elif ver_code != 0:
            final = RunState.FAILED.value
            reason = f"verification failed with exit {ver_code}"
        uncertainty = []
        if source_detail and source_changed:
            uncertainty.append(source_detail)
        if result.failure_reason and result.failure_reason not in reason:
            uncertainty.append(result.failure_reason)
        code = exit_code_for_state(final)
        ended = utc_now()
        started = db.require_run(run_id).started_at
        db.transition(
            run_id,
            final,
            reason or "finished",
            failure_reason=reason,
            exit_code=code,
            ended_at=ended,
            duration_seconds=_duration(started, ended),
        )
        publish_report(config, db, run_id, extra_uncertainty=uncertainty)
        _maybe_remove_worktree(config, repo, dest, final)
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
    ver_code, _text = run_verification(
        job.verification,
        cwd=workspace,
        env=env,
        log_path=run_dir / "verification.log",
        timeout=float(job.max_runtime_seconds or run.max_runtime_seconds or 600),
    )
    final = RunState.SUCCEEDED.value if ver_code == 0 and (run.provider_exit_code in (None, 0)) else RunState.FAILED.value
    if run.provider_exit_code not in (None, 0):
        final = RunState.FAILED.value
    reason = "" if final == RunState.SUCCEEDED.value else f"verification exit {ver_code}"
    if run.provider_exit_code not in (None, 0) and not reason:
        reason = f"provider exited {run.provider_exit_code}"
    ended = utc_now()
    db.update_run(run_id, verification_ran=True, verification_exit_code=ver_code)
    current = db.require_run(run_id)
    if current.state not in TERMINAL_VALUES:
        db.transition(
            run_id,
            final,
            "verification finished during recover",
            failure_reason=reason,
            exit_code=exit_code_for_state(final),
            ended_at=ended,
            duration_seconds=_duration(current.started_at, ended),
            recovery_class=current.recovery_class or "verification_never_ran",
        )
    publish_report(config, db, run_id)
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
    findings = extract_findings(stdout)
    if run.state != RunState.SUCCEEDED.value and stderr.strip():
        extra = stderr.strip()[-1000:]
        findings = f"{findings}\n{extra}".strip() if findings else extra
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
    blob = stdout + "\n" + stderr
    for needle in SUSPICIOUS:
        if needle in blob:
            uncertainty.append(
                f"Provider output mentioned {needle!r}. The PATH shim may still have blocked it."
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
    )
    text = render_report(info)
    report_path = run_dir / "report.md"
    report_path.write_text(text, encoding="utf-8")
    db.update_run(run_id, report_path=str(report_path), artifact_paths=found, run_dir=str(run_dir))
    _write_metadata(run_dir, db.require_run(run_id))
    _emit(db, run_dir, run_id, "report", f"report {run.state}", run.state)
    return db.require_run(run_id)


def run_verification(
    commands: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    timeout: float,
) -> tuple[int, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not commands:
        log_path.write_text("no verification commands\n", encoding="utf-8")
        return 0, "no verification commands\n"
    rc = 0
    chunks = []
    with log_path.open("a", encoding="utf-8") as handle:
        for command in commands:
            handle.write(f"$ {command}\n")
            handle.flush()
            try:
                proc = subprocess.run(
                    command,
                    shell=True,
                    cwd=str(cwd),
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                handle.write((exc.stdout or "") if isinstance(exc.stdout, str) else "")
                handle.write("verification timed out\n")
                chunks.append("verification timed out\n")
                rc = 124
                continue
            handle.write(proc.stdout or "")
            handle.write(proc.stderr or "")
            handle.write(f"exit {proc.returncode}\n")
            chunks.append((proc.stdout or "") + (proc.stderr or ""))
            if proc.returncode != 0 and rc == 0:
                rc = proc.returncode
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
    run_dir.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_FILES:
        path = run_dir / name
        if not path.exists():
            path.write_text("{}\n" if name == "metadata.json" else "", encoding="utf-8")


def _emit(db: Database, run_dir: Path, run_id: str, kind: str, message: str, state: str | None) -> None:
    safe = scrub_text(message)
    db.add_event(run_id, kind, safe, state)
    path = run_dir / "events.jsonl"
    line = json.dumps({"ts": utc_now(), "kind": kind, "state": state, "message": safe})
    with path.open("a", encoding="utf-8") as handle:
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
    }
    (run_dir / "metadata.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _child_env(config: Config, run_dir: Path, workspace: Path, source: Path, run_id: str) -> dict[str, str]:
    del config, run_id
    bin_dir = run_dir / "bin"
    hooks = run_dir / "empty-hooks"
    gitconfig = run_dir / "empty-gitconfig"
    gitconfig.write_text("", encoding="utf-8")
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
    return minimal_env(parent, extra=extra)


def _source_changed(repo: Path, head: str, tree: str) -> tuple[bool, str]:
    try:
        new_head = rev_parse(repo, "HEAD")
        new_tree = porcelain(repo)
    except Exception as exc:
        return True, f"could not re-read the source repository: {exc}"
    if new_head != head or new_tree != tree:
        return True, "source HEAD or working tree differs from the pre-run snapshot"
    return False, ""


def _maybe_remove_worktree(config: Config, source: Path, dest: Path, state: str) -> None:
    if not config.destroy_failed_worktrees:
        return
    if state not in {RunState.FAILED.value, RunState.CANCELLED.value}:
        return
    if not dest.exists():
        return
    subprocess.run(
        [GIT_BIN, "-C", str(source), "worktree", "remove", "--force", str(dest)],
        check=False,
        capture_output=True,
        text=True,
    )


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
