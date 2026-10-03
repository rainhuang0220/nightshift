"""Queue is a SQL view of runs in QUEUED, claimed by a conditional update."""

from __future__ import annotations

import json
import os
from pathlib import Path

from nightshift.db import Database
from nightshift.job import load_job, resolved_repository
from nightshift.locks import group_key, repo_key
from nightshift.models import RunRecord, RunState, empty_run, utc_now


def enqueue(db: Database, job_path: Path, *, provider: str | None = None) -> RunRecord:
    job = load_job(job_path)
    return enqueue_job(db, job, provider=provider)


def enqueue_job(db: Database, job, *, provider: str | None = None, run_id: str | None = None) -> RunRecord:
    chosen = provider or job.provider
    record = empty_run(
        job_id=job.id,
        job_path=job.job_file,
        job_snapshot=json.dumps(job.to_dict()),
        description=job.description,
        job_type=job.type,
        source_repo=str(resolved_repository(job)),
        base_ref=job.base_ref,
        provider=chosen,
        model=job.model or "",
        state=RunState.QUEUED.value,
        attempt=1,
        max_attempts=job.max_attempts,
        concurrency_group=job.concurrency_group,
        network=job.network,
        write_scope=job.write_scope,
        max_runtime_seconds=job.max_runtime_seconds,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    if run_id is not None:
        record.run_id = run_id
    return db.insert_run(record)


def claim_next(db: Database) -> RunRecord | None:
    """Claim the oldest queued run whose group and repository are free.

    The conditional update and the lock insert share one immediate transaction.
    This does not launch a provider.
    """
    from nightshift.db import _LockDenied, _acquire_locks_conn

    try:
        with db.transaction():
            for run_id in _queued_ids_locked(db):
                run = db._fetch(run_id)
                if run is None or run.state != RunState.QUEUED.value:
                    continue
                keys = _lock_keys(run)
                if not _locks_available(db, keys, run.run_id):
                    continue
                now = utc_now()
                cursor = db._conn.execute(
                    """
                    UPDATE runs
                       SET state = ?, updated_at = ?
                     WHERE run_id = ? AND state = ?
                    """,
                    (RunState.PREPARING.value, now, run.run_id, RunState.QUEUED.value),
                )
                if cursor.rowcount != 1:
                    continue
                if keys and not _acquire_locks_conn(db._conn, keys, run.run_id, os.getpid()):
                    db._conn.execute(
                        "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ? AND state = ?",
                        (RunState.QUEUED.value, utc_now(), run.run_id, RunState.PREPARING.value),
                    )
                    continue
                db._insert_event(
                    db._conn,
                    run.run_id,
                    "state",
                    RunState.PREPARING.value,
                    "claimed",
                )
                claimed = db._fetch(run.run_id)
                return claimed
    except _LockDenied:
        return None
    return None


def _queued_ids_locked(db: Database) -> list[str]:
    rows = db._conn.execute(
        "SELECT run_id FROM runs WHERE state = ? ORDER BY created_at, run_id",
        (RunState.QUEUED.value,),
    ).fetchall()
    return [row["run_id"] for row in rows]


def _lock_keys(run: RunRecord) -> list[str]:
    keys = [group_key(run.concurrency_group)] if run.concurrency_group else []
    if run.source_repo:
        keys.append(repo_key(run.source_repo))
    return keys


def _locks_available(db: Database, keys: list[str], run_id: str) -> bool:
    """Read-only classification. Stale rows are removed later by the shared acquire."""
    if not keys:
        return True
    from nightshift.locks import lock_holder_alive

    marks = ",".join("?" for _ in keys)
    rows = db._conn.execute(
        f"SELECT lock_key, run_id, pid FROM locks WHERE lock_key IN ({marks})",
        keys,
    ).fetchall()
    for row in rows:
        if row["run_id"] == run_id:
            continue
        holder = db._fetch(row["run_id"])
        if not lock_holder_alive(holder, row["pid"]):
            continue
        return False
    return True
