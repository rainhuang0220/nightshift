"""Private directories and files for runtime state.

Modes are 0700 for directories and 0600 for files that may hold logs or
credentials. Opens use O_NOFOLLOW and O_CLOEXEC and require a regular file.
A symlink is left in place and the call fails closed.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from nightshift.models import NightshiftError


def ensure_private_dir(path: Path) -> Path:
    """Create a real directory at mode 0700. A final symlink is refused."""
    if path.is_symlink():
        raise NightshiftError(f"refusing to use a symlinked directory: {path.name}")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise NightshiftError(f"refusing to use a symlinked directory: {path.name}")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise NightshiftError(f"refusing to use a symlinked directory: {path.name}")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return path


def open_private(path: Path, *, append: bool = False):
    """Open a regular text file at mode 0600. Symlinks are not followed."""
    fd = _open_regular(path, append=append)
    return os.fdopen(fd, "a" if append else "w", encoding="utf-8")


def write_private_text(path: Path, text: str) -> None:
    with open_private(path) as handle:
        handle.write(text)


def open_private_binary(path: Path):
    fd = _open_regular(path, append=True)
    return os.fdopen(fd, "ab", buffering=0)


def read_private_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Bounded read that cannot follow a final symlink or block opening a FIFO."""
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NightshiftError(f'refusing to read a non-regular control file: {path.name}')
        with os.fdopen(fd, 'rb') as handle:
            fd = -1
            return handle.read(max_bytes)
    finally:
        if fd >= 0:
            os.close(fd)


def chmod_private_file(path: Path) -> None:
    """Set mode 0600 on a regular file. A symlink is refused and left alone."""
    info = _lstat(path)
    if info is None:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise NightshiftError(f"refusing to chmod a non-regular file: {path.name}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NightshiftError(f"refusing to chmod a non-regular file: {path.name}")
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _open_regular(path: Path, *, append: bool) -> int:
    parent = path.parent
    if not parent.exists():
        ensure_private_dir(parent)
    if parent.is_symlink() or not parent.is_dir():
        raise NightshiftError(f"refusing to open through a symlinked directory: {path.name}")
    info = _lstat(path)
    if info is not None and (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)):
        raise NightshiftError(f"refusing to follow a non-regular control file: {path.name}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    flags |= os.O_APPEND if append else os.O_TRUNC
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EEXIST}:
                raise NightshiftError(f"refusing to follow a non-regular control file: {path.name}") from exc
            raise
    finally:
        os.close(parent_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NightshiftError(f"refusing to follow a non-regular control file: {path.name}")
        os.fchmod(fd, 0o600)
    except Exception:
        os.close(fd)
        raise
    return fd


def _lstat(path: Path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
