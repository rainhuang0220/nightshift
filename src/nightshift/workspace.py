"""Inspect a source checkout and create a detached worktree.

The source working tree is only read. `git worktree add` updates the source
repository's worktree registry under `.git/worktrees`. It does not change
HEAD, the index, or the working tree files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nightshift.gitutil import (
    add_detached_worktree,
    current_branch,
    inside,
    is_git_repo,
    porcelain,
    rev_parse,
)


class WorkspaceError(Exception):
    pass


@dataclass(frozen=True)
class SourceSnapshot:
    repo: Path
    head: str
    revision: str
    porcelain: str
    branch: str


def inspect_source(repo: Path, base_ref: str) -> SourceSnapshot:
    if not repo.exists():
        raise WorkspaceError(f"repository does not exist: {repo}")
    if not is_git_repo(repo):
        raise WorkspaceError(f"not a git repository: {repo}")
    try:
        head = rev_parse(repo, "HEAD")
        revision = rev_parse(repo, base_ref)
    except Exception as exc:
        raise WorkspaceError(str(exc)) from exc
    return SourceSnapshot(
        repo=repo.resolve(),
        head=head,
        revision=revision,
        porcelain=porcelain(repo),
        branch=current_branch(repo),
    )


def assert_unchanged(snapshot: SourceSnapshot) -> None:
    head = rev_parse(snapshot.repo, "HEAD")
    tree = porcelain(snapshot.repo)
    if head != snapshot.head or tree != snapshot.porcelain:
        raise WorkspaceError(
            "source repository HEAD or working tree changed while preparing the workspace"
        )


def create_worktree(snapshot: SourceSnapshot, dest: Path) -> Path:
    dest = dest.resolve()
    if dest.exists():
        raise WorkspaceError(f"workspace path already exists: {dest}")
    if inside(dest, snapshot.repo) or inside(snapshot.repo, dest):
        raise WorkspaceError("workspace must not overlap the source repository")
    add_detached_worktree(snapshot.repo, dest, snapshot.revision)
    assert_unchanged(snapshot)
    return dest
