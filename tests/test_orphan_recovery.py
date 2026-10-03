import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from nightshift.config import load_config
from nightshift.db import Database
from nightshift.locks import LockManager, process_start_token
from nightshift.models import empty_run
from nightshift.supervisor import recover_run, recover_all
from nightshift.job import load_job
from nightshift.testkit import make_repo, write_job


class OrphanRecoveryTests(unittest.TestCase):
    def test_explicit_retry_also_finds_previously_recovered_terminal_attempt(self):
        with tempfile.TemporaryDirectory() as raw:
            config = load_config(Path(raw))
            config.ensure_dirs()
            db = Database(config.db_path)
            try:
                run = empty_run(state='INTERRUPTED', max_attempts=2, attempt=1)
                db.insert_run(run)
                lines, refused = recover_all(config, db, LockManager(db), retry=True)
                self.assertFalse(refused, lines)
                updated = db.require_run(run.run_id)
                self.assertEqual(updated.state, 'QUEUED')
                self.assertEqual(updated.attempt, 2)
            finally:
                db.close()

    def test_recovery_stops_proved_orphan_without_relaunch_or_attempt_increment(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = load_config(root)
            config.ensure_dirs()
            db = Database(config.db_path)
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)', 'orphan-session'],
                                     start_new_session=True)
            try:
                record = empty_run(state='RUNNING', workspace_path=str(root), pid=child.pid,
                                   process_meta={'phase': 'provider', 'pid': child.pid,
                                                 'pgid': os.getpgid(child.pid), 'match': 'orphan-session',
                                                 'identity': process_start_token(child.pid),
                                                 'controller_pid': 99999999, 'controller_identity': 'gone'})
                record.run_dir = str(config.runs_dir / record.run_id)
                Path(record.run_dir).mkdir()
                source = root / 'source'
                make_repo(source)
                record.job_snapshot = json.dumps(load_job(write_job(root / 'job', source)).to_dict())
                db.insert_run(record)
                self.assertTrue(LockManager(db).acquire(['repo:orphan'], record.run_id, pid=99999999))
                other = empty_run()
                db.insert_run(other)
                self.assertFalse(LockManager(db).acquire(['repo:orphan'], other.run_id))
                updated, decision = recover_run(config, db, LockManager(db), record.run_id)
                self.assertEqual(updated.state, 'INTERRUPTED')
                self.assertEqual(decision.classification, 'controller_gone_child_stopped')
                self.assertEqual(updated.attempt, 1)
                self.assertIsNotNone(child.wait(timeout=5))
                again, _ = recover_run(config, db, LockManager(db), record.run_id)
                self.assertEqual(again.attempt, 1)
                self.assertTrue((config.runs_dir / record.run_id / 'report.md').is_file())
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
                db.close()
