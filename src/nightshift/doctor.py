"""Local environment checks. Does not launch a model."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys

from pathlib import Path

from nightshift.config import Config
from nightshift.db import Database
from nightshift.gitutil import GIT_BIN


def read_grok_version(binary: str) -> tuple[str, str]:
    """Return `(version line, how it ran)`.

    `--version` is tried inside a narrow seatbelt first. A failure keeps the
    unsandboxed command so doctor and run metadata still record the version.
    The seatbelt is not widened when that happens.
    """
    contained, seatbelt_error = _version_inside_seatbelt(binary)
    if contained:
        return contained, "seatbelt"
    outside, outside_error = _version_outside(binary)
    detail = seatbelt_error or outside_error or "version command failed"
    flat = " ".join(detail.split())
    return outside, "outside-seatbelt: " + flat[:200]


def _version_outside(binary: str) -> tuple[str, str]:
    try:
        proc = subprocess.run(
            [binary, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", exc.__class__.__name__
    text = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        return "", text or f"exit {proc.returncode}"
    return (text.splitlines()[0] if text else ""), ""


def _version_inside_seatbelt(binary: str) -> tuple[str, str]:
    import tempfile

    from nightshift.containment import build_read_policy, contained_run, sandbox_available, write_profile

    if not sandbox_available():
        return "", "sandbox-exec is not available"
    try:
        with tempfile.TemporaryDirectory(prefix="nightshift-version-") as raw:
            root = Path(raw)
            home = root / "home"
            tmp = root / "tmp"
            source = root / "source"
            home.mkdir()
            tmp.mkdir()
            source.mkdir()
            profile = write_profile(
                root / "version.sb",
                writable=[home, tmp],
                network=False,
                read_policy=build_read_policy(
                    runtime_roots=[home, tmp],
                    source_root=source,
                ),
            )
            code, output = contained_run(
                [binary, "--version"],
                cwd=home,
                env={
                    "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
                    "HOME": str(home),
                    "TMPDIR": str(tmp),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                profile=profile,
                timeout=15,
            )
    except Exception as exc:
        return "", exc.__class__.__name__ + ": " + str(exc)
    if code != 0:
        return "", f"exit {code}: {output.strip()}"
    for line in output.splitlines():
        cleaned = line.strip()
        if cleaned and "operation not permitted" not in cleaned.lower():
            return cleaned, ""
    return "", output.strip() or "empty version output"


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
        shown, how = read_grok_version(grok)
        if shown:
            lines.append(f"[pass] Grok CLI: {shown} ({how})")
        else:
            ok = False
            lines.append(f"[fail] Grok CLI: {how}")
    if shutil.which(GIT_BIN) or shutil.which("git"):
        lines.append("[pass] git")
    else:
        ok = False
        lines.append("[fail] git: not on PATH")
    return (0 if ok else 1), lines
