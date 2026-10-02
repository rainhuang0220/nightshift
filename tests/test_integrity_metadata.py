"""Protected git metadata is content-hashed. The object database is not."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from nightshift.integrity import IntegrityError, capture, compare
from nightshift.testkit import git, make_repo


class MetadataHashTests(unittest.TestCase):
    def test_same_size_hook_and_worktree_replacements_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "repo"
            make_repo(repo)
            hook = repo / ".git" / "hooks" / "pre-commit"
            hook.write_text("A" * 32, encoding="utf-8")
            base = capture(repo)
            hook.write_text("B" * 32, encoding="utf-8")
            self.assertIn("hooks", compare(base, capture(repo)).changed_categories)
            hook.write_text("A" * 32, encoding="utf-8")
            self.assertNotIn("hooks", compare(base, capture(repo)).changed_categories)
            worktree = repo / ".git" / "worktrees" / "demo"
            worktree.mkdir(parents=True)
            (worktree / "gitdir").write_text("same-size-path-aaa\n", encoding="utf-8")
            recorded = capture(repo)
            (worktree / "gitdir").write_text("same-size-path-bbb\n", encoding="utf-8")
            self.assertIn("worktrees", compare(recorded, capture(repo)).changed_categories)

    def test_symlink_replacement_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            make_repo(repo)
            first = root / "first-target"
            second = root / "second-target"
            first.write_text("one\n", encoding="utf-8")
            second.write_text("two\n", encoding="utf-8")
            link = repo / ".git" / "hooks" / "post-commit"
            link.symlink_to(first)
            base = capture(repo)
            link.unlink()
            link.symlink_to(second)
            self.assertIn("hooks", compare(base, capture(repo)).changed_categories)
            link.unlink()
            link.symlink_to(first)
            self.assertNotIn("hooks", compare(base, capture(repo)).changed_categories)

    def test_oversized_or_unreadable_metadata_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "repo"
            make_repo(repo)
            huge = repo / ".git" / "hooks" / "huge"
            huge.write_bytes(b"x" * 1_000_001)
            with self.assertRaises(IntegrityError):
                capture(repo)
            huge.unlink()
            hidden = repo / ".git" / "hooks" / "hidden"
            hidden.write_text("secret\n", encoding="utf-8")
            os.chmod(hidden, 0)
            try:
                with self.assertRaises(IntegrityError):
                    capture(repo)
            finally:
                os.chmod(hidden, stat.S_IRUSR | stat.S_IWUSR)

    def test_object_database_only_change_is_not_protected_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw) / "repo"
            make_repo(repo)
            base = capture(repo)
            subprocess.run(
                ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                input=b"loose-object-not-a-ref\n",
                check=True,
                stdout=subprocess.DEVNULL,
            )
            after = compare(base, capture(repo))
            self.assertTrue(after.ok, after.changed_categories)
            self.assertFalse((repo / ".git" / "objects").samefile(repo / ".git" / "hooks"))


class ImportGuardTests(unittest.TestCase):
    def test_capture_does_not_name_the_object_database(self) -> None:
        source = Path(capture.__code__.co_filename)  # type: ignore[attr-defined]
        # The function object points at integrity.py. Read the shipped module.
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("objects", capture.__code__.co_names)
        self.assertIn("hooks", text)
        self.assertNotIn('common / "objects"', text)


if __name__ == "__main__":
    unittest.main()
