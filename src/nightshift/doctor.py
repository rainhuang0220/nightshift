"""Local environment checks. Does not launch a model."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys

from nightshift.config import Config
from nightshift.db import Database
from nightshift.gitutil import GIT_BIN


def run_doctor(config: Config) -> tuple[int, list[str]]:
    lines: list[str] = []
    ok = True
    version = platform.python_version()
    if sys.version_info >= (3, 11):
        lines.append(f"[pass] Python {version} (>= 3.11)")
    else:
        ok = False
        lines.append(f"[fail] Python {version} (>= 3.11 required)")
    try:
        config.ensure_dirs()
        probe = config.state_dir / ".doctor-write"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        lines.append("[pass] writable state")
    except OSError as exc:
        ok = False
        lines.append(f"[fail] writable state: {exc.__class__.__name__}")
    try:
        db = Database(config.db_path)
        db.list_runs()
        with db._lock:
            row = db._conn.execute("PRAGMA integrity_check").fetchone()
        db.close()
        if row is None or row[0] != "ok":
            ok = False
            lines.append("[fail] SQLite: integrity check failed")
        else:
            lines.append("[pass] SQLite")
    except Exception as exc:
        ok = False
        lines.append(f"[fail] SQLite: {exc.__class__.__name__}")
    grok = shutil.which("grok")
    if not grok:
        ok = False
        lines.append("[fail] Grok CLI: not on PATH")
    else:
        try:
            proc = subprocess.run(
                [grok, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            ok = False
            lines.append("[fail] Grok CLI: version command failed")
        else:
            detail = (proc.stdout or proc.stderr or "").strip().splitlines()
            shown = detail[0] if detail else "unknown"
            if proc.returncode == 0:
                lines.append(f"[pass] Grok CLI: {shown}")
            else:
                ok = False
                lines.append(f"[fail] Grok CLI: {shown}")
    if shutil.which(GIT_BIN) or shutil.which("git"):
        lines.append("[pass] git")
    else:
        ok = False
        lines.append("[fail] git: not on PATH")
    return (0 if ok else 1), lines
