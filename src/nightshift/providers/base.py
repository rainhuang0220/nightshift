"""Process adapter shared by providers. No model reasoning lives here."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class ProviderRequest:
    run_id: str
    workspace: Path
    prompt_path: Path
    stdout_path: Path
    stderr_path: Path
    env: dict[str, str]
    timeout: float
    write_scope: str
    session_id: str
    model: str | None
    max_turns: int
    resume: bool = False


@dataclass
class ProviderResult:
    exit_code: int
    session_id: str | None = None
    pid: int | None = None
    pgid: int | None = None
    failure_reason: str | None = None
    argv: list[str] = field(default_factory=list)


StopCheck = Callable[[], str | None]


def terminate_process(pid: int | None, pgid: int | None = None) -> None:
    """Terminate a child. Never signal the supervisor's own process group."""
    if pid is None or pid <= 0:
        return
    own_group = os.getpgrp()
    target_group = pgid if pgid and pgid > 0 and pgid != own_group else None
    _signal(pid, target_group, signal.SIGTERM)
    for _ in range(25):
        if not _alive(pid):
            return
        time.sleep(0.1)
    _signal(pid, target_group, signal.SIGKILL)


def run_subprocess(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
    timeout: float,
    on_pid: Callable[[int, int | None], None] | None = None,
    poll_stop: StopCheck | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> ProviderResult:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.touch()
    stderr_path.touch()
    with stdout_path.open("ab", buffering=0) as stdout, stderr_path.open("ab", buffering=0) as stderr:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pgid = None
        if on_pid is not None:
            on_pid(proc.pid, pgid)
        deadline = time.monotonic() + max(timeout, 0.1)
        next_beat = time.monotonic() + 5
        stop_reason = None
        while True:
            code = proc.poll()
            if code is not None:
                return ProviderResult(exit_code=code, pid=proc.pid, pgid=pgid, argv=list(argv))
            now = time.monotonic()
            if poll_stop is not None:
                stop_reason = poll_stop()
                if stop_reason:
                    break
            if now >= deadline:
                stop_reason = "timeout"
                break
            if heartbeat is not None and now >= next_beat:
                heartbeat()
                next_beat = now + 5
            time.sleep(0.2)
        terminate_process(proc.pid, pgid)
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            code = -9
        reason = "timed out" if stop_reason == "timeout" else stop_reason
        return ProviderResult(
            exit_code=code if code is not None else -1,
            pid=proc.pid,
            pgid=pgid,
            failure_reason=reason,
            argv=list(argv),
        )


def _signal(pid: int, pgid: int | None, sig: int) -> None:
    try:
        if pgid is not None:
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        # macOS returns EPERM when the group is already empty or only a zombie remains.
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            return


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return _process_state(pid) != "Z"


def _process_state(pid: int) -> str:
    try:
        out = subprocess.check_output(
            ["ps", "-o", "state=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ""
    return out.strip()[:1]
