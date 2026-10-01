import tempfile
import unittest
from pathlib import Path

from nightshift.gitutil import rev_parse
from nightshift.guard import decide, guard_git_argv
from nightshift.workspace import create_worktree, inspect_source
from nightshift.testkit import make_repo, snapshot


class WorkspaceTests(unittest.TestCase):
    def test_dirty_source_is_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            head = make_repo(source, dirty=True)
            before_head, before_status = snapshot(source)
            self.assertEqual(before_head, head)
            self.assertIn("DIRTY.txt", before_status)
            snap = inspect_source(source, "HEAD")
            dest = root / "worktrees" / "run-1"
            create_worktree(snap, dest)
            after_head, after_status = snapshot(source)
            self.assertEqual(after_head, before_head)
            self.assertEqual(after_status, before_status)
            self.assertTrue((source / "DIRTY.txt").is_file())
            self.assertFalse((dest / "DIRTY.txt").exists())
            self.assertEqual((dest / "README.md").read_text(encoding="utf-8"), "hello\n")
            self.assertEqual(rev_parse(dest, "HEAD"), head)
            self.assertNotEqual(dest.resolve(), source.resolve())


class GuardTests(unittest.TestCase):
    def test_git_push_and_source_writes_are_blocked(self) -> None:
        workspace = Path("/tmp/nightshift-workspace")
        source = Path("/tmp/nightshift-source")
        denied = decide("git", ["push", "origin", "main"], cwd=workspace, workspace=workspace, source=source)
        self.assertFalse(denied.allow)
        force = decide("git", ["push", "--force"], cwd=workspace, workspace=workspace, source=source)
        self.assertFalse(force.allow)
        hard = decide("git", ["reset", "--hard"], cwd=workspace, workspace=workspace, source=source)
        self.assertFalse(hard.allow)
        clean = decide("git", ["clean", "-fd"], cwd=workspace, workspace=workspace, source=source)
        self.assertFalse(clean.allow)
        global_config = decide(
            "git",
            ["config", "--global", "user.email", "x"],
            cwd=workspace,
            workspace=workspace,
            source=source,
        )
        self.assertFalse(global_config.allow)
        outside = decide("git", ["-C", str(source), "commit", "-m", "no"], cwd=workspace, workspace=workspace, source=source)
        self.assertFalse(outside.allow)
        allowed = decide("git", ["status", "--porcelain"], cwd=source, workspace=workspace, source=source)
        self.assertTrue(allowed.allow)
        commit = decide("git", ["commit", "-m", "local"], cwd=workspace, workspace=workspace, source=source)
        self.assertTrue(commit.allow)

    def test_sudo_gh_and_kaggle_are_blocked(self) -> None:
        workspace = Path("/tmp/ws")
        source = Path("/tmp/src")
        self.assertFalse(decide("sudo", ["true"], cwd=workspace, workspace=workspace, source=source).allow)
        self.assertFalse(
            decide("gh", ["pr", "create", "--fill"], cwd=workspace, workspace=workspace, source=source).allow
        )
        self.assertFalse(
            decide("gh", ["pr", "merge"], cwd=workspace, workspace=workspace, source=source).allow
        )
        self.assertTrue(decide("gh", ["pr", "view", "1"], cwd=workspace, workspace=workspace, source=source).allow)
        self.assertFalse(
            decide("kaggle", ["competitions", "submit"], cwd=workspace, workspace=workspace, source=source).allow
        )
        self.assertFalse(decide("npm", ["publish"], cwd=workspace, workspace=workspace, source=source).allow)

    def test_hooks_override_is_inserted_before_the_subcommand(self) -> None:
        argv = guard_git_argv(["-c", "user.email=nightshift@localhost", "commit", "-m", "note"], "/hooks")
        self.assertLess(argv.index("core.hooksPath=/hooks"), argv.index("commit"))
        self.assertIn("commit.gpgsign=false", argv)


if __name__ == "__main__":
    unittest.main()
