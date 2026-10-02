"""Git helpers.

The default workspace write is an independent local clone performed by
`workspace.py`. The optional worktree backend calls `add_detached_worktree`,
which registers `.git/worktrees` on the source. Nightshift never stashes,
resets, cleans, or checks out the source working tree.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


GIT_BIN = shutil.which("git") or "git"


class GitError(Exception):
    def __init__(self, message: str, *, returncode: int = 1) -> None:
        super().__init__(message)
        self.returncode = returncode


def hook_config_args(anchor: Path) -> list[str]:
    """Per-invocation Git config that disables hooks and templates."""
    hooks = anchor / "hooks"
    template = anchor / "template"
    hooks.mkdir(parents=True, exist_ok=True)
    template.mkdir(parents=True, exist_ok=True)
    return [
        "-c",
        f"core.hooksPath={hooks}",
        "-c",
        f"init.templateDir={template}",
    ]


def sanitized_git_env(anchor: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for one Git invocation. The operator config is not modified."""
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key in {
            "GIT_CONFIG",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_SYSTEM",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_PARAMETERS",
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_NAMESPACE",
            "GIT_COMMON_DIR",
        } or key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
            env.pop(key, None)
    anchor.mkdir(parents=True, exist_ok=True)
    empty = anchor / "gitconfig"
    empty.write_text("", encoding="utf-8")
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = str(empty)
    env["GIT_CONFIG_SYSTEM"] = str(empty)
    env["GIT_CONFIG_COUNT"] = "0"
    return env


def run_git(
    repo: Path,
    args: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
    config_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [GIT_BIN, "-C", str(repo)]
    if config_args:
        command.extend(config_args)
    command.extend(args)
    proc = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise GitError(f"git {' '.join(args)} failed: {detail}", returncode=proc.returncode)
    return proc


def is_git_repo(repo: Path) -> bool:
    if not repo.exists():
        return False
    proc = run_git(repo, ["rev-parse", "--is-inside-work-tree"], check=False)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def rev_parse(repo: Path, ref: str) -> str:
    return run_git(repo, ["rev-parse", "--verify", ref]).stdout.strip()


def porcelain(repo: Path) -> str:
    return run_git(repo, ["status", "--porcelain=v1"]).stdout


def current_branch(repo: Path) -> str:
    proc = run_git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def add_detached_worktree(source: Path, dest: Path, revision: str) -> None:
    """Register a detached worktree. This writes `.git/worktrees` metadata only.

    The checkout runs with hooks and templates disabled for this invocation.
    The operator's global Git config is not edited.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nightshift-git-") as raw:
        anchor = Path(raw)
        run_git(
            source,
            ["worktree", "add", "--detach", str(dest), revision],
            env=sanitized_git_env(anchor),
            config_args=hook_config_args(anchor),
        )


def commits_since(repo: Path, revision: str) -> list[str]:
    proc = run_git(
        repo,
        ["log", "--format=%H %s", f"{revision}..HEAD"],
        check=False,
    )
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def changed_files(repo: Path, revision: str) -> list[str]:
    names: list[str] = []
    diff = run_git(repo, ["diff", "--name-only", revision], check=False)
    if diff.returncode == 0:
        names.extend(line for line in diff.stdout.splitlines() if line.strip())
    status = run_git(repo, ["status", "--porcelain=v1"], check=False)
    if status.returncode == 0:
        for line in status.stdout.splitlines():
            if not line.strip():
                continue
            path = line[3:] if len(line) > 3 else line
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            names.append(path.strip())
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


def inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True
