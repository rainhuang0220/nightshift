import tempfile
import unittest
from pathlib import Path

from nightshift.testkit import git, make_repo
from nightshift.workspace import WorkspaceError, cleanup_workspace, inspect_source, prepare_workspace


class WorkspaceOwnershipTests(unittest.TestCase):
    def test_cleanup_preserves_an_unowned_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'source'
            make_repo(source)
            dest = root / 'unrelated'
            dest.mkdir()
            (dest / 'notes').write_text('human work')
            with self.assertRaises(WorkspaceError):
                cleanup_workspace('clone', source, dest)
            self.assertEqual((dest / 'notes').read_text(), 'human work')

    def test_owned_cleanup_checks_source_and_refuses_symlink_alias(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source, other = root / 'source', root / 'other'
            make_repo(source)
            make_repo(other)
            dest = prepare_workspace(inspect_source(source, 'HEAD'), root / 'clone')
            with self.assertRaises(WorkspaceError):
                cleanup_workspace('clone', other, dest)
            alias = root / 'alias'
            alias.symlink_to(dest, target_is_directory=True)
            with self.assertRaises(WorkspaceError):
                cleanup_workspace('clone', source, alias)
            self.assertTrue(dest.exists())
            cleanup_workspace('clone', source, dest)
            self.assertFalse(dest.exists())

    def test_result_commit_uses_configured_source_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'source'
            make_repo(source)
            dest = prepare_workspace(inspect_source(source, 'HEAD'), root / 'clone')
            (dest / 'result').write_text('result')
            git(dest, 'add', '.')
            git(dest, 'commit', '-m', 'result')
            self.assertEqual(git(dest, 'show', '-s', '--format=%an <%ae>', 'HEAD').stdout.strip(),
                             'Test User <test@example.com>')
