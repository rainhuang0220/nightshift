"""Operator CLI. Exit 0 only when the command succeeded."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from nightshift import __version__
from nightshift.config import Config, load_config
from nightshift.db import Database
from nightshift.doctor import run_doctor
from nightshift.job import JobValidationError, load_job
from nightshift.work_order import WorkOrderError
from nightshift.locks import LockManager
from nightshift.models import (
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    NightshiftError,
    RunState,
    exit_code_for_state,
)
from nightshift.policy import scrub_text
from nightshift.queue import enqueue
from nightshift.runner import execute_run, publish_report
from nightshift.supervisor import cancel_run, recover_all, serve


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        print(__version__)
        return EXIT_OK
    try:
        if args.command == "work-order" and args.work_order_command in {"validate", "preview"}:
            from nightshift.work_order import load, dumps
            order = load(Path(args.path))
            if args.work_order_command == "validate":
                sys.stdout.write(dumps(order))
            else:
                from nightshift.intake import plan_order
                plan = plan_order(order, provider=args.provider, max_runtime_seconds=args.max_runtime_seconds)
                print(json.dumps(plan.to_dict(), sort_keys=True, indent=2))
            return EXIT_OK
        config = load_config(args.root, args.config)
        if args.command == "doctor":
            code, lines = run_doctor(config)
            sys.stdout.write("\n".join(lines) + "\n")
            return code
        if args.command == "job":
            return _job(args)
        if args.command == "auth":
            config.ensure_dirs()
            return _auth(config, args)
        if args.command == "safety":
            config.ensure_dirs()
            return _safety(config, args)
        db = Database(config.db_path)
        locks = LockManager(db)
        try:
            return _dispatch(config, db, locks, args)
        finally:
            db.close()
    except WorkOrderError as exc:
        print(scrub_text(f"invalid work order: {exc}"), file=sys.stderr)
        return EXIT_USAGE
    except JobValidationError as exc:
        for error in exc.errors:
            print(f"invalid: {error}", file=sys.stderr)
        return EXIT_USAGE
    except NightshiftError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED


def _dispatch(config: Config, db: Database, locks: LockManager, args: argparse.Namespace) -> int:
    if args.command == "work-order" and args.work_order_command == "import":
        from nightshift.intake import import_order
        from nightshift.work_order import load
        run = import_order(db, load(Path(args.path)), provider=args.provider, max_runtime_seconds=args.max_runtime_seconds)
        if args.run and run.state == RunState.QUEUED.value:
            run = execute_run(config, db, locks, run.run_id)
        print(json.dumps({"run_id": run.run_id, "state": run.state, "attempt": run.attempt,
                          "workspace": run.workspace_path, "report": run.report_path}, sort_keys=True))
        return exit_code_for_state(run.state) if args.run else EXIT_OK
    if args.command == "queue" and args.queue_command == "add":
        run = enqueue(db, Path(args.path), provider=args.provider)
        print(f"queued {run.run_id} {run.job_id} {run.state}")
        return EXIT_OK
    if args.command == "queue" and args.queue_command == "list":
        return _print_runs(db.list_runs())
    if args.command == "run":
        run = enqueue(db, Path(args.path), provider=args.provider)
        finished = execute_run(config, db, locks, run.run_id)
        print(f"run {finished.run_id} {finished.state}")
        if finished.report_path:
            print(f"report {finished.report_path}")
        if finished.failure_reason:
            print(scrub_text(finished.failure_reason), file=sys.stderr)
        return exit_code_for_state(finished.state)
    if args.command == "daemon":
        stop = threading.Event()

        def _stop(signum, frame) -> None:
            del signum, frame
            stop.set()

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        return serve(config, db, locks, stop)
    if args.command == "status":
        runs = db.list_runs()
        counts: dict[str, int] = {}
        for run in runs:
            counts[run.state] = counts.get(run.state, 0) + 1
        if not runs:
            print("runs 0")
            return EXIT_OK
        summary = " ".join(f"{state}={counts[state]}" for state in sorted(counts))
        print(f"runs {len(runs)} {summary}")
        return _print_runs(runs)
    if args.command == "logs":
        return _logs(db, args.run_id)
    if args.command == "report":
        return _report(config, db, args.run_id)
    if args.command == "recover":
        lines, refused = recover_all(config, db, locks, retry=args.retry)
        sys.stdout.write("\n".join(lines) + "\n")
        return EXIT_FAILED if refused else EXIT_OK
    if args.command == "cancel":
        run = cancel_run(config, db, locks, args.run_id)
        print(f"cancel {run.run_id} {run.state}")
        return EXIT_OK if run.state == RunState.CANCELLED.value else EXIT_FAILED
    print("error: unknown command", file=sys.stderr)
    return EXIT_USAGE


def _auth(config: Config, args: argparse.Namespace) -> int:
    from nightshift.runtime import auth_bootstrap, auth_status

    if args.auth_command != "grok":
        print("error: unknown auth command", file=sys.stderr)
        return EXIT_USAGE
    profile = config.auth_store()
    if args.grok_auth_command == "status":
        status = auth_status(profile)
        print(f"auth {status['auth']}")
        if status["auth_mode"]:
            print(f"auth_mode {status['auth_mode']}")
        print(f"profile_mode {status['profile_mode']}")
        return EXIT_OK
    if args.grok_auth_command == "bootstrap":
        result = auth_bootstrap(profile)
        print(result)
        return EXIT_OK
    print("error: unknown grok auth command", file=sys.stderr)
    return EXIT_USAGE


def _safety(config: Config, args: argparse.Namespace) -> int:
    if args.safety_command != "probe":
        print("error: unknown safety command", file=sys.stderr)
        return EXIT_USAGE
    from nightshift.probe import run_safety_probe

    code, text = run_safety_probe(args.provider, profile=config.auth_store())
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return code


def _job(args: argparse.Namespace) -> int:
    if args.job_command != "validate":
        print("error: unknown job command", file=sys.stderr)
        return EXIT_USAGE
    job = load_job(Path(args.path))
    print(f"valid {job.id} provider={job.provider} write_scope={job.write_scope}")
    return EXIT_OK


def _print_runs(runs) -> int:
    if not runs:
        print("no runs")
        return EXIT_OK
    for run in runs:
        print(
            f"{run.run_id} {run.job_id} {run.state} attempt={run.attempt} provider={run.provider}"
        )
    return EXIT_OK


def _attempt_log(run_dir: Path | None, attempt: int, name: str) -> Path:
    if run_dir is None:
        return Path(name)
    current = run_dir / f"attempt-{attempt if attempt > 0 else 1}" / name
    if current.is_file():
        return current
    return run_dir / name


def _logs(db: Database, run_id: str) -> int:
    run = db.require_run(run_id)
    print(f"run {run.run_id} {run.state}")
    print("--- events")
    for event in db.list_events(run_id):
        print(f"{event['ts']} {event['kind']} {event['state'] or '-'} {scrub_text(event['message'])}")
    run_dir = Path(run.run_dir) if run.run_dir else None
    print("--- stdout")
    print(_tail(_attempt_log(run_dir, run.attempt, "provider.stdout.log")) if run_dir else "(no run directory)")
    print("--- stderr")
    print(_tail(_attempt_log(run_dir, run.attempt, "provider.stderr.log")) if run_dir else "(no run directory)")
    return EXIT_OK


def _report(config: Config, db: Database, run_id: str | None) -> int:
    if run_id:
        run = db.require_run(run_id)
    else:
        run = db.latest_run()
        if run is None:
            print("error: no runs", file=sys.stderr)
            return 3
    path = Path(run.report_path) if run.report_path else None
    if path is None or not path.is_file():
        run = publish_report(config, db, run.run_id)
        path = Path(run.report_path)
    if not path.is_file():
        print("error: report was not written", file=sys.stderr)
        return EXIT_FAILED
    sys.stdout.write(path.read_text(encoding="utf-8"))
    return EXIT_OK


def _tail(path: Path, limit: int = 80) -> str:
    if not path.is_file():
        return "(missing)"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return scrub_text("\n".join(lines[-limit:]))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nightshift")
    parser.add_argument("--root", type=Path, default=None, help="directory for state, runs, and worktrees")
    parser.add_argument("--config", type=Path, default=None, help="TOML config file")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check Python, state, SQLite, and the Grok CLI")
    sub.add_parser("version", help="print the Nightshift version")

    work_order = sub.add_parser("work-order", help="validate, preview and import generic Engineering Work Orders v1")
    order_sub = work_order.add_subparsers(dest="work_order_command", required=True)
    for command in ("validate", "preview", "import"):
        entry = order_sub.add_parser(command)
        entry.add_argument("path")
        if command != "validate":
            entry.add_argument("--provider", choices=("fake", "grok"), required=True)
            entry.add_argument("--max-runtime-seconds", type=int, default=900)
        if command == "import":
            entry.add_argument("--run", action="store_true", help="execute only if this task is still queued")

    job = sub.add_parser("job", help="job manifests")
    job_sub = job.add_subparsers(dest="job_command", required=True)
    validate = job_sub.add_parser("validate", help="validate a job directory or job.toml")
    validate.add_argument("path")

    queue = sub.add_parser("queue", help="queue jobs")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    add = queue_sub.add_parser("add", help="enqueue a job")
    add.add_argument("path")
    add.add_argument("--provider", choices=("fake", "grok"), default=None)
    queue_sub.add_parser("list", help="list runs")

    run = sub.add_parser("run", help="enqueue a job and execute it")
    run.add_argument("path")
    run.add_argument("--provider", choices=("fake", "grok"), default=None)

    sub.add_parser("daemon", help="execute queued jobs until interrupted")
    sub.add_parser("status", help="show run counts and recent runs")

    logs = sub.add_parser("logs", help="show events and provider logs")
    logs.add_argument("run_id")

    report = sub.add_parser("report", help="print a morning report")
    report.add_argument("run_id", nargs="?")

    recover = sub.add_parser("recover", help="reconcile active runs without launching a provider")
    recover.add_argument(
        "--retry",
        action="store_true",
        help="requeue INTERRUPTED or FAILED runs when attempts remain",
    )

    cancel = sub.add_parser("cancel", help="cancel a queued or active run")
    cancel.add_argument("run_id")

    auth = sub.add_parser("auth", help="manage the dedicated Nightshift Grok profile")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    grok_auth = auth_sub.add_parser("grok", help="Grok authentication profile")
    grok_auth_sub = grok_auth.add_subparsers(dest="grok_auth_command", required=True)
    grok_auth_sub.add_parser("status", help="report whether the Nightshift profile has auth material")
    grok_auth_sub.add_parser("bootstrap", help="copy the minimum Grok auth file into the Nightshift profile")

    safety = sub.add_parser("safety", help="isolation and safety checks")
    safety_sub = safety.add_subparsers(dest="safety_command", required=True)
    probe = safety_sub.add_parser("probe", help="run the safety probe; fake unless --provider grok")
    probe.add_argument("--provider", choices=("fake", "grok"), default="fake")
    return parser


if __name__ == "__main__":
    sys.exit(main())
