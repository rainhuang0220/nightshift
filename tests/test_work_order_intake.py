import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nightshift.config import load_config
from nightshift.db import Database
from nightshift.locks import LockManager
from nightshift.runner import execute_run
from nightshift.testkit import make_repo, run_cli
from test_work_order import sample_order


def local_order(root):
    order = sample_order()
    source = root / 'source'
    revision = make_repo(source, dirty=True)
    order['repository'].update(path=str(source), base_revision=revision)
    now = datetime.now(timezone.utc)
    order['created_at'] = order['provenance']['decision_at'] = now.isoformat()
    order['evidence'][0].update(observed_at=now.isoformat(), expires_at=(now+timedelta(days=3)).isoformat())
    order['validation'] = [{'argv': ['/bin/test', '-f', 'nightshift-notes/result.md'],
                            'expectation': 'Local result exists', 'timeout_seconds': 30}]
    return order


class IntakeTests(unittest.TestCase):
    def test_preview_is_read_only_and_import_is_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            order = local_order(root)
            manifest = root / 'order.json'
            manifest.write_text(json.dumps(order))
            home = root / 'ns'
            preview = run_cli(home, ['work-order', 'preview', str(manifest), '--provider', 'fake'])
            self.assertEqual(preview.returncode, 0, preview.stderr)
            plan = json.loads(preview.stdout)
            self.assertEqual(plan['base_ref'], order['repository']['base_revision'])
            self.assertEqual(plan['work_order'], order)
            self.assertFalse(home.exists())
            first = run_cli(home, ['work-order', 'import', str(manifest), '--provider', 'fake'])
            second = run_cli(home, ['work-order', 'import', str(manifest), '--provider', 'fake'])
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout, second.stdout)
            config = load_config(home)
            db = Database(config.db_path)
            try:
                self.assertEqual(len(db.list_runs()), 1)
                result = execute_run(config, db, LockManager(db), db.list_runs()[0].run_id)
                self.assertEqual(result.state, 'SUCCEEDED')
                self.assertEqual(json.loads(result.job_snapshot)['work_order'], order)
                self.assertTrue(Path(result.run_dir, 'result.json').is_file())
            finally:
                db.close()

    def test_conflicting_identity_and_stale_order_are_rejected(self):
        from nightshift.intake import import_order
        from nightshift.work_order import WorkOrderError
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            order = local_order(root)
            config = load_config(root / 'ns')
            config.ensure_dirs()
            db = Database(config.db_path)
            try:
                import_order(db, order, provider='fake')
                order['objective'] = 'A different objective'
                with self.assertRaisesRegex(WorkOrderError, 'task_id'):
                    import_order(db, order, provider='fake')
                self.assertEqual(len(db.list_runs()), 1)
            finally:
                db.close()
            order['task_id'] = 'old-task'
            order['created_at'] = order['provenance']['decision_at'] = '2020-01-01T00:00:00Z'
            order['evidence'][0].update(observed_at='2020-01-01T00:00:00Z', expires_at='2020-01-02T00:00:00Z')
            from nightshift.intake import plan_order
            with self.assertRaisesRegex(WorkOrderError, 'stale'):
                plan_order(order, provider='fake')
