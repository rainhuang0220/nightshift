"""Prepare an isolated workspace from a source checkout.

The default backend is an independent local clone. It has its own refs and
git metadata. A commit in the clone does not update source refs.

The worktree backend remains available as isolation = "worktree".

WEAKER ISOLATION
SHARES SOURCE GIT METADATA
NOT DEFAULT FOR UNATTENDED JOBS

`git worktree add` updates the source repository's `.git/worktrees` registry.
It does not change HEAD, the index, or the working-tree files. Nightshift
never stashes, resets, cleans, or checks out the source working tree.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from nightshift.gitutil import (
    GIT_BIN,
    add_detached_worktree,
    current_branch,
    hook_config_args,
    inside,
    is_git_repo,
    porcelain,
    rev_parse,
    run_git,
    sanitized_git_env,
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
    """Linked worktree. Weaker isolation; shares the source git directory."""
    dest = _fresh_dest(snapshot, dest)
    add_detached_worktree(snapshot.repo, dest, snapshot.revision)
    assert_unchanged(snapshot)
    return dest


def prepare_workspace(snapshot: SourceSnapshot, dest: Path, isolation: str = "clone") -> Path:
    return backend_for(isolation).prepare(snapshot, dest)


def cleanup_workspace(isolation: str, source: Path, dest: Path) -> None:
    backend_for(isolation).cleanup(source, dest)


def import_instructions(isolation: str = "clone") -> str:
    return backend_for(isolation).import_instructions()


def backend_for(isolation: str) -> "WorkspaceBackend":
    if isolation == "clone":
        return CloneWorkspaceBackend()
    if isolation == "worktree":
        return WorktreeWorkspaceBackend()
    raise WorkspaceError(f"unknown isolation {isolation!r}")


class WorkspaceBackend:
    name = ""
    weaker = False

    def prepare(self, snapshot: SourceSnapshot, dest: Path) -> Path:
        raise NotImplementedError

    def inspect(self, repo: Path, base_ref: str) -> SourceSnapshot:
        return inspect_source(repo, base_ref)

    def cleanup(self, source: Path, dest: Path) -> None:
        raise NotImplementedError

    def import_instructions(self) -> str:
        raise NotImplementedError


class CloneWorkspaceBackend(WorkspaceBackend):
    """Independent local clone. Default for unattended jobs."""

    name = "clone"
    weaker = False

    def prepare(self, snapshot: SourceSnapshot, dest: Path) -> Path:
        dest = _fresh_dest(snapshot, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="nightshift-git-") as raw:
            anchor = Path(raw)
            env = sanitized_git_env(anchor)
            config_args = hook_config_args(anchor)
            proc = subprocess.run(
                [GIT_BIN, *config_args, "clone", "--no-hardlinks", "--no-checkout", str(snapshot.repo), str(dest)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                raise WorkspaceError(f"git clone failed: {detail}")
            run_git(dest, ["remote", "remove", "origin"], check=False, env=env, config_args=config_args)
            try:
                run_git(dest, ["checkout", "--detach", snapshot.revision], env=env, config_args=config_args)
            except Exception as exc:
                raise WorkspaceError(str(exc)) from exc
        hooks = dest / ".git" / "nightshift-hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        run_git(dest, ["config", "--local", "core.hooksPath", str(hooks)])
        run_git(dest, ["config", "--local", "user.email", "nightshift@localhost"])
        run_git(dest, ["config", "--local", "user.name", "Nightshift"])
        run_git(dest, ["config", "--local", "commit.gpgsign", "false"])
        return dest

    def cleanup(self, source: Path, dest: Path) -> None:
        dest = dest.resolve()
        source = source.resolve()
        if dest == source or inside(source, dest) or inside(dest, source):
            raise WorkspaceError("refusing to delete a path that overlaps the source repository")
        if dest.exists():
            shutil.rmtree(dest)

    def import_instructions(self) -> str:
        return (
            "Nightshift does not merge or push. Review the independent clone and "
            "import commits by hand."
        )


class WorktreeWorkspaceBackend(WorkspaceBackend):
    """WEAKER ISOLATION. SHARES SOURCE GIT METADATA. NOT DEFAULT FOR UNATTENDED JOBS."""

    name = "worktree"
    weaker = True

    def prepare(self, snapshot: SourceSnapshot, dest: Path) -> Path:
        return create_worktree(snapshot, dest)

    def cleanup(self, source: Path, dest: Path) -> None:
        subprocess.run(
            [GIT_BIN, "-C", str(source), "worktree", "remove", "--force", str(dest)],
            check=False,
            capture_output=True,
            text=True,
        )

    def import_instructions(self) -> str:
        return (
            "WEAKER ISOLATION. SHARES SOURCE GIT METADATA. NOT DEFAULT FOR UNATTENDED JOBS. "
            "Nightshift does not merge or push."
        )


def _fresh_dest(snapshot: SourceSnapshot, dest: Path) -> Path:
    dest = dest.resolve()
    if dest.exists():
        raise WorkspaceError(f"workspace path already exists: {dest}")
    if inside(dest, snapshot.repo) or inside(snapshot.repo, dest):
        raise WorkspaceError("workspace must not overlap the source repository")
    return dest
