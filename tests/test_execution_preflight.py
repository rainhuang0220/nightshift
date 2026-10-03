import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nightshift.config import load_config
from nightshift.db import Database
from nightshift.locks import LockManager
from nightshift.queue import enqueue
from nightshift.runner import execute_run
from nightshift.testkit import make_repo, write_job, snapshot, run_cli


class ExecutionPreflightTests(unittest.TestCase):
    def test_cli_rejects_control_paths_inside_source_before_creating_state(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'source'
            make_repo(source)
            job = write_job(root / 'job', source)
            before = snapshot(source)
            result = run_cli(source, ['run', str(job)])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('overlap', result.stderr)
            self.assertEqual(snapshot(source), before)
            self.assertFalse((source / 'state').exists())

    def test_blocked_preflight_does_not_create_a_clone_or_launch_a_provider(self):
        for case in ('recursive', 'overlap', 'missing_executable', 'low_disk', 'missing_sandbox'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / 'source'
                make_repo(source)
                if case == 'recursive':
                    (source / 'pyproject.toml').write_text('[project]\nname="nightshift"\n')
                    (source / 'src/nightshift').mkdir(parents=True)
                    (source / 'src/nightshift/__init__.py').write_text('')
                config = load_config(source / 'ns' if case == 'overlap' else root / 'ns')
                config.ensure_dirs()
                db = Database(config.db_path)
                before = snapshot(source)
                try:
                    job = write_job(root / 'job', source,
                                    verification_steps=[{'argv': ['definitely-no-such-ns-executable']}])
                    if case == 'missing_executable':
                        manifest = job / 'job.toml'
                        manifest.write_text(manifest.read_text().replace('verification = ["test -f nightshift-notes/result.md"]', 'verification = [{argv=["definitely-no-such-ns-executable"]}]'))
                    with patch('nightshift.runner.get_provider') as provider, \
                         patch('nightshift.runner.shutil.disk_usage') as usage, \
                         patch('nightshift.runner.sandbox_available', return_value=case != 'missing_sandbox'):
                        usage.return_value.free = 1 if case == 'low_disk' else 1024**3
                        run = enqueue(db, job)
                        result = execute_run(config, db, LockManager(db), run.run_id)
                    self.assertEqual(result.state, 'BLOCKED', result.failure_reason)
                    self.assertEqual(list(config.worktrees_dir.iterdir()), [])
                    provider.assert_not_called()
                    self.assertEqual(snapshot(source), before)
                finally:
                    db.close()
