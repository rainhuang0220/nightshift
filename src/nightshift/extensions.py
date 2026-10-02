"""Extension surface audit and project-config neutralization.

Untrusted repository files may influence reasoning. They must not silently
grant tools, hooks, plugins, or MCP servers. Neutralization happens only in
the isolated workspace. The source repository is not modified.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

# Relative paths that can contribute executable Grok/Claude/Cursor config.
# AGENTS.md is intentionally absent: it is untrusted instruction text.
_NEUTRALIZE = (
    Path(".grok"),
    Path(".mcp.json"),
    Path(".cursor/mcp.json"),
    Path(".cursor/hooks.json"),
    Path(".cursor/hooks"),
    Path(".claude"),
)

_FORBIDDEN_SOURCE = {"user", "project", "plugin", "claude", "cursor", "configtoml", "local", "marketplace"}
_FORBIDDEN_VENDORS = {"claude", "cursor", "codex"}


@dataclass(frozen=True)
class ExtensionSurfaceAudit:
    ok: bool
    violations: tuple[str, ...]
    counts: dict[str, int]
    untrusted_instructions: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "violations": list(self.violations),
            "counts": dict(self.counts),
            "untrusted_instructions": self.untrusted_instructions,
        }


@dataclass
class NeutralizeResult:
    moved: list[str] = field(default_factory=list)


def _escaping_symlink(workspace: Path, relative: Path) -> Path | None:
    """Return the first symlink on `relative` whose target leaves `workspace`.

    Walking stops at that symlink. Callers unlink it and do not move the
    target, so a clone entry that points at the operator home cannot be
    followed into a real directory.
    """
    current = workspace
    for part in relative.parts:
        current = current / part
        if not current.is_symlink():
            if not current.exists():
                return None
            continue
        try:
            current.resolve().relative_to(workspace)
        except ValueError:
            return current
    return None


def neutralize_project_extensions(workspace: Path, record_dir: Path) -> list[str]:
    """Move extension config out of `workspace` for the provider process.

    The move is temporary. `restore_neutralized_extensions` puts the same
    entries back. A symlink that points outside the workspace is unlinked
    and remembered; its target is not moved or deleted. The source
    repository is not a workspace and is not modified.
    """
    root = workspace.resolve()
    actions: list[dict[str, str]] = []
    moved: list[str] = []
    for relative in _NEUTRALIZE:
        escape = _escaping_symlink(root, relative)
        if escape is not None:
            link = escape.relative_to(root).as_posix()
            actions.append({"path": link, "kind": "symlink", "target": os.readlink(escape)})
            escape.unlink()
            moved.append(link + " (symlink unlinked)")
            continue
        path = workspace / relative
        if not path.exists() and not path.is_symlink():
            continue
        label = relative.as_posix()
        if path.is_symlink():
            actions.append({"path": label, "kind": "symlink", "target": os.readlink(path)})
            path.unlink()
            moved.append(label + " (symlink unlinked)")
            continue
        destination = record_dir / "neutralized" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(destination))
        actions.append({"path": label, "kind": "dir" if destination.is_dir() else "file"})
        moved.append(label)
    if actions:
        manifest = record_dir / "neutralized-manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(actions) + "\n", encoding="utf-8")
    return moved


def restore_neutralized_extensions(workspace: Path, record_dir: Path) -> list[str]:
    """Put neutralized clone entries back so the workspace matches the checkout.

    An entry the provider replaced is left in place. Calling this twice is
    safe: the second call finds the restored path and does nothing.
    """
    if not workspace.is_dir():
        return []
    manifest = record_dir / "neutralized-manifest.json"
    if not manifest.is_file():
        return []
    try:
        actions = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(actions, list):
        return []
    restored: list[str] = []
    for action in reversed(actions):
        if not isinstance(action, dict):
            continue
        relative = str(action.get("path") or "")
        kind = str(action.get("kind") or "")
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            continue
        destination = workspace / relative
        if destination.exists() or destination.is_symlink():
            continue
        if kind == "symlink":
            target = action.get("target")
            if not isinstance(target, str):
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(target)
            restored.append(relative)
            continue
        source = record_dir / "neutralized" / relative
        if not source.exists() and not source.is_symlink():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        restored.append(relative)
    return restored


def project_instruction_names(workspace: Path) -> list[str]:
    names = []
    for relative in ("AGENTS.md", "CLAUDE.md", ".grok/AGENTS.md"):
        path = workspace / relative
        if path.is_file() and not path.is_symlink():
            names.append(relative)
    return names


def audit_payload(data: dict, *, grok_home: Path | None = None) -> ExtensionSurfaceAudit:
    """Fail closed on executable extension surfaces.

    Built-in agents may remain. Project instruction files are counted and are
    not, by themselves, a violation. Operator-global instruction files are.
    Default Claude/Cursor compatibility flags stay enabled even on an empty
    profile, so those flags are not treated as imported servers. Discovered
    Claude, Cursor, or Codex items are violations.
    """
    violations: list[str] = []
    hooks = _as_list(data.get("hooks"))
    skills = _as_list(data.get("skills"))
    plugins = _as_list(data.get("plugins"))
    mcp_servers = _as_list(data.get("mcpServers"))
    agents = _as_list(data.get("agents"))
    instructions = _as_list(data.get("projectInstructions"))
    lsp_servers = _as_list(data.get("lspServers"))
    marketplaces = _as_list(data.get("marketplaces"))
    for hook in hooks:
        violations.append(_hook_violation(hook))
    for skill in skills:
        violation = _skill_violation(skill, grok_home)
        if violation:
            violations.append(violation)
    for plugin in plugins:
        scope = str(plugin.get("scope") or "plugin")
        violations.append(f"{scope} plugin")
    for server in mcp_servers:
        source = _source_type(server)
        vendor = _vendor(server)
        if vendor in _FORBIDDEN_VENDORS:
            violations.append(f"{vendor} mcp")
        else:
            violations.append(f"{source or 'external'} mcp")
    for agent in agents:
        source = _source_type(agent)
        if source != "builtin":
            violations.append(f"{source or 'unknown'} agent")
    untrusted = 0
    for item in instructions:
        scope = str(item.get("scope") or "")
        if scope == "project":
            untrusted += 1
            continue
        violations.append("operator instructions" if scope == "global" else f"{scope or 'unknown'} instructions")
    if lsp_servers:
        violations.append("lsp server")
    if marketplaces:
        violations.append("marketplace")
    permissions = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    sources = permissions.get("sources") or []
    if isinstance(sources, list) and sources:
        violations.append("unexpected permission grant")
    loaded = permissions.get("loaded")
    if isinstance(loaded, int) and loaded > 0:
        violations.append("unexpected permission grant")
    elif isinstance(loaded, (list, dict)) and loaded:
        violations.append("unexpected permission grant")
    counts = {
        "hooks": len(hooks),
        "skills": len(skills),
        "plugins": len(plugins),
        "mcp_servers": len(mcp_servers),
        "agents": len(agents),
        "project_instructions": len(instructions),
        "permission_sources": len(sources) if isinstance(sources, list) else 0,
        "permissions_loaded": loaded if isinstance(loaded, int) else 0,
        "lsp_servers": len(lsp_servers),
        "marketplaces": len(marketplaces),
    }
    unique = tuple(dict.fromkeys(violations))
    return ExtensionSurfaceAudit(
        ok=not unique,
        violations=unique,
        counts=counts,
        untrusted_instructions=untrusted,
    )


def run_inspect(
    env: dict[str, str],
    cwd: Path,
    *,
    timeout: float = 60,
    profile: Path | None = None,
    poll_stop=None,
    on_pid=None,
) -> ExtensionSurfaceAudit:
    """Ask the Grok CLI what it discovers. The caller supplies the sanitized env.

    When `profile` is set, inspect runs under that seatbelt. A contained
    failure is returned to the caller; this function does not widen the profile.
    Either path can be cancelled while the child is still running.
    """
    try:
        if profile is not None:
            from nightshift.containment import contained_run

            code, output = contained_run(
                ["grok", "inspect", "--json"],
                cwd=cwd,
                env=env,
                profile=profile,
                timeout=timeout,
                poll_stop=poll_stop,
                on_pid=on_pid,
            )
        else:
            code, output = _unsandboxed_command(
                ["grok", "inspect", "--json"],
                cwd=cwd,
                env=env,
                timeout=timeout,
                poll_stop=poll_stop,
                on_pid=on_pid,
            )
        if code != 0:
            return _failed("inspect failed")
        payload = _json_object(output)
    except (OSError, subprocess.TimeoutExpired):
        return _failed("inspect failed")
    if payload is None:
        return _failed("inspect output was not json")
    if not isinstance(payload, dict):
        return _failed("inspect output was not an object")
    home = Path(env["GROK_HOME"]) if env.get("GROK_HOME") else None
    return audit_payload(payload, grok_home=home)


def _unsandboxed_command(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    poll_stop=None,
    on_pid=None,
) -> tuple[int, str]:
    """Run a preflight command without a seatbelt, still as its own process group."""
    import time

    from nightshift.providers.base import terminate_process

    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        pgid = None
    if on_pid is not None:
        on_pid(proc.pid, pgid)
    watcher = None
    if poll_stop is not None:
        def _watch() -> None:
            while proc.poll() is None:
                if poll_stop():
                    terminate_process(proc.pid, pgid)
                    return
                time.sleep(0.05)

        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_process(proc.pid, pgid)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
    finally:
        if watcher is not None:
            watcher.join(timeout=1)
    output = (stdout or "") + (stderr or "")
    code = proc.returncode if proc.returncode is not None else 1
    return code, output


def _json_object(text: str) -> dict | None:
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        else:
            return payload if isinstance(payload, dict) else None
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _failed(reason: str) -> ExtensionSurfaceAudit:
    return ExtensionSurfaceAudit(ok=False, violations=(reason,), counts={})


def _hook_violation(hook: dict) -> str:
    vendor = _vendor(hook)
    if vendor in _FORBIDDEN_VENDORS:
        return f"{vendor} hook"
    source = _source_type(hook)
    if source == "plugin":
        return "plugin hook"
    if source == "project":
        return "project hook"
    if source == "user":
        return "user hook"
    return f"{source or 'unknown'} hook"


def _skill_violation(skill: dict, grok_home: Path | None) -> str | None:
    source = _source_type(skill)
    vendor = _vendor(skill)
    if vendor in _FORBIDDEN_VENDORS:
        return f"{vendor} skill"
    if source in {"user", "project", "plugin"} or source in _FORBIDDEN_SOURCE:
        return f"{source} skill"
    if source == "bundled":
        path = _source_path(skill)
        if grok_home is not None and path is not None:
            try:
                path.resolve().relative_to(grok_home.resolve())
                return None
            except ValueError:
                return "bundled skill outside nightshift profile"
        return "bundled skill outside nightshift profile"
    if source == "builtin":
        return None
    return f"{source or 'unknown'} skill"


def _source_type(item: dict) -> str:
    source = item.get("source")
    if isinstance(source, dict):
        return str(source.get("type") or "").lower()
    if isinstance(source, str):
        return source.lower()
    return ""


def _source_path(item: dict) -> Path | None:
    source = item.get("source")
    if isinstance(source, dict) and source.get("path"):
        return Path(str(source["path"]))
    return None


def _vendor(item: dict) -> str:
    if item.get("vendor"):
        return str(item["vendor"]).lower()
    source = item.get("source")
    if isinstance(source, dict) and source.get("vendor"):
        return str(source["vendor"]).lower()
    return ""


def _as_list(value) -> list[dict]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def chmod_tree_private(root: Path) -> None:
    if not root.exists():
        return
    os.chmod(root, 0o700)
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o700)
        for name in filenames:
            os.chmod(Path(dirpath) / name, 0o600)
        del dirnames
