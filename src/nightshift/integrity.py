"""Source-repository integrity snapshots.

The snapshot covers mutable git control metadata. It does not hash the
object database. A difference is evidence for a human; Nightshift does not
restore the source.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from nightshift.gitutil import current_branch, porcelain, rev_parse, run_git

CATEGORIES = (
    "head",
    "porcelain",
    "branch",
    "refs",
    "packed_refs",
    "config",
    "index",
    "hooks",
    "worktrees",
)
_MAX_HASH_BYTES = 1_000_000


@dataclass(frozen=True)
class SourceIntegrity:
    head: str
    branch: str
    porcelain: str
    refs: tuple[tuple[str, str], ...]
    packed_refs_sha256: str
    config_sha256: str
    index_sha256: str
    hooks_sha256: str
    worktrees_sha256: str

    def to_dict(self) -> dict:
        return {
            "head": self.head,
            "branch": self.branch,
            "porcelain": self.porcelain,
            "refs": [list(item) for item in self.refs],
            "packed_refs_sha256": self.packed_refs_sha256,
            "config_sha256": self.config_sha256,
            "index_sha256": self.index_sha256,
            "hooks_sha256": self.hooks_sha256,
            "worktrees_sha256": self.worktrees_sha256,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SourceIntegrity":
        refs = tuple((str(name), str(oid)) for name, oid in data.get("refs") or [])
        return cls(
            head=str(data.get("head") or ""),
            branch=str(data.get("branch") or ""),
            porcelain=str(data.get("porcelain") or ""),
            refs=refs,
            packed_refs_sha256=str(data.get("packed_refs_sha256") or ""),
            config_sha256=str(data.get("config_sha256") or ""),
            index_sha256=str(data.get("index_sha256") or ""),
            hooks_sha256=str(data.get("hooks_sha256") or ""),
            worktrees_sha256=str(data.get("worktrees_sha256") or ""),
        )


@dataclass(frozen=True)
class SourceIntegrityResult:
    ok: bool
    changed_categories: tuple[str, ...]
    before: SourceIntegrity
    after: SourceIntegrity


def capture(repo: Path) -> SourceIntegrity:
    common = _absolute_git(repo, "--git-common-dir")
    index = _absolute_git(repo, "--git-path", "index")
    return SourceIntegrity(
        head=rev_parse(repo, "HEAD"),
        branch=current_branch(repo),
        porcelain=porcelain(repo),
        refs=_refs(repo),
        packed_refs_sha256=_sha256_file(common / "packed-refs"),
        config_sha256=_sha256_file(common / "config"),
        index_sha256=_sha256_file(index),
        hooks_sha256=_sha256_tree(common / "hooks"),
        worktrees_sha256=_sha256_tree(common / "worktrees"),
    )


def compare(before: SourceIntegrity, after: SourceIntegrity) -> SourceIntegrityResult:
    changed: list[str] = []
    pairs = (
        ("head", before.head, after.head),
        ("porcelain", before.porcelain, after.porcelain),
        ("branch", before.branch, after.branch),
        ("refs", before.refs, after.refs),
        ("packed_refs", before.packed_refs_sha256, after.packed_refs_sha256),
        ("config", before.config_sha256, after.config_sha256),
        ("index", before.index_sha256, after.index_sha256),
        ("hooks", before.hooks_sha256, after.hooks_sha256),
        ("worktrees", before.worktrees_sha256, after.worktrees_sha256),
    )
    for name, left, right in pairs:
        if left != right:
            changed.append(name)
    return SourceIntegrityResult(
        ok=not changed,
        changed_categories=tuple(changed),
        before=before,
        after=after,
    )


def dumps(snapshot: SourceIntegrity) -> str:
    return json.dumps(snapshot.to_dict(), sort_keys=True)


def loads(payload: str) -> SourceIntegrity:
    return SourceIntegrity.from_dict(json.loads(payload))


def _refs(repo: Path) -> tuple[tuple[str, str], ...]:
    proc = run_git(repo, ["for-each-ref", "--format=%(refname)%00%(objectname)"])
    found: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        if "\0" not in line:
            continue
        name, oid = line.split("\0", 1)
        if name and oid:
            found.append((name, oid))
    found.sort()
    return tuple(found)


def _absolute_git(repo: Path, flag: str, name: str | None = None) -> Path:
    args = ["rev-parse", "--path-format=absolute", flag]
    if name is not None:
        args.append(name)
    text = run_git(repo, args).stdout.strip()
    return Path(text)


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_tree(root: Path) -> str:
    if not root.exists():
        return ""
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            digest.update(rel.encode())
            digest.update(b"\0")
            try:
                if path.is_symlink():
                    digest.update(b"link\0")
                    digest.update(os.readlink(path).encode())
                elif path.is_file():
                    size = path.stat().st_size
                    if size > _MAX_HASH_BYTES:
                        digest.update(f"large\0{size}".encode())
                    else:
                        digest.update(b"file\0")
                        digest.update(_sha256_file(path).encode())
            except OSError:
                digest.update(b"unreadable\0")
            digest.update(b"\n")
    return digest.hexdigest()
