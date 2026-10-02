"""Isolated Grok HOME and the minimum authentication bootstrap.

Nightshift does not run the provider under the operator's normal HOME.
Authentication is a separate copy of the minimum Grok auth file. This module
never prints file contents.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from nightshift.models import NightshiftError
from nightshift.priv import ensure_private_dir

AUTH_FILENAME = "auth.json"


def grok_profile_dir(state_dir: Path) -> Path:
    return state_dir / "grok-profile"


def prepare_runtime_dirs(run_dir: Path, profile: Path) -> tuple[Path, Path]:
    """Return `(runtime_home, private_tmp)`, both mode 0700."""
    runtime_home = ensure_private_dir(run_dir / "runtime-home")
    private_tmp = ensure_private_dir(run_dir / "tmp")
    ensure_private_dir(profile)
    return runtime_home, private_tmp


def apply_runtime_env(env: dict[str, str], *, runtime_home: Path, grok_home: Path, private_tmp: Path) -> dict[str, str]:
    """Replace HOME and point Grok at the Nightshift profile."""
    updated = dict(env)
    updated["HOME"] = str(runtime_home)
    updated["TMPDIR"] = str(private_tmp)
    updated["TEMP"] = str(private_tmp)
    updated["TMP"] = str(private_tmp)
    updated["GROK_HOME"] = str(grok_home)
    updated["GROK_MEMORY"] = "0"
    updated["GROK_WORKFLOWS"] = "0"
    updated["PYTHONDONTWRITEBYTECODE"] = "1"
    updated.pop("SSH_AUTH_SOCK", None)
    return updated


def auth_status(profile: Path) -> dict[str, str]:
    """Report presence and mode only. No paths and no file contents."""
    ensure_private_dir(profile)
    path = profile / AUTH_FILENAME
    profile_mode = _mode(profile)
    if not path.is_file():
        return {"auth": "absent", "profile_mode": profile_mode, "auth_mode": ""}
    return {"auth": "present", "profile_mode": profile_mode, "auth_mode": _mode(path)}


def auth_bootstrap(profile: Path, source_home: Path | None = None) -> str:
    """Copy only `auth.json` from an existing Grok profile into `profile`.

    The copy is a separate file so later refresh does not rewrite the
    operator's live auth file. Contents are never returned.
    """
    origin = (source_home if source_home is not None else Path.home() / ".grok") / AUTH_FILENAME
    if not origin.is_file():
        raise NightshiftError("grok auth file is missing; cannot bootstrap", exit_code=1)
    ensure_private_dir(profile)
    destination = profile / AUTH_FILENAME
    data = origin.read_bytes()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(destination, flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(destination, 0o600)
    os.chmod(profile, 0o700)
    return "bootstrapped"


def _mode(path: Path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))
