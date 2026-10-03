"""Convert generic work orders to Nightshift's own immutable job snapshot."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from nightshift.db import Database
from nightshift.gitutil import run_git
from nightshift.job import _verification_value
from nightshift.models import Job
from nightshift.queue import enqueue_job
from nightshift.work_order import WorkOrderError, dumps, timestamp, validate
from nightshift.workspace import WorkspaceError, inspect_source
import tomllib


def check_freshness(order: dict, *, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    if timestamp(order['created_at'], 'created_at') > now:
        raise WorkOrderError('work order creation is in the future')
    for item in order['evidence']:
        if timestamp(item['expires_at'], 'expires_at') <= now:
            raise WorkOrderError('stale work-order evidence; ask the planner for a fresh decision')


def plan_order(order: dict, *, provider: str, max_runtime_seconds: int = 900) -> Job:
    validate(order)
    check_freshness(order)
    if provider not in {'fake', 'grok'}:
        raise WorkOrderError('unsupported execution provider')
    if type(max_runtime_seconds) is not int or not 1 <= max_runtime_seconds <= 86400:
        raise WorkOrderError('max_runtime_seconds must be in 1..86400')
    local = order['repository']['path']
    if local is None:
        raise WorkOrderError('v1 intake requires a local repository path; no automatic network clone')
    repo = Path(local).expanduser().resolve()
    reject_control_plane(repo)
    revision = order['repository']['base_revision']
    if revision is None:
        raise WorkOrderError('intake requires a pinned full base_revision')
    try:
        snapshot = inspect_source(repo, revision)
        top = Path(run_git(repo, ['rev-parse', '--show-toplevel']).stdout.strip()).resolve()
        if top != repo:
            raise WorkOrderError('repository.path must be the checkout root')
    except WorkspaceError as exc:
        raise WorkOrderError(str(exc)) from exc
    display, steps = _verification_value([{'argv': item['argv'], 'timeout_seconds': item['timeout_seconds']}
                                         for item in order['validation']])
    parts = [f"# {order['title']}", order['objective'], '', '## Rationale', order['rationale'], '', '## Evidence']
    for item in order['evidence']:
        parts.append(f"- [{item['kind']}] {item['summary']} (source: {item['source']}; expires: {item['expires_at']})")
    for label, lines in [('Constraints', order['constraints']), ('Validation expectations', [v['expectation'] for v in order['validation']]),
                         ('Allowed actions', order['actions']['allowed']), ('Forbidden actions', order['actions']['forbidden']),
                         ('References', order['references'])]:
        parts += ['', '## ' + label] + ['- ' + line for line in lines]
    return Job(schema_version=1, id=order['task_id'], description=order['title'], type='engineering',
               repository=str(repo), base_ref=snapshot.revision, provider=provider, model=None,
               max_runtime_seconds=max_runtime_seconds, max_attempts=1,
               concurrency_group='work-order', network=False,
               write_scope='workspace' if 'edit' in order['actions']['allowed'] else 'none',
               expected_artifacts=[], verification=display,
               success_criteria=[v['expectation'] for v in order['validation']], allow_bash=[],
               prompt='\n'.join(parts) + '\n', job_dir='', job_file='work-order:' + order['task_id'],
               isolation='clone', verification_steps=steps, work_order=json.loads(dumps(order)))


def import_order(db: Database, order: dict, *, provider: str, max_runtime_seconds: int = 900):
    plan = plan_order(order, provider=provider, max_runtime_seconds=max_runtime_seconds)
    run_id = 'ns-wo-' + hashlib.sha256(order['task_id'].encode()).hexdigest()[:24]
    existing = db.get_run(run_id)
    if existing is None:
        try:
            return enqueue_job(db, plan, run_id=run_id)
        except sqlite3.IntegrityError:
            existing = db.require_run(run_id)
    stored = json.loads(existing.job_snapshot)
    if stored.get('work_order') != plan.work_order or existing.provider != provider or existing.max_runtime_seconds != max_runtime_seconds:
        raise WorkOrderError('task_id already imported with different content or execution settings')
    return existing


def reject_control_plane(repo: Path) -> None:
    project = repo / "pyproject.toml"
    if not project.is_file() or not (repo / "src/nightshift/__init__.py").is_file():
        return
    try:
        name = tomllib.loads(project.read_text(encoding="utf-8")).get("project", {}).get("name")
    except (ValueError, OSError):
        name = "nightshift"
    if name == "nightshift":
        raise WorkOrderError("refusing to target the Nightshift control plane recursively")
