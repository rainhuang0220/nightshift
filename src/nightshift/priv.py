"""Private directories and files for runtime state.

Modes are 0700 for directories and 0600 for files that may hold logs or
credentials. This is hygiene, not a cryptographic boundary.
"""

from __future__ import annotations

import os
from pathlib import Path


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def open_private(path: Path, *, append: bool = False):
    """Open a text file created at mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    os.chmod(path, 0o600)
    return os.fdopen(fd, "a" if append else "w", encoding="utf-8")


def write_private_text(path: Path, text: str) -> None:
    with open_private(path) as handle:
        handle.write(text)


def open_private_binary(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.chmod(path, 0o600)
    return os.fdopen(fd, "ab", buffering=0)


def chmod_private_file(path: Path) -> None:
    if path.exists():
        os.chmod(path, 0o600)
