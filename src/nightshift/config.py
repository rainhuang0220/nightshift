"""Runtime paths and optional TOML configuration.

Relative path settings resolve against the Nightshift root. The root is
``--root``, then ``NIGHTSHIFT_ROOT``, then the process working directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from nightshift.priv import ensure_private_dir

import tomllib

from nightshift.models import UsageError


@dataclass(frozen=True)
class Config:
    root: Path
    state_dir: Path
    runs_dir: Path
    worktrees_dir: Path
    db_path: Path
    concurrency: int = 1
    poll_interval_seconds: float = 2.0
    default_provider: str = "grok"
    max_turns: int = 40
    permission_mode: str = "dontAsk"
    destroy_failed_worktrees: bool = False
    explicit_read_roots: tuple[Path, ...] = ()

    def ensure_dirs(self) -> None:
        ensure_private_dir(self.state_dir)
        ensure_private_dir(self.runs_dir)
        ensure_private_dir(self.worktrees_dir)
        from nightshift.runtime import ensure_auth_store

        ensure_auth_store(self.state_dir)

    def auth_store(self) -> Path:
        from nightshift.runtime import auth_store_dir

        return auth_store_dir(self.state_dir)


def load_config(root: Path | None = None, config_path: Path | None = None) -> Config:
    env_root = os.environ.get("NIGHTSHIFT_ROOT")
    chosen_root = (root or (Path(env_root) if env_root else Path.cwd())).expanduser().resolve()
    path = _discover_config(chosen_root, config_path)
    data: dict = {}
    if path is not None:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise UsageError(f"invalid config {path}: {exc}") from exc
    paths = data.get("paths") or {}
    supervisor = data.get("supervisor") or {}
    provider = data.get("provider") or {}
    policy = data.get("policy") or {}
    containment = data.get("containment") or {}
    state_dir = _under_root(chosen_root, paths.get("state_dir") or "state")
    runs_dir = _under_root(chosen_root, paths.get("runs_dir") or "runs")
    worktrees_dir = _under_root(chosen_root, paths.get("worktrees_dir") or "worktrees")
    concurrency = int(supervisor.get("concurrency", 1))
    if concurrency < 1:
        raise UsageError("supervisor.concurrency must be >= 1")
    poll = float(supervisor.get("poll_interval_seconds", 2))
    if poll <= 0:
        raise UsageError("supervisor.poll_interval_seconds must be > 0")
    permission_mode = str(provider.get("permission_mode", "dontAsk"))
    if permission_mode in {"bypassPermissions", "always-approve", "always_approve"}:
        raise UsageError(
            "refusing permission_mode that bypasses approvals; Nightshift uses dontAsk plus deny rules"
        )
    read_roots = _read_roots(chosen_root, containment.get("read_roots") or [])
    return Config(
        root=chosen_root,
        state_dir=state_dir,
        runs_dir=runs_dir,
        worktrees_dir=worktrees_dir,
        db_path=state_dir / "nightshift.db",
        concurrency=concurrency,
        poll_interval_seconds=poll,
        default_provider=str(provider.get("default", "grok")),
        max_turns=int(provider.get("max_turns", 40)),
        permission_mode=permission_mode,
        destroy_failed_worktrees=bool(policy.get("destroy_failed_worktrees", False)),
        explicit_read_roots=read_roots,
    )


def _discover_config(root: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if not path.is_file():
            raise UsageError(f"config not found: {path}")
        return path
    env = os.environ.get("NIGHTSHIFT_CONFIG")
    if env:
        path = Path(env).expanduser().resolve()
        if not path.is_file():
            raise UsageError(f"config not found: {path}")
        return path
    for candidate in (root / "config" / "nightshift.toml", root / "nightshift.toml"):
        if candidate.is_file():
            return candidate
    return None


def _read_roots(root: Path, value) -> tuple[Path, ...]:
    """Operator-configured toolchain roots. A job prompt cannot add these."""
    if value in (None, "", []):
        return ()
    if not isinstance(value, list) or any(isinstance(item, bool) or not isinstance(item, str) for item in value):
        raise UsageError("containment.read_roots must be a list of path strings")
    return tuple(_under_root(root, item) for item in value)


def _under_root(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()
