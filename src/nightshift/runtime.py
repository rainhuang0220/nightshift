"""Isolated Grok HOME and the minimum authentication bootstrap.

The persistent auth store and the per-run GROK_HOME are different directories.
`nightshift auth grok bootstrap` copies only `auth.json` into the store.
Each launch copies that file into a new private GROK_HOME and deletes the
copy when the run ends. The store is never deleted here, and file contents
are never printed.
"""

from __future__ import annotations

import errno
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


def prepare_run_grok_home(run_dir: Path, auth_store: Path, *, copy_auth: bool = True) -> Path:
    """Create a private per-run GROK_HOME.

    Provider launches copy the minimum auth file. Verification does not.
    A verification child can read the run directory, so leaving the copy
    there would publish the credential to that command.

    A symlink at `grok-home` is removed and not followed. The persistent
    store is never the directory this function writes.
    """
    home = per_run_grok_home(run_dir)
    _drop_directory_symlink(home)
    _ensure_real_private_dir(home)
    if not copy_auth:
        _unlink_directory_entry(home, AUTH_FILENAME)
        return home
    source = auth_store / AUTH_FILENAME
    info = _lstat(source)
    if info is not None and stat.S_ISREG(info.st_mode):
        copy_auth_file(source, home / AUTH_FILENAME)
    return home


def scrub_per_run_auth(run_dir: Path) -> bool:
    """Remove the per-run auth copy. Keep other files and the persistent store.

    When `grok-home` itself is a symlink, only that link is removed. The
    function does not follow it, and it does not raise: callers in `finally`
    still have to release locks.
    """
    home = per_run_grok_home(run_dir)
    try:
        if _drop_directory_symlink(home):
            return True
        return _unlink_directory_entry(home, AUTH_FILENAME)
    except OSError:
        try:
            return _drop_directory_symlink(home)
        except OSError:
            return False


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
    """Write one auth file at mode 0600. The destination is not a symlink.

    The parent directory is opened with `O_NOFOLLOW`. A symlink parent is
    refused, and a symlink final component is unlinked before a new file
    is created. Neither step follows the link into another directory.
    """
    source_info = _lstat(source)
    if source_info is None or not stat.S_ISREG(source_info.st_mode):
        raise NightshiftError("refusing to copy an auth symlink")
    data = source.read_bytes()
    parent = destination.parent
    parent_info = _lstat(parent)
    if parent_info is None:
        parent.mkdir(parents=True, exist_ok=True)
        parent_info = _lstat(parent)
    if parent_info is None or stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise NightshiftError("refusing to copy auth through a symlinked directory")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(parent_fd, 0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
        try:
            fd = os.open(destination.name, flags, 0o600, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno != errno.ELOOP:
                raise
            os.unlink(destination.name, dir_fd=parent_fd)
            fd = os.open(
                destination.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
        try:
            os.write(fd, data)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _mode(path: Path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))


def _lstat(path: Path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _drop_directory_symlink(path: Path) -> bool:
    """Remove `path` when it is a symlink. The target is left in place."""
    info = _lstat(path)
    if info is None or not stat.S_ISLNK(info.st_mode):
        return False
    os.unlink(path)
    return True


def _ensure_real_private_dir(path: Path) -> None:
    """Create `path` as a real directory and set mode 0700 without following a symlink."""
    _drop_directory_symlink(path)
    info = _lstat(path)
    if info is None:
        path.mkdir(parents=True, exist_ok=True)
        info = _lstat(path)
    if info is not None and stat.S_ISLNK(info.st_mode):
        os.unlink(path)
        path.mkdir(parents=True, exist_ok=True)
        info = _lstat(path)
    if info is None or stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise NightshiftError("refusing to use a symlinked directory for auth")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def _unlink_directory_entry(parent: Path, name: str) -> bool:
    """Unlink one entry in a real directory. A symlink entry is not followed."""
    info = _lstat(parent)
    if info is None:
        return False
    if stat.S_ISLNK(info.st_mode):
        raise OSError(errno.ELOOP, "parent is a symlink")
    if not stat.S_ISDIR(info.st_mode):
        return False
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            return False
        return True
    finally:
        os.close(fd)
