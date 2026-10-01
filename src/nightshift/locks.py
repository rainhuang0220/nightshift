"""Locks stop two runs from sharing a mutable workspace or concurrency group.

A lock whose owner process is gone, or whose run is already terminal, is stale
and may be taken over. Live owners are never stolen.
"""

from __future__ import annotations

import os
import subprocess

from nightshift.db import Database
from nightshift.models import TERMINAL_STATES

GROUP_PREFIX = "group:"
REPO_PREFIX = "repo:"
WORKSPACE_PREFIX = "workspace:"


def group_key(name: str) -> str:
    return GROUP_PREFIX + name


def repo_key(path: str) -> str:
    return REPO_PREFIX + path


def workspace_key(path: str) -> str:
    return WORKSPACE_PREFIX + path


def pid_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_matches(pid: int | None, token: str | None) -> bool:
    """True only when `pid` is alive and its command line contains `token`.

    The token is a session or run id Nightshift put on the child argv, so a
    recycled pid that belongs to an unrelated process does not look alive.
    """
    if not pid_alive(pid):
        return False
    if not token:
        return False
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    if proc.returncode != 0:
        return False
    return token in proc.stdout


class LockManager:
    def __init__(self, db: Database) -> None:
        self.db = db

    def acquire(self, keys: list[str], run_id: str, pid: int | None = None) -> bool:
        owner = pid if pid is not None else os.getpid()
        unique = list(dict.fromkeys(keys))
        for key in unique:
            if not self._clear_if_stale(key, run_id):
                return False
        self.db.write_locks(unique, run_id, owner)
        # Confirm we still own every key. A concurrent acquire can win the write.
        held = set(self.db.locks_held_by_others(unique, run_id))
        if held:
            self.db.release_locks(run_id)
            return False
        return True

    def release(self, run_id: str) -> None:
        self.db.release_locks(run_id)

    def _clear_if_stale(self, key: str, run_id: str) -> bool:
        rows = [row for row in self.db.lock_rows() if row["lock_key"] == key]
        if not rows:
            return True
        row = rows[0]
        if row["run_id"] == run_id:
            return True
        holder = self.db.get_run(row["run_id"])
        terminal = holder is None or holder.state in {state.value for state in TERMINAL_STATES}
        if terminal or not pid_alive(row["pid"]):
            self.db.delete_lock(key)
            return True
        return False
