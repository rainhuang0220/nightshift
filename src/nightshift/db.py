"""SQLite is the authority for run state. JSON logs are copies."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from nightshift.models import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    IllegalTransition,
    NotFoundError,
    RunRecord,
    RunState,
    ensure_transition,
    utc_now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    job_path TEXT NOT NULL DEFAULT '',
    job_snapshot TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    job_type TEXT NOT NULL DEFAULT '',
    source_repo TEXT NOT NULL DEFAULT '',
    source_revision TEXT NOT NULL DEFAULT '',
    source_head TEXT NOT NULL DEFAULT '',
    base_ref TEXT NOT NULL DEFAULT '',
    workspace_path TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    pid INTEGER,
    process_meta TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    concurrency_group TEXT NOT NULL DEFAULT '',
    network INTEGER NOT NULL DEFAULT 0,
    write_scope TEXT NOT NULL DEFAULT 'workspace',
    max_runtime_seconds INTEGER NOT NULL DEFAULT 600,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    ended_at TEXT NOT NULL DEFAULT '',
    heartbeat_at TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    provider_exit_code INTEGER,
    verification_exit_code INTEGER,
    verification_ran INTEGER NOT NULL DEFAULT 0,
    failure_reason TEXT NOT NULL DEFAULT '',
    artifact_paths TEXT NOT NULL DEFAULT '[]',
    report_path TEXT NOT NULL DEFAULT '',
    run_dir TEXT NOT NULL DEFAULT '',
    host_info TEXT NOT NULL DEFAULT '{}',
    duration_seconds REAL,
    recovery_class TEXT NOT NULL DEFAULT '',
    source_porcelain TEXT NOT NULL DEFAULT '',
    source_porcelain_after TEXT NOT NULL DEFAULT '',
    provider_argv TEXT NOT NULL DEFAULT '[]',
    source_integrity TEXT NOT NULL DEFAULT '',
    invocation TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT,
    message TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS locks (
    lock_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    pid INTEGER
);

CREATE INDEX IF NOT EXISTS idx_runs_state_created ON runs(state, created_at);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
"""

_JSON_FIELDS = {"process_meta", "artifact_paths", "host_info", "provider_argv", "invocation"}
_BOOL_FIELDS = {"network", "verification_ran"}
_MUTABLE = {
    "job_id",
    "job_path",
    "job_snapshot",
    "description",
    "job_type",
    "source_repo",
    "source_revision",
    "source_head",
    "base_ref",
    "workspace_path",
    "provider",
    "model",
    "session_id",
    "pid",
    "process_meta",
    "state",
    "attempt",
    "max_attempts",
    "concurrency_group",
    "network",
    "write_scope",
    "max_runtime_seconds",
    "created_at",
    "updated_at",
    "started_at",
    "ended_at",
    "heartbeat_at",
    "exit_code",
    "provider_exit_code",
    "verification_exit_code",
    "verification_ran",
    "failure_reason",
    "artifact_paths",
    "report_path",
    "run_dir",
    "host_info",
    "duration_seconds",
    "recovery_class",
    "source_porcelain",
    "source_porcelain_after",
    "provider_argv",
    "source_integrity",
    "invocation",
}


def _encode(key: str, value):
    if key in _JSON_FIELDS:
        if value is None:
            value = {} if key in {"process_meta", "host_info", "invocation"} else []
        return json.dumps(value)
    if key in _BOOL_FIELDS:
        return 1 if value else 0
    return value


class _LockDenied(Exception):
    """Ownership could not be proved. The open transaction must roll back."""


def _acquire_locks_conn(conn: sqlite3.Connection, keys: list[str], run_id: str, pid: int) -> bool:
    """Inside an open IMMEDIATE transaction: own every key, or change nothing.

    A live owner other than `run_id` fails the call before any delete or insert.
    Stale rows are removed only after every key has been classified. Same-run
    ownership is refreshed in place.
    """
    from nightshift.locks import lock_holder_alive

    stale: list[str] = []
    for key in keys:
        row = conn.execute(
            "SELECT lock_key, run_id, pid FROM locks WHERE lock_key = ?",
            (key,),
        ).fetchone()
        if row is None or row["run_id"] == run_id:
            continue
        holder = conn.execute("SELECT * FROM runs WHERE run_id = ?", (row["run_id"],)).fetchone()
        if not lock_holder_alive(_row_to_run(holder) if holder else None, row["pid"]):
            stale.append(key)
            continue
        return False
    for key in stale:
        conn.execute("DELETE FROM locks WHERE lock_key = ?", (key,))
    now = utc_now()
    for key in keys:
        row = conn.execute(
            "SELECT lock_key, run_id FROM locks WHERE lock_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO locks (lock_key, run_id, acquired_at, pid) VALUES (?, ?, ?, ?)",
                (key, run_id, now, pid),
            )
            continue
        if row["run_id"] != run_id:
            raise _LockDenied()
        conn.execute(
            "UPDATE locks SET acquired_at = ?, pid = ? WHERE lock_key = ? AND run_id = ?",
            (now, pid, key, run_id),
        )
    for key in keys:
        row = conn.execute("SELECT run_id FROM locks WHERE lock_key = ?", (key,)).fetchone()
        if row is None or row["run_id"] != run_id:
            raise _LockDenied()
    return True


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    data = dict(row)
    data["process_meta"] = json.loads(data["process_meta"] or "{}")
    data["artifact_paths"] = json.loads(data["artifact_paths"] or "[]")
    data["host_info"] = json.loads(data["host_info"] or "{}")
    data["provider_argv"] = json.loads(data["provider_argv"] or "[]")
    data["invocation"] = json.loads(data.get("invocation") or "{}")
    data["source_integrity"] = data.get("source_integrity") or ""
    data["network"] = bool(data["network"])
    data["verification_ran"] = bool(data["verification_ran"])
    return RunRecord(**data)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._migrate()
        os.chmod(path, 0o600)
        if path.parent.exists():
            os.chmod(path.parent, 0o700)

    def _migrate(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(runs)")}
        if "source_integrity" not in columns:
            self._conn.execute("ALTER TABLE runs ADD COLUMN source_integrity TEXT NOT NULL DEFAULT ''")
        if "invocation" not in columns:
            self._conn.execute("ALTER TABLE runs ADD COLUMN invocation TEXT NOT NULL DEFAULT '{}'")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._depth:
                raise RuntimeError("nested transaction")
            self._depth += 1
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    yield self._conn
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                else:
                    self._conn.execute("COMMIT")
            finally:
                self._depth -= 1

    def insert_run(self, run: RunRecord) -> RunRecord:
        fields = list(_MUTABLE) + ["run_id"]
        # run_id is in the dataclass and also the primary key; _MUTABLE excludes it.
        columns = ["run_id", *_MUTABLE]
        values = []
        raw = run.__dict__
        for key in columns:
            values.append(_encode(key, raw[key]))
        placeholders = ",".join("?" for _ in columns)
        with self.transaction() as conn:
            conn.execute(
                f"INSERT INTO runs ({','.join(columns)}) VALUES ({placeholders})",
                values,
            )
            self._insert_event(conn, run.run_id, "queued", run.state, "run queued")
        fetched = self.get_run(run.run_id)
        assert fetched is not None
        return fetched

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._fetch(run_id)

    def _fetch(self, run_id: str) -> RunRecord | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return _row_to_run(row)

    def require_run(self, run_id: str) -> RunRecord:
        run = self.get_run(run_id)
        if run is None:
            raise NotFoundError(f"unknown run {run_id}")
        return run

    def list_runs(self) -> list[RunRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM runs ORDER BY created_at, run_id").fetchall()
        return [_row_to_run(row) for row in rows]

    def latest_run(self) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, run_id DESC LIMIT 1"
            ).fetchone()
        return _row_to_run(row) if row else None

    def count_states(self, states: list[str] | None = None) -> int:
        with self._lock:
            if not states:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
                return int(row["n"])
            marks = ",".join("?" for _ in states)
            row = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM runs WHERE state IN ({marks})",
                states,
            ).fetchone()
            return int(row["n"])

    def active_runs(self) -> list[RunRecord]:
        states = [state.value for state in ACTIVE_STATES]
        marks = ",".join("?" for _ in states)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM runs WHERE state IN ({marks}) ORDER BY created_at",
                states,
            ).fetchall()
        return [_row_to_run(row) for row in rows]

    def transition(self, run_id: str, new_state: str, message: str = "", **fields) -> RunRecord:
        unknown = set(fields) - _MUTABLE - {"state"}
        if unknown:
            raise IllegalTransition(f"unknown run fields: {sorted(unknown)}")
        with self.transaction():
            current = self._fetch(run_id)
            if current is None:
                raise NotFoundError(f"unknown run {run_id}")
            ensure_transition(current.state, new_state)
            fields["state"] = new_state
            fields["updated_at"] = utc_now()
            assignments = ", ".join(f"{key} = ?" for key in fields)
            values = [_encode(key, value) for key, value in fields.items()]
            cursor = self._conn.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ? AND state = ?",
                [*values, run_id, current.state],
            )
            if cursor.rowcount != 1:
                raise IllegalTransition(f"concurrent transition for {run_id}")
            self._insert_event(
                self._conn,
                run_id,
                "state",
                new_state,
                message or f"{current.state} -> {new_state}",
            )
            updated = self._fetch(run_id)
        assert updated is not None
        return updated

    def update_run(self, run_id: str, **fields) -> RunRecord:
        unknown = set(fields) - _MUTABLE
        if unknown:
            raise NightshiftUpdateError(f"unknown run fields: {sorted(unknown)}")
        if "state" in fields:
            raise IllegalTransition("state changes must use transition()")
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [_encode(key, value) for key, value in fields.items()]
        with self.transaction():
            if self._fetch(run_id) is None:
                raise NotFoundError(f"unknown run {run_id}")
            self._conn.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?",
                [*values, run_id],
            )
            updated = self._fetch(run_id)
        assert updated is not None
        return updated

    def heartbeat(self, run_id: str) -> None:
        now = utc_now()
        with self.transaction():
            self._conn.execute(
                "UPDATE runs SET heartbeat_at = ? WHERE run_id = ?",
                (now, run_id),
            )

    def add_event(self, run_id: str, kind: str, message: str, state: str | None = None) -> None:
        with self.transaction() as conn:
            self._insert_event(conn, run_id, kind, state, message)

    def list_events(self, run_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._conn.execute(
                    "SELECT id, run_id, ts, kind, state, message FROM events WHERE run_id = ? ORDER BY id",
                    (run_id,),
                )
            )

    def _insert_event(self, conn: sqlite3.Connection, run_id: str, kind: str, state: str | None, message: str) -> None:
        conn.execute(
            "INSERT INTO events (run_id, ts, kind, state, message) VALUES (?, ?, ?, ?, ?)",
            (run_id, utc_now(), kind, state, message),
        )

    def lock_rows(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute("SELECT lock_key, run_id, acquired_at, pid FROM locks ORDER BY lock_key"))

    def locks_held_by_others(self, keys: list[str], run_id: str) -> list[str]:
        if not keys:
            return []
        marks = ",".join("?" for _ in keys)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT lock_key, run_id, pid FROM locks WHERE lock_key IN ({marks})",
                keys,
            ).fetchall()
        return [row["lock_key"] for row in rows if row["run_id"] != run_id]

    def delete_lock(self, key: str) -> None:
        with self.transaction():
            self._conn.execute("DELETE FROM locks WHERE lock_key = ?", (key,))

    def write_locks(self, keys: list[str], run_id: str, pid: int) -> None:
        """Insert lock rows for `run_id`. A live other owner is left unchanged."""
        self.try_acquire_locks(keys, run_id, pid)

    def try_acquire_locks(self, keys: list[str], run_id: str, pid: int) -> bool:
        """Acquire every key or none. Never replaces a live owner."""
        unique = list(dict.fromkeys(key for key in keys if key))
        if not unique:
            return True
        try:
            with self.transaction() as conn:
                return _acquire_locks_conn(conn, unique, run_id, pid)
        except _LockDenied:
            return False

    def release_locks(self, run_id: str) -> None:
        with self.transaction():
            self._conn.execute("DELETE FROM locks WHERE run_id = ?", (run_id,))

    def queued_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id FROM runs WHERE state = ? ORDER BY created_at, run_id",
                (RunState.QUEUED.value,),
            ).fetchall()
        return [row["run_id"] for row in rows]


class NightshiftUpdateError(Exception):
    pass
