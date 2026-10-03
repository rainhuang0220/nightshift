"""Process adapter shared by providers. No model reasoning lives here."""

from __future__ import annotations

import codecs
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from nightshift.policy import scrub_text
from nightshift.priv import open_private_binary


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
    containment_profile: Path | None = None


@dataclass
class ProviderResult:
    exit_code: int
    session_id: str | None = None
    pid: int | None = None
    pgid: int | None = None
    failure_reason: str | None = None
    argv: list[str] = field(default_factory=list)


MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024

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
            # The leader may have exited while descendants keep the group alive.
            if target_group is not None:
                _signal(pid, target_group, signal.SIGKILL)
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
    containment_profile: Path | None = None,
    max_output_bytes: int = MAX_LOG_BYTES,
) -> ProviderResult:
    if max_output_bytes < 128:
        raise ValueError("max_output_bytes must be at least 128")
    # Open logs before launch; a symlink or unwritable log fails closed.
    handles = [open_private_binary(stdout_path), open_private_binary(stderr_path)]
    launched = list(argv)
    if containment_profile is not None:
        sandbox = Path("/usr/bin/sandbox-exec")
        if not sandbox.is_file():
            for handle in handles:
                handle.close()
            return ProviderResult(
                exit_code=127,
                failure_reason="sandbox-exec is required and was not found",
                argv=launched,
            )
        launched = [str(sandbox), "-f", str(containment_profile), *launched]
    try:
        proc = subprocess.Popen(
            launched, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
    except BaseException:
        for handle in handles:
            handle.close()
        raise
    errors: list[str] = []
    pumps = [threading.Thread(target=_pump, args=(stream, handle, max_output_bytes, errors), daemon=True)
             for stream, handle in zip((proc.stdout, proc.stderr), handles)]
    for pump in pumps:
        pump.start()
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = None
    if on_pid is not None:
        try:
            on_pid(proc.pid, pgid)
        except BaseException:
            terminate_process(proc.pid, pgid)
            proc.wait()
            _join_pumps(pumps)
            raise
    deadline = time.monotonic() + max(timeout, 0.1)
    next_beat = time.monotonic() + 5
    stop_reason = None
    while True:
        code = proc.poll()
        if code is not None:
            terminate_process(proc.pid, pgid)
            _join_pumps(pumps)
            return ProviderResult(exit_code=code if not errors else 125, pid=proc.pid, pgid=pgid,
                                  failure_reason="log capture failed" if errors else None, argv=launched)
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
    _join_pumps(pumps)
    reason = "timed out" if stop_reason == "timeout" else stop_reason
    return ProviderResult(
        exit_code=code if code is not None else -1,
        pid=proc.pid,
        pgid=pgid,
        failure_reason=reason,
        argv=launched,
    )


def _pump(stream, handle, limit: int, errors: list[str]) -> None:
    """Drain every byte; persist bounded, complete redacted lines.

    Oversized lines are suppressed as a whole, so splitting a credential at
    the limit cannot expose its suffix. UTF-8 decoding is incremental.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    dropping = False
    marker = b"\n[nightshift: output truncated]\n"
    budget = max(0, limit - handle.tell() - len(marker))
    written = 0
    truncated = False

    def emit(line: str) -> None:
        nonlocal written, truncated
        data = scrub_text(line).encode("utf-8")
        available = budget - written
        if len(data) > available:
            # Never persist a partial line with a partial redaction.
            truncated = True
            return
        handle.write(data)
        written += len(data)

    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            text = decoder.decode(chunk)
            for piece in text.splitlines(keepends=True):
                if not dropping:
                    pending += piece
                    if len(pending) > MAX_LINE_BYTES:
                        pending = ""
                        dropping = True
                        truncated = True
                if piece.endswith(("\n", "\r")):
                    if not dropping:
                        emit(pending)
                    pending = ""
                    dropping = False
        if not dropping:
            emit(pending + decoder.decode(b"", final=True))
        if truncated:
            handle.write(marker)
    except Exception:
        errors.append("log capture failed")
    finally:
        handle.close()
        stream.close()


def _join_pumps(pumps: list[threading.Thread]) -> None:
    for pump in pumps:
        pump.join(timeout=5)


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
