"""Seatbelt profiles for provider and verification processes.

Grok permission rules and the PATH shim are not this boundary. `sandbox-exec`
is the process and filesystem containment used on this machine. If it is
missing, Nightshift refuses the run instead of executing unsandboxed.

Read access is deny-by-default. The profile allows required system and
toolchain reads, denies the operator home and the original source checkout,
then re-allows only the Nightshift roots that must remain visible. The last
matching seatbelt rule wins, so that re-allow is written after the denies.
`prompt.md` is not an input to this policy.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from nightshift.policy import scrub_text
from nightshift.priv import write_private_text
from nightshift.providers.base import terminate_process

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")

_SYSTEM_CANDIDATES = (
    "/usr",
    "/bin",
    "/sbin",
    "/System",
    "/Library",
    "/dev",
    "/opt/homebrew",
    "/opt/local",
    "/private/etc",
    "/private/var/select",
    "/Library/Developer/CommandLineTools",
    "/Applications/Xcode.app",
    "/usr/local",
)

# Directory symlinks the kernel walks before a resolved subpath rule can match.
# These are literals, not subpaths: `(subpath "/var")` would expose all of /private/var.
_TRAVERSAL_ANCHORS = (
    "/",
    "/var",
    "/private",
    "/private/var",
    "/etc",
    "/tmp",
    "/Users",
    "/Applications",
    "/opt",
)


class ContainmentError(Exception):
    pass


@dataclass(frozen=True)
class ContainmentReadPolicy:
    """Paths the provider may read. The operator home is denied, not listed.

    `system_roots` are OS and package-manager prefixes. `runtime_roots` are
    the isolated workspace, run directory, runtime HOME, and per-run
    GROK_HOME. `tool_roots` are resolved interpreter and Grok executable
    paths. `explicit_read_roots` come from Nightshift config, never from a
    job prompt. `source_root` is the original checkout and stays unreadable
    except where a runtime root is re-allowed afterwards.
    """

    system_roots: tuple[Path, ...]
    runtime_roots: tuple[Path, ...]
    tool_roots: tuple[Path, ...]
    operator_denied_root: Path | None
    explicit_read_roots: tuple[Path, ...]
    source_root: Path | None = None


def sandbox_available() -> bool:
    return SANDBOX_EXEC.is_file()


def default_system_roots() -> tuple[Path, ...]:
    found: list[Path] = []
    seen: set[str] = set()
    for raw in _SYSTEM_CANDIDATES:
        path = Path(raw)
        if not path.exists():
            continue
        resolved = path.resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        found.append(resolved)
    return tuple(found)


def default_tool_roots() -> tuple[Path, ...]:
    """Resolved executables the child must be able to open.

    The Grok install directory is included only when it is not the operator
    `.grok` profile. Authentication, skills, and sessions stay denied.
    """
    candidates: list[Path] = []
    grok = shutil.which("grok")
    if grok:
        link = Path(grok)
        candidates.append(link)
        resolved = link.resolve()
        candidates.append(resolved)
        home_grok = (Path.home() / ".grok").resolve()
        for directory in (link.parent, resolved.parent):
            resolved_dir = directory.resolve()
            if resolved_dir != home_grok:
                candidates.append(directory)
    candidates.append(Path(sys.executable))
    candidates.append(Path(sys.executable).resolve())
    python3 = shutil.which("python3")
    if python3:
        candidates.append(Path(python3))
        candidates.append(Path(python3).resolve())
    return _unique_paths(candidates)


def build_read_policy(
    *,
    runtime_roots: list[Path],
    source_root: Path | None = None,
    operator_home: Path | None = None,
    explicit_read_roots: list[Path] | None = None,
    tool_roots: list[Path] | None = None,
    system_roots: list[Path] | None = None,
) -> ContainmentReadPolicy:
    """Assemble a read policy. This function does not read a prompt."""
    home = (operator_home if operator_home is not None else Path.home()).expanduser().resolve()
    explicit = [_resolve(path) for path in (explicit_read_roots or [])]
    for path in explicit:
        if path == home:
            raise ContainmentError("refusing an explicit read root that is the operator home")
    runtime = [_resolve(path) for path in runtime_roots]
    for path in runtime:
        if path == home:
            raise ContainmentError("refusing a runtime root that is the operator home")
    return ContainmentReadPolicy(
        system_roots=tuple(system_roots) if system_roots is not None else default_system_roots(),
        runtime_roots=tuple(runtime),
        tool_roots=tuple(tool_roots) if tool_roots is not None else default_tool_roots(),
        operator_denied_root=home,
        explicit_read_roots=tuple(explicit),
        source_root=_resolve(source_root) if source_root is not None else None,
    )


def write_profile(
    path: Path,
    *,
    writable: list[Path],
    network: bool,
    read_policy: ContainmentReadPolicy | None = None,
    source_root: Path | None = None,
    read_deny: list[Path] | None = None,
) -> Path:
    """Write a deny-default profile. Paths are resolved so /var aliases match."""
    if not sandbox_available():
        raise ContainmentError("sandbox-exec is not available; refusing to run unsandboxed")
    policy = read_policy
    if policy is None:
        policy = build_read_policy(runtime_roots=list(writable), source_root=source_root)
    elif source_root is not None and policy.source_root is None:
        policy = ContainmentReadPolicy(
            system_roots=policy.system_roots,
            runtime_roots=policy.runtime_roots,
            tool_roots=policy.tool_roots,
            operator_denied_root=policy.operator_denied_root,
            explicit_read_roots=policy.explicit_read_roots,
            source_root=_resolve(source_root),
        )
    for item in writable:
        _resolve(item).mkdir(parents=True, exist_ok=True)
    lines = render_profile(policy, writable=writable, network=network, extra_denies=read_deny or [])
    write_private_text(path, "\n".join(lines) + "\n")
    return path


def render_profile(
    policy: ContainmentReadPolicy,
    *,
    writable: list[Path],
    network: bool,
    extra_denies: list[Path] | None = None,
) -> list[str]:
    """Return seatbelt rules in evaluation order.

    Order is deny-default, system and tool reads, operator-home deny,
    original-source deny, re-allow of runtime and tool roots, then writes.
    """
    lines = [
        "(version 1)",
        "; nightshift: deny default, then allow only named read roots",
        "(deny default)",
        "(allow process*)",
        "(allow signal)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow ipc-posix*)",
        "(allow file-ioctl)",
        '(allow file-read* (literal "/dev/null"))',
        '(allow file-write* (literal "/dev/null"))',
        '(allow file-read* (literal "/dev/dtracehelper"))',
        '(allow file-write* (literal "/dev/dtracehelper"))',
        '(allow file-read* (literal "/dev/random"))',
        '(allow file-read* (literal "/dev/urandom"))',
        '(allow file-read* (literal "/dev/zero"))',
        "; traversal anchors are literals so a symlink root is not a subpath allow",
    ]
    lines.extend(_traversal_literal_lines())
    lines.append("; system and toolchain reads")
    for root in policy.system_roots:
        lines.extend(_allow_read_lines(root))
    lines.append("; tool reads, including a resolved Grok executable")
    for root in policy.tool_roots:
        lines.extend(_allow_read_lines(root))
    lines.append("; explicit operator-configured read roots")
    for root in policy.explicit_read_roots:
        lines.extend(_allow_read_lines(root))
    if policy.operator_denied_root is not None:
        lines.append("; deny the operator home after system allows")
        lines.extend(_deny_tree_lines(policy.operator_denied_root))
    if policy.source_root is not None:
        lines.append("; deny the original source checkout")
        lines.extend(_deny_tree_lines(policy.source_root))
    lines.append("; re-allow Nightshift roots that live under a denied parent")
    reallow: list[Path] = []
    reallow.extend(policy.runtime_roots)
    reallow.extend(policy.tool_roots)
    reallow.extend(policy.explicit_read_roots)
    lines.extend(_ancestor_metadata_lines(reallow))
    seen: set[str] = set()
    home = _resolve(policy.operator_denied_root) if policy.operator_denied_root is not None else None
    for root in reallow:
        forms = _path_forms(root)
        if home is not None and any(_resolve(form) == home for form in forms):
            raise ContainmentError("refusing to re-allow the operator home")
        for rule in _allow_read_lines(root):
            if rule in seen:
                continue
            seen.add(rule)
            lines.append(rule)
    lines.append("; writable roots after the home and source write denies")
    for item in writable:
        lines.extend(_allow_write_lines(item))
    for item in extra_denies or []:
        lines.extend(_deny_tree_lines(item))
    lines.append("; network is separate from filesystem read containment")
    lines.append("(allow network*)" if network else "(deny network*)")
    return lines


def profile_has_global_file_read(text: str) -> bool:
    """True when a profile grants file-read without a path filter."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in {"(allow file-read*)", "(allow file-read* )"}:
            return True
    return False


def contained_argv(argv: list[str], profile: Path | None) -> list[str]:
    if profile is None:
        return list(argv)
    if not sandbox_available():
        raise ContainmentError("sandbox-exec is not available; refusing to run unsandboxed")
    return [str(SANDBOX_EXEC), "-f", str(profile), *argv]


def contained_run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    profile: Path,
    timeout: float,
) -> tuple[int, str]:
    """Run argv under the profile. Timeout kills the child process group."""
    full = contained_argv(argv, profile)
    proc = subprocess.Popen(
        full,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        pgid = _pgid(proc.pid)
        terminate_process(proc.pid, pgid)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        output = scrub_text((stdout or "") + (stderr or "") + "verification timed out\n")
        return 124, output
    output = scrub_text((stdout or "") + (stderr or ""))
    code = proc.returncode if proc.returncode is not None else 1
    return code, output


def _ancestor_metadata_lines(roots: list[Path]) -> list[str]:
    """Allow `stat` of parents above a re-allowed root.

    Grok walks each path component and treats EPERM as fatal. A metadata
    literal lets that walk succeed. It does not grant directory listing or
    file contents. `subpath` on `/private/var/folders` would.
    """
    lines: list[str] = []
    seen: set[str] = set()
    anchors = set(_TRAVERSAL_ANCHORS)
    lines.append("; ancestor metadata only; contents stay denied")
    for root in roots:
        for form in _var_aliases(root):
            current = form.parent
            while str(current) not in anchors and current != current.parent:
                rule = f'(allow file-read-metadata (literal "{_escape(current)}"))'
                if rule not in seen:
                    seen.add(rule)
                    lines.append(rule)
                current = current.parent
    return lines


def _var_aliases(path: Path) -> tuple[Path, ...]:
    """Include the `/var` and `/private/var` spellings of one path."""
    forms = list(_path_forms(path))
    extras: list[Path] = []
    for form in forms:
        text = str(form)
        alias: Path | None = None
        if text.startswith("/private/var/"):
            alias = Path("/var" + text[len("/private/var") :])
        elif text.startswith("/var/"):
            alias = Path("/private/var" + text[len("/var") :])
        if alias is not None and alias not in forms and alias not in extras:
            extras.append(alias)
    return tuple(forms + extras)


def _traversal_literal_lines() -> list[str]:
    lines: list[str] = []
    for raw in _TRAVERSAL_ANCHORS:
        path = Path(raw)
        if raw != "/" and not path.exists() and not path.is_symlink():
            continue
        lines.append(f'(allow file-read* (literal "{_escape(path)}"))')
    return lines


def _allow_read_lines(path: Path) -> list[str]:
    """Allow one root under both its symlink path and its resolved path.

    A symlink such as `/var` is always a literal. `subpath` on that link would
    grant the whole target tree.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for form in _path_forms(path):
        if str(form) in _TRAVERSAL_ANCHORS:
            kind = "literal"
        else:
            kind = "literal" if _is_file_rule(form) else "subpath"
        rule = f'(allow file-read* ({kind} "{_escape(form)}"))'
        if rule in seen:
            continue
        seen.add(rule)
        lines.append(rule)
    return lines


def _allow_write_lines(path: Path) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for form in _path_forms(path):
        if str(form) in _TRAVERSAL_ANCHORS:
            raise ContainmentError(f"refusing to grant writes under {form}")
        rule = f'(allow file-write* (subpath "{_escape(form)}"))'
        if rule in seen:
            continue
        seen.add(rule)
        lines.append(rule)
    return lines


def _deny_tree_lines(path: Path) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for form in _path_forms(path):
        for op in ("file-read*", "file-write*"):
            rule = f'(deny {op} (subpath "{_escape(form)}"))'
            if rule in seen:
                continue
            seen.add(rule)
            lines.append(rule)
    return lines


def _is_file_rule(path: Path) -> bool:
    """Files and links to files use `literal`. Directories use `subpath`."""
    absolute = _absolute(path)
    if absolute.is_symlink():
        return absolute.resolve().is_file()
    return absolute.is_file()


def _absolute(path: Path) -> Path:
    expanded = Path(path).expanduser()
    if expanded.is_absolute():
        return expanded
    return expanded.resolve()


def _resolve(path: Path) -> Path:
    return Path(path).expanduser().resolve()


def _path_forms(path: Path) -> tuple[Path, ...]:
    """Return the absolute path and, when it differs, the symlink-resolved path."""
    absolute = _absolute(path)
    try:
        resolved = absolute.resolve()
    except OSError:
        resolved = absolute
    if resolved == absolute:
        return (absolute,)
    return (absolute, resolved)


def _unique_paths(paths: list[Path]) -> tuple[Path, ...]:
    found: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        absolute = _absolute(path)
        key = str(absolute)
        if key in seen:
            continue
        seen.add(key)
        found.append(absolute)
        resolved = absolute.resolve()
        resolved_key = str(resolved)
        if resolved_key not in seen:
            seen.add(resolved_key)
            found.append(resolved)
    return tuple(found)


def _escape(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def _pgid(pid: int) -> int | None:
    try:
        group = os.getpgid(pid)
    except ProcessLookupError:
        return None
    if group == os.getpgrp():
        return None
    return group
