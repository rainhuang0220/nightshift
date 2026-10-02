"""Seatbelt profiles for provider and verification processes.

Grok permission rules and the PATH shim are not this boundary. `sandbox-exec`
is the process and filesystem containment used on this machine. If it is
missing, Nightshift refuses the run instead of executing unsandboxed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from nightshift.policy import scrub_text
from nightshift.priv import write_private_text
from nightshift.providers.base import terminate_process

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")


class ContainmentError(Exception):
    pass


def sandbox_available() -> bool:
    return SANDBOX_EXEC.is_file()


def write_profile(
    path: Path,
    *,
    writable: list[Path],
    network: bool,
    read_deny: list[Path] | None = None,
) -> Path:
    """Write a deny-default profile. Paths are resolved so /var aliases match."""
    if not sandbox_available():
        raise ContainmentError("sandbox-exec is not available; refusing to run unsandboxed")
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process*)",
        "(allow signal)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix*)",
        "(allow file-ioctl)",
        "(allow file-read*)",
        '(allow file-read* (literal "/dev/null"))',
        '(allow file-write* (literal "/dev/null"))',
    ]
    for item in writable:
        resolved = Path(item).resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        lines.append(f'(allow file-write* (subpath "{_escape(resolved)}"))')
    for item in read_deny or []:
        target = Path(item)
        lines.append(f'(deny file-read* (subpath "{_escape(target)}"))')
        lines.append(f'(deny file-write* (subpath "{_escape(target)}"))')
    lines.append("(allow network*)" if network else "(deny network*)")
    write_private_text(path, "\n".join(lines) + "\n")
    return path


def contained_argv(argv: list[str], profile: Path | None) -> list[str]:
    if profile is None:
        return list(argv)
    if not sandbox_available():
        raise ContainmentError("sandbox-exec is not available; refusing to run unsandboxed")
    return [str(SANDBOX_EXEC), "-f", str(profile), *argv]


def contained_run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    profile: Path,
    timeout: float,
) -> tuple[int, str]:
    """Run argv under the profile. Timeout kills the child process group."""
    full = contained_argv(argv, profile)
    proc = subprocess.Popen(
        full,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        pgid = _pgid(proc.pid)
        terminate_process(proc.pid, pgid)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        output = scrub_text((stdout or "") + (stderr or "") + "verification timed out\n")
        return 124, output
    output = scrub_text((stdout or "") + (stderr or ""))
    code = proc.returncode if proc.returncode is not None else 1
    return code, output


def operator_read_denies(operator_home: Path) -> list[Path]:
    """Credential locations the sandboxed child must not read by absolute path.

    The Grok binary may live under the operator's `.grok/bin`, so the whole
    `.grok` tree is not denied. Secret files and extension directories are.
    """
    home = operator_home
    names = [
        ".ssh",
        ".aws",
        ".gnupg",
        ".config/gh",
        ".netrc",
        ".npmrc",
        ".git-credentials",
        ".kaggle",
        ".config/kaggle",
        ".grok/auth.json",
        ".grok/mcp_credentials.json",
        ".grok/config.toml",
        ".grok/skills",
        ".grok/hooks",
        ".grok/installed-plugins",
        ".grok/memory",
        ".grok/memory-v2",
        ".grok/sessions",
        ".grok/plugins",
    ]
    return [home / name for name in names]


def _escape(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def _pgid(pid: int) -> int | None:
    try:
        group = os.getpgid(pid)
    except ProcessLookupError:
        return None
    if group == os.getpgrp():
        return None
    return group
