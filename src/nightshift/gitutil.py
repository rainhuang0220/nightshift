"""Git helpers.

The default workspace write is an independent local clone performed by
`workspace.py`. The optional worktree backend calls `add_detached_worktree`,
which registers `.git/worktrees` on the source. Nightshift never stashes,
resets, cleans, or checks out the source working tree.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


GIT_BIN = shutil.which("git") or "git"


class GitError(Exception):
    def __init__(self, message: str, *, returncode: int = 1) -> None:
        super().__init__(message)
        self.returncode = returncode


def run_git(
    repo: Path,
    args: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [GIT_BIN, "-C", str(repo), *args],
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
    """Register a detached worktree. This writes `.git/worktrees` metadata only."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_git(source, ["worktree", "add", "--detach", str(dest), revision])


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
