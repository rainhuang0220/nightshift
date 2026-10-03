import tempfile
import unittest
from pathlib import Path

from nightshift.config import load_config
from nightshift.db import Database
from nightshift.locks import LockManager
from nightshift.models import RunState
from nightshift.queue import enqueue
from nightshift.runner import execute_run
from nightshift.supervisor import recover_run
from nightshift.testkit import make_repo, write_job


class ControllerLeaseTests(unittest.TestCase):
    def test_non_regular_verification_journal_is_unknown_without_blocking_recovery(self):
        import os
        import subprocess
        import sys
        from nightshift.testkit import cli_env
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            attempt = root / 'attempt-1'
            attempt.mkdir()
            os.mkfifo(attempt / 'verification-results.json')
            program = ('from nightshift.models import empty_run; '
                       'from nightshift.supervisor import probe_run; '
                       'import sys; print(probe_run(empty_run(state="VERIFYING",run_dir=sys.argv[1])).verification_started)')
            result = subprocess.run([sys.executable, '-c', program, str(root)], env=cli_env(),
                                    text=True, capture_output=True, timeout=2)
            self.assertEqual(result.stdout.strip(), 'True')

    def test_started_verification_journal_prevents_repetition_before_pid_is_recorded(self):
        from nightshift.models import empty_run
        from nightshift.supervisor import probe_run, classify_recovery
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            attempt = root / 'attempt-1'
            attempt.mkdir()
            (attempt / 'verification-results.json').write_text('[{"index":0,"started_at":"now","exit_code":null}]')
            run = empty_run(state='VERIFYING', run_dir=str(root), provider_exit_code=0,
                            verification_ran=False, process_meta={'phase':'provider','pid':99999999})
            decision = classify_recovery(run, probe_run(run))
            self.assertEqual(decision.new_state, 'INTERRUPTED')
            self.assertEqual(decision.action, 'mark')
            self.assertFalse(decision.launch_provider)

    def test_second_controller_and_recovery_cannot_touch_an_owned_attempt(self):
        from nightshift.locks import execution_lease
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'source'
            make_repo(source)
            config = load_config(root / 'ns')
            config.ensure_dirs()
            db = Database(config.db_path)
            try:
                run = enqueue(db, write_job(root / 'job', source))
                db.transition(run.run_id, RunState.PREPARING.value, 'claimed')
                with execution_lease(config.state_dir, run.run_id) as owned:
                    self.assertTrue(owned)
                    with execution_lease(config.state_dir, run.run_id) as second:
                        self.assertFalse(second)
                    updated, decision = recover_run(config, db, LockManager(db), run.run_id)
                    self.assertEqual(updated.state, 'PREPARING')
                    self.assertEqual(decision.classification, 'controller_still_alive')
                    with self.assertRaisesRegex(Exception, 'controller'):
                        execute_run(config, db, LockManager(db), run.run_id)
                    self.assertFalse((config.runs_dir / run.run_id).exists())
                updated, decision = recover_run(config, db, LockManager(db), run.run_id)
                self.assertEqual(updated.state, 'INTERRUPTED')
                again, _ = recover_run(config, db, LockManager(db), run.run_id)
                self.assertEqual(again.attempt, 1)
            finally:
                db.close()

    def test_dead_verification_is_interrupted_without_repeating_commands(self):
        from nightshift.models import empty_run
        from nightshift.supervisor import ProcessProbe, classify_recovery
        run = empty_run(state='VERIFYING', provider_exit_code=0, verification_ran=False,
                        process_meta={'phase': 'verifying', 'pid': 99999999, 'identity': 'gone'})
        decision = classify_recovery(run, ProcessProbe(alive=False, workspace_exists=True))
        self.assertEqual(decision.action, 'mark')
        self.assertEqual(decision.new_state, 'INTERRUPTED')
        self.assertFalse(decision.launch_provider)
