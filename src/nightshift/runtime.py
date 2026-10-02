"""Isolated Grok HOME and the minimum authentication bootstrap.

The persistent auth store and the per-run GROK_HOME are different directories.
`nightshift auth grok bootstrap` copies only `auth.json` into the store.
Each launch copies that file into a new private GROK_HOME and deletes the
copy when the run ends. The store is never deleted here, and file contents
are never printed.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from nightshift.models import NightshiftError
from nightshift.priv import ensure_private_dir

AUTH_FILENAME = "auth.json"
LEGACY_PROFILE_DIR = "grok-profile"
PER_RUN_HOME_DIR = "grok-home"


def auth_store_dir(state_dir: Path) -> Path:
    return state_dir / "credentials" / "grok"


def legacy_profile_dir(state_dir: Path) -> Path:
    return state_dir / LEGACY_PROFILE_DIR


def per_run_grok_home(run_dir: Path) -> Path:
    return run_dir / PER_RUN_HOME_DIR


def ensure_auth_store(state_dir: Path) -> Path:
    """Create the persistent store and copy a legacy auth file once.

    A mismatched copy is removed and the legacy file is left in place. This
    function never deletes the persistent store and never prints bytes.
    """
    store = ensure_private_dir(auth_store_dir(state_dir))
    os.chmod(store.parent, 0o700)
    destination = store / AUTH_FILENAME
    legacy = legacy_profile_dir(state_dir) / AUTH_FILENAME
    if destination.exists() or not legacy.is_file() or legacy.is_symlink():
        return store
    copy_auth_file(legacy, destination)
    if destination.read_bytes() != legacy.read_bytes():
        destination.unlink()
        raise NightshiftError("auth migration copy mismatched; the existing file was left in place")
    return store


def prepare_runtime_dirs(run_dir: Path) -> tuple[Path, Path]:
    """Return `(runtime_home, private_tmp)`, both mode 0700."""
    runtime_home = ensure_private_dir(run_dir / "runtime-home")
    private_tmp = ensure_private_dir(run_dir / "tmp")
    return runtime_home, private_tmp


def prepare_run_grok_home(run_dir: Path, auth_store: Path) -> Path:
    """Create a private per-run GROK_HOME and copy the minimum auth file."""
    home = ensure_private_dir(per_run_grok_home(run_dir))
    source = auth_store / AUTH_FILENAME
    if source.is_file() and not source.is_symlink():
        copy_auth_file(source, home / AUTH_FILENAME)
    return home


def scrub_per_run_auth(run_dir: Path) -> bool:
    """Remove the per-run auth copy. Keep other files and the persistent store.

    A symlink is unlinked without following it, so this cannot delete the
    persistent auth file by accident.
    """
    path = per_run_grok_home(run_dir) / AUTH_FILENAME
    if not path.is_symlink() and not path.exists():
        return False
    path.unlink()
    return True


def stage_pythonpath(run_dir: Path) -> Path:
    """Copy the Nightshift package into the run so the child need not read the source tree."""
    import nightshift

    package = Path(nightshift.__file__).resolve().parent
    dest_root = ensure_private_dir(run_dir / "pythonpath")
    dest_pkg = dest_root / "nightshift"
    if dest_pkg.exists():
        shutil.rmtree(dest_pkg)
    for path in package.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package)
        target = dest_pkg / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        os.chmod(target, 0o600)
    return dest_root


def apply_runtime_env(env: dict[str, str], *, runtime_home: Path, grok_home: Path, private_tmp: Path) -> dict[str, str]:
    """Replace HOME and point Grok at this run's profile."""
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
    if not path.is_file() or path.is_symlink():
        return {"auth": "absent", "profile_mode": profile_mode, "auth_mode": ""}
    return {"auth": "present", "profile_mode": profile_mode, "auth_mode": _mode(path)}


def auth_bootstrap(profile: Path, source_home: Path | None = None) -> str:
    """Copy only `auth.json` from an existing Grok profile into `profile`.

    The copy is a separate file so later refresh does not rewrite the
    operator's live auth file. Contents are never returned.
    """
    origin = (source_home if source_home is not None else Path.home() / ".grok") / AUTH_FILENAME
    if not origin.is_file() or origin.is_symlink():
        raise NightshiftError("grok auth file is missing; cannot bootstrap", exit_code=1)
    ensure_private_dir(profile)
    destination = profile / AUTH_FILENAME
    copy_auth_file(origin, destination)
    os.chmod(profile, 0o700)
    if profile.parent != profile:
        os.chmod(profile.parent, 0o700)
    return "bootstrapped"


def copy_auth_file(source: Path, destination: Path) -> None:
    """Write one auth file at mode 0600. The destination is not a symlink."""
    if source.is_symlink():
        raise NightshiftError("refusing to copy an auth symlink")
    data = source.read_bytes()
    ensure_private_dir(destination.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(destination, flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(destination, 0o600)
    if destination.is_symlink():
        raise NightshiftError("auth destination must be a regular file")


def _mode(path: Path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))
