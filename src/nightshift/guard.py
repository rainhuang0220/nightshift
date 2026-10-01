"""PATH shims for provider and verification subprocesses.

The shim is a hard control for commands resolved through PATH. An absolute
path such as /usr/bin/git bypasses it. Unknown git global options fail closed.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

READ_ONLY_GIT = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "rev-parse",
        "ls-files",
        "blame",
        "describe",
        "merge-base",
        "shortlog",
        "check-ignore",
        "cat-file",
        "ls-tree",
        "show-ref",
        "rev-list",
        "name-rev",
        "version",
        "help",
        "for-each-ref",
        "count-objects",
        "check-attr",
        "var",
        "grep",
    }
)

WORKSPACE_GIT = frozenset(
    {
        "add",
        "commit",
        "restore",
        "checkout",
        "switch",
        "branch",
        "merge",
        "rebase",
        "cherry-pick",
        "revert",
        "stash",
        "mv",
        "rm",
        "apply",
        "am",
        "reset",
        "tag",
        "notes",
        "bisect",
        "format-patch",
        "bundle",
    }
)

_GIT_BOOL_OPTS = frozenset(
    {
        "--no-pager",
        "--paginate",
        "--bare",
        "--literal-pathspecs",
        "--no-optional-locks",
        "--no-replace-objects",
        "--version",
        "--help",
        "-P",
    }
)
_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--namespace", "--super-prefix", "--list-cmds"})
_ALWAYS_DENY_GIT = frozenset(
    {
        "push",
        "clean",
        "worktree",
        "daemon",
        "send-email",
        "request-pull",
        "filter-branch",
        "fast-export",
        "fast-import",
        "update-server-info",
        "upload-pack",
        "receive-pack",
        "http-backend",
        "shell",
        "remote",
        "submodule",
        "clone",
        "init",
        "fetch",
        "pull",
        "archive",
        "gc",
        "prune",
        "repack",
        "config",
    }
)

_GH_ALLOWED = (
    ("pr", "view"),
    ("pr", "list"),
    ("pr", "status"),
    ("pr", "diff"),
    ("pr", "checks"),
    ("issue", "view"),
    ("issue", "list"),
    ("issue", "status"),
    ("repo", "view"),
    ("status",),
    ("help",),
)


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str


@dataclass(frozen=True)
class ParsedGit:
    subcommand: str
    rest: tuple[str, ...]
    dash_c: tuple[str, ...]
    work_tree: str | None
    denied: str | None


def decide(
    tool: str,
    argv: list[str],
    *,
    cwd: Path,
    workspace: Path,
    source: Path | None,
) -> Decision:
    name = tool.lower()
    if name == "sudo":
        return Decision(False, "sudo is not allowed")
    if name == "kaggle":
        return Decision(False, "kaggle is not allowed")
    if name == "gh":
        return _decide_gh(argv)
    if name == "npm":
        return _decide_publish(argv, "npm publish")
    if name == "twine":
        return _decide_twine(argv)
    if name == "git":
        return _decide_git(argv, cwd=cwd, workspace=workspace, source=source)
    return Decision(False, f"{tool} is not allowed through the Nightshift guard")


def _decide_publish(argv: list[str], what: str) -> Decision:
    args = _skip_options(argv)
    if args and args[0] == "publish":
        return Decision(False, f"{what} is not allowed")
    return Decision(True, "allowed")


def _decide_twine(argv: list[str]) -> Decision:
    args = _skip_options(argv)
    if args and args[0] == "upload":
        return Decision(False, "twine upload is not allowed")
    return Decision(True, "allowed")


def _decide_gh(argv: list[str]) -> Decision:
    args = _skip_options(argv, value_opts={"-R", "--repo", "--hostname", "-e"})
    if not args:
        return Decision(False, "missing gh command")
    for allowed in _GH_ALLOWED:
        if tuple(args[: len(allowed)]) == allowed:
            return Decision(True, "read-only gh command")
    return Decision(False, f"gh {' '.join(args[:3])} is not allowed")


def _decide_git(argv: list[str], *, cwd: Path, workspace: Path, source: Path | None) -> Decision:
    parsed = parse_git(argv)
    if parsed.denied:
        return Decision(False, parsed.denied)
    if parsed.work_tree is not None:
        return Decision(False, "git --git-dir/--work-tree is not allowed")
    sub = parsed.subcommand
    if sub in _ALWAYS_DENY_GIT and sub != "config":
        return Decision(False, f"git {sub} is not allowed")
    if sub == "config":
        return _decide_git_config(parsed.rest)
    if sub == "reset" and "--hard" in parsed.rest:
        # Hard reset is refused even inside the workspace. A normal reset
        # without --hard remains available for local commits.
        return Decision(False, "git reset --hard is not allowed")
    if sub == "push" or _looks_like_push(sub, parsed.rest):
        return Decision(False, "git push is not allowed")
    target = _effective_dir(cwd, parsed.dash_c)
    in_workspace = _is_within(target, workspace)
    in_source = source is not None and _is_within(target, source) and not in_workspace
    if sub in READ_ONLY_GIT:
        if in_source or in_workspace or source is None:
            return Decision(True, "read-only git")
        return Decision(True, "read-only git")
    if sub not in WORKSPACE_GIT:
        return Decision(False, f"git {sub} is not allowed")
    if in_source:
        return Decision(False, "refusing to modify the original repository")
    if not in_workspace:
        return Decision(False, "git writes are confined to the Nightshift workspace")
    return Decision(True, "workspace git command")


def _decide_git_config(rest: tuple[str, ...]) -> Decision:
    if "--global" in rest or "--system" in rest:
        return Decision(False, "git config --global/--system is not allowed")
    write_flags = {"--unset", "--unset-all", "--add", "--replace-all", "--edit", "--remove-section", "--rename-section"}
    if write_flags.intersection(rest):
        return Decision(False, "git config writes are not allowed")
    read_flags = {"--get", "--get-all", "--get-regexp", "--list", "-l", "--show-origin", "--show-scope"}
    if read_flags.intersection(rest):
        return Decision(True, "read-only git config")
    return Decision(False, "git config writes are not allowed")


def _looks_like_push(sub: str, rest: tuple[str, ...]) -> bool:
    if sub == "push":
        return True
    return False


def parse_git(argv: list[str]) -> ParsedGit:
    dash_c: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            i += 1
            break
        if arg in {"--git-dir", "--work-tree"} or arg.startswith("--git-dir=") or arg.startswith("--work-tree="):
            return ParsedGit("", tuple(), tuple(), arg, None)
        if arg in _GIT_VALUE_OPTS:
            if i + 1 >= len(argv):
                return ParsedGit("", tuple(), tuple(), None, f"git option {arg} is missing a value")
            if arg == "-C":
                dash_c.append(argv[i + 1])
            i += 2
            continue
        if arg.startswith("-c") and arg != "-c":
            i += 1
            continue
        if arg.startswith("--") and "=" in arg:
            return ParsedGit("", tuple(), tuple(), None, f"unrecognized git option {arg}")
        if arg in _GIT_BOOL_OPTS or (arg.startswith("-") and arg not in {"-C", "-c"}):
            if arg.startswith("-") and arg not in _GIT_BOOL_OPTS:
                return ParsedGit("", tuple(), tuple(), None, f"unrecognized git option {arg}")
            i += 1
            continue
        if arg.startswith("-"):
            return ParsedGit("", tuple(), tuple(), None, f"unrecognized git option {arg}")
        sub = arg
        rest = tuple(argv[i + 1 :])
        return ParsedGit(sub, rest, tuple(dash_c), None, None)
    if i >= len(argv):
        return ParsedGit("", tuple(), tuple(dash_c), None, "missing git subcommand")
    return ParsedGit(argv[i], tuple(argv[i + 1 :]), tuple(dash_c), None, None)


def guard_git_argv(argv: list[str], hooks_path: str) -> list[str]:
    """Insert per-invocation overrides just before the subcommand.

    Later `-c` values win, so hooks and signing stay off even if the caller
    tried to point hooks at another directory.
    """
    index = _subcommand_index(argv)
    safety = ["-c", f"core.hooksPath={hooks_path}", "-c", "commit.gpgsign=false"]
    return [*argv[:index], *safety, *argv[index:]]


def _subcommand_index(argv: list[str]) -> int:
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            return i + 1
        if arg in _GIT_VALUE_OPTS or arg in {"--git-dir", "--work-tree"}:
            i += 2
            continue
        if arg.startswith("-"):
            i += 1
            continue
        return i
    return len(argv)


def _effective_dir(cwd: Path, dash_c: tuple[str, ...]) -> Path:
    current = cwd.resolve()
    for item in dash_c:
        path = Path(item)
        if not path.is_absolute():
            path = current / path
        current = path.resolve()
    return current


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _skip_options(argv: list[str], value_opts: set[str] | None = None) -> list[str]:
    value_opts = value_opts or set()
    out: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg in value_opts:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        out.append(arg)
    return out


def shim_main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    tool = os.environ.get("NIGHTSHIFT_GUARD_TOOL", "")
    workspace = Path(os.environ["NIGHTSHIFT_GUARD_WORKSPACE"])
    source_raw = os.environ.get("NIGHTSHIFT_GUARD_SOURCE", "")
    source = Path(source_raw) if source_raw else None
    decision = decide(tool, args, cwd=Path.cwd(), workspace=workspace, source=source)
    if not decision.allow:
        sys.stderr.write(f"nightshift: blocked {tool}: {decision.reason}\n")
        return 126
    real = os.environ.get(f"NIGHTSHIFT_REAL_{tool.upper()}", "")
    if not real:
        sys.stderr.write(f"nightshift: {tool} is not available\n")
        return 127
    if tool == "git":
        hooks = os.environ.get("NIGHTSHIFT_GUARD_HOOKS", "")
        if hooks:
            args = guard_git_argv(args, hooks)
    os.execv(real, [real, *args])
    return 127


def write_shims(
    bin_dir: Path,
    *,
    workspace: Path,
    source: Path,
    hooks_path: Path,
    pythonpath: str,
) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    hooks_path.mkdir(parents=True, exist_ok=True)
    for tool in ("git", "gh", "sudo", "kaggle", "npm", "twine"):
        path = bin_dir / tool
        path.write_text(_shim_script(tool, workspace, source, hooks_path, pythonpath), encoding="utf-8")
        path.chmod(0o755)


def _shim_script(tool: str, workspace: Path, source: Path, hooks: Path, pythonpath: str) -> str:
    return f"""#!/usr/bin/env python3
import os, sys
os.environ["NIGHTSHIFT_GUARD_TOOL"] = {tool!r}
os.environ["NIGHTSHIFT_GUARD_WORKSPACE"] = {str(workspace)!r}
os.environ["NIGHTSHIFT_GUARD_SOURCE"] = {str(source)!r}
os.environ["NIGHTSHIFT_GUARD_HOOKS"] = {str(hooks)!r}
root = {pythonpath!r}
if root and root not in sys.path:
    sys.path.insert(0, root)
from nightshift.guard import shim_main
raise SystemExit(shim_main())
"""
