"""Runtime paths and optional TOML configuration.

Relative path settings resolve against the Nightshift root. The root is
``--root``, then ``NIGHTSHIFT_ROOT``, then the process working directory.
"""

from __future__ import annotations

import math
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

    def validate_source_paths(self, repository: Path) -> None:
        source = repository.expanduser().resolve()
        for control in (self.state_dir, self.runs_dir, self.worktrees_dir):
            path = control.resolve()
            if source == path or source in path.parents or path in source.parents:
                raise UsageError('source repository and control/workspace paths must not overlap')


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
    _validate_config(data)
    paths = data.get("paths") or {}
    supervisor = data.get("supervisor") or {}
    provider = data.get("provider") or {}
    policy = data.get("policy") or {}
    containment = data.get("containment") or {}
    state_dir = _under_root(chosen_root, paths.get("state_dir") or "state")
    runs_dir = _under_root(chosen_root, paths.get("runs_dir") or "runs")
    worktrees_dir = _under_root(chosen_root, paths.get("worktrees_dir") or "worktrees")
    locations = [state_dir, runs_dir, worktrees_dir]
    for index, left in enumerate(locations):
        if left == chosen_root or left == Path.home().resolve() or left == Path('/'):
            raise UsageError("state, run and workspace paths must be dedicated directories")
        for right in locations[index + 1:]:
            if left == right or left in right.parents or right in left.parents:
                raise UsageError("state, run and workspace directories must not overlap")
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
    for read_root in read_roots:
        for control in locations:
            if read_root == control or read_root in control.parents or control in read_root.parents:
                raise UsageError('containment.read_roots must not overlap private control directories')
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
    if value is None or value == []:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() or '\x00' in item
                                          for item in value):
        raise UsageError("containment.read_roots must be a list of path strings")
    return tuple(_under_root(root, item) for item in value)


def _under_root(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


_CONFIG_KEYS = {
    "paths": {"state_dir", "runs_dir", "worktrees_dir"},
    "supervisor": {"concurrency", "poll_interval_seconds"},
    "provider": {"default", "max_turns", "permission_mode"},
    "policy": {"destroy_failed_worktrees"},
    "containment": {"read_roots"},
}


def _validate_config(data: dict) -> None:
    if set(data) - set(_CONFIG_KEYS) - {"schema_version"}:
        raise UsageError("unknown config section")
    if "schema_version" in data and (type(data["schema_version"]) is not int or data["schema_version"] != 1):
        raise UsageError("config schema_version must be 1")
    for section, keys in _CONFIG_KEYS.items():
        value = data.get(section, {})
        if not isinstance(value, dict) or set(value) - keys:
            raise UsageError(f"invalid or unknown fields in config.{section}")
        for key, item in value.items():
            label = f"{section}.{key}"
            if key in {"concurrency", "max_turns"}:
                if type(item) is not int or not 1 <= item <= 1000:
                    raise UsageError(f"{label} must be an integer in 1..1000")
            elif key == "poll_interval_seconds":
                if type(item) not in {int, float} or not math.isfinite(item) or item <= 0:
                    raise UsageError(f"{label} must be a finite positive number")
            elif key == "destroy_failed_worktrees":
                if type(item) is not bool:
                    raise UsageError(f"{label} must be a boolean")
            elif key == "read_roots":
                _read_roots(Path.cwd(), item)
            elif not isinstance(item, str) or not item.strip() or "\x00" in item:
                raise UsageError(f"{label} must be a nonempty path/string")
    provider = data.get("provider", {})
    if provider.get("default", "grok") not in {"fake", "grok"}:
        raise UsageError("provider.default must be fake or grok")
    if provider.get("permission_mode", "dontAsk") != "dontAsk":
        raise UsageError("provider.permission_mode must be dontAsk")
