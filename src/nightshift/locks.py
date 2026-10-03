"""Locks stop two runs from sharing a mutable workspace or concurrency group.

A lock whose owner process is gone, or whose run is already terminal, is stale
and may be taken over. Live owners are never stolen.
"""

from __future__ import annotations

import os
import subprocess

from nightshift.db import Database

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


def process_start_token(pid: int | None) -> str:
    """Process start time, used so a recycled pid does not match an old phase."""
    if pid is None or pid <= 0:
        return ""
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def process_identity_matches(pid: int | None, identity: str | None) -> bool:
    """True when `pid` is alive and its start time is still `identity`."""
    if not identity or not pid_alive(pid):
        return False
    return process_start_token(pid) == identity


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


def phase_process_alive(meta: dict, *, fallback_pid: int | None = None) -> bool:
    """Whether the recorded phase process is still that same process.

    PREPARING, verification, and inspect use the pid plus its start time.
    The provider also requires the session id Nightshift put on its argv.
    An empty phase keeps the older pid-plus-token check.
    """
    phase = str(meta.get("phase") or "")
    token = str(meta.get("match") or "")
    identity = str(meta.get("identity") or "")
    raw_pid = meta.get("pid") if meta.get("pid") is not None else fallback_pid
    try:
        pid = int(raw_pid) if raw_pid is not None else None
    except (TypeError, ValueError):
        pid = None
    if phase == "preparing":
        return process_identity_matches(pid, identity)
    if phase == "provider":
        alive = process_matches(pid, token)
        if alive and identity:
            return process_identity_matches(pid, identity)
        return alive
    if phase in {"verifying", "inspect"}:
        return process_identity_matches(pid, identity)
    return process_matches(fallback_pid, token) if fallback_pid else False


class LockManager:
    def __init__(self, db: Database) -> None:
        self.db = db

    def acquire(self, keys: list[str], run_id: str, pid: int | None = None) -> bool:
        owner = pid if pid is not None else os.getpid()
        unique = list(dict.fromkeys(keys))
        return self.db.try_acquire_locks(unique, run_id, owner)

    def release(self, run_id: str) -> None:
        self.db.release_locks(run_id)


def lock_holder_alive(holder, controller_pid: int | None) -> bool:
    """A surviving phase keeps its locks even if the controller disappeared."""
    from nightshift.models import TERMINAL_STATES
    if holder is None:
        return False
    if phase_process_alive(holder.process_meta or {}, fallback_pid=holder.pid):
        return True
    return holder.state not in {state.value for state in TERMINAL_STATES} and pid_alive(controller_pid)


from contextlib import contextmanager
import fcntl
from pathlib import Path


@contextmanager
def execution_lease(state_dir: Path, run_id: str):
    """One controller per run, released by the kernel even after SIGKILL.

    Lease files are never unlinked: replacing the inode would split owners.
    Children do not inherit the descriptor (CLOEXEC, close_fds).
    """
    from nightshift.priv import ensure_private_dir, open_private_binary

    directory = ensure_private_dir(state_dir / "leases")
    with open_private_binary(directory / (run_id + ".lock")) as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
