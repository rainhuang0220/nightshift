import json
import tempfile
import unittest
from pathlib import Path

from nightshift.config import load_config
from nightshift.db import Database
from nightshift.locks import LockManager
from nightshift.queue import enqueue
from nightshift.runner import execute_run
from nightshift.testkit import make_repo, write_job


class VerificationAuditTests(unittest.TestCase):
    def test_blocked_run_does_not_claim_tests_executed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            job = write_job(root / 'job', root / 'missing')
            config = load_config(root / 'ns')
            config.ensure_dirs()
            db = Database(config.db_path)
            try:
                run = execute_run(config, db, LockManager(db), enqueue(db, job).run_id)
                report = Path(run.report_path).read_text()
                executed = report.split('### Tests executed\n')[1].split('### Test results')[0]
                self.assertNotIn('test -f', executed)
                result = json.loads(Path(run.run_dir, 'result.json').read_text())
                self.assertEqual(result['state'], 'BLOCKED')
                self.assertEqual(result['verification_results'], [])
            finally:
                db.close()

    def test_each_check_records_exit_and_time_in_result_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'source'
            make_repo(source)
            job = write_job(root / 'job', source, verification=['exit 7', 'printf second'], expected_artifacts=[])
            config = load_config(root / 'ns')
            config.ensure_dirs()
            db = Database(config.db_path)
            try:
                run = execute_run(config, db, LockManager(db), enqueue(db, job).run_id)
                self.assertEqual(run.state, 'FAILED')
                result = json.loads(Path(run.run_dir, 'result.json').read_text())
                self.assertEqual([r['exit_code'] for r in result['verification_results']], [7, 0])
                self.assertTrue(all(r['duration_seconds'] >= 0 for r in result['verification_results']))
                self.assertEqual(result['source_revision'], __import__('nightshift.testkit', fromlist=['git']).git(source, 'rev-parse', 'HEAD').stdout.strip())
            finally:
                db.close()
