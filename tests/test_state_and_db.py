import tempfile
import unittest
from pathlib import Path

from nightshift.db import Database
from nightshift.locks import LockManager
from nightshift.models import IllegalTransition, RunState, empty_run, ensure_transition
from nightshift.queue import claim_next, enqueue
from nightshift.testkit import make_repo, write_job


class TransitionTests(unittest.TestCase):
    def test_happy_path_is_legal(self) -> None:
        ensure_transition(RunState.QUEUED, RunState.PREPARING)
        ensure_transition(RunState.PREPARING, RunState.RUNNING)
        ensure_transition(RunState.RUNNING, RunState.VERIFYING)
        ensure_transition(RunState.VERIFYING, RunState.SUCCEEDED)

    def test_illegal_transition_is_rejected(self) -> None:
        with self.assertRaises(IllegalTransition):
            ensure_transition(RunState.SUCCEEDED, RunState.RUNNING)
        with self.assertRaises(IllegalTransition):
            ensure_transition(RunState.CANCELLED, RunState.QUEUED)

    def test_explicit_retry_edges_exist(self) -> None:
        ensure_transition(RunState.INTERRUPTED, RunState.QUEUED)
        ensure_transition(RunState.FAILED, RunState.QUEUED)


class DatabaseTests(unittest.TestCase):
    def test_reopen_returns_the_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            db = Database(root / "nightshift.db")
            stored = db.insert_run(empty_run(job_id="persist", provider="fake", state=RunState.QUEUED.value))
            db.close()
            again = Database(root / "nightshift.db")
            loaded = again.get_run(stored.run_id)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.run_id, stored.run_id)
            self.assertEqual(loaded.job_id, "persist")
            self.assertEqual(loaded.state, RunState.QUEUED.value)
            self.assertEqual(loaded.provider, "fake")
            again.close()

    def test_database_rejects_illegal_transition(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            db = Database(Path(raw) / "nightshift.db")
            run = db.insert_run(empty_run(job_id="job", provider="fake"))
            run = db.transition(run.run_id, RunState.PREPARING.value, "prep")
            run = db.transition(run.run_id, RunState.RUNNING.value, "run")
            run = db.transition(run.run_id, RunState.VERIFYING.value, "verify")
            run = db.transition(run.run_id, RunState.SUCCEEDED.value, "ok", exit_code=0)
            with self.assertRaises(IllegalTransition):
                db.transition(run.run_id, RunState.RUNNING.value, "nope")
            self.assertEqual(db.require_run(run.run_id).state, RunState.SUCCEEDED.value)
            db.close()


class QueueAndLockTests(unittest.TestCase):
    def test_queue_lists_and_claims_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first_repo = root / "repo-a"
            second_repo = root / "repo-b"
            make_repo(first_repo)
            make_repo(second_repo)
            db = Database(root / "state" / "nightshift.db")
            first = enqueue(db, write_job(root / "job-a", first_repo, id="alpha", concurrency_group="alpha"))
            second = enqueue(
                db,
                write_job(root / "job-b", second_repo, id="beta", concurrency_group="beta"),
            )
            listed = db.list_runs()
            self.assertEqual([item.run_id for item in listed], [first.run_id, second.run_id])
            self.assertTrue(all(item.state == RunState.QUEUED.value for item in listed))
            claimed = claim_next(db)
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed.run_id, first.run_id)
            self.assertEqual(claimed.state, RunState.PREPARING.value)
            nxt = claim_next(db)
            self.assertIsNotNone(nxt)
            assert nxt is not None
            self.assertEqual(nxt.run_id, second.run_id)
            self.assertIsNone(claim_next(db))
            db.close()

    def test_same_concurrency_group_cannot_be_claimed_together(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo_a = root / "repo-a"
            repo_b = root / "repo-b"
            make_repo(repo_a)
            make_repo(repo_b)
            db = Database(root / "nightshift.db")
            enqueue(db, write_job(root / "job-a", repo_a, id="one", concurrency_group="shared"))
            enqueue(db, write_job(root / "job-b", repo_b, id="two", concurrency_group="shared"))
            first = claim_next(db)
            self.assertIsNotNone(first)
            self.assertIsNone(claim_next(db))
            LockManager(db).release(first.run_id)
            second = claim_next(db)
            self.assertIsNotNone(second)
            assert first is not None and second is not None
            self.assertNotEqual(first.run_id, second.run_id)
            db.close()

    def test_locks_reject_a_live_owner_and_replace_a_dead_one(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as raw:
            db = Database(Path(raw) / "nightshift.db")
            locks = LockManager(db)
            holder = db.insert_run(empty_run(job_id="holder", provider="fake", state=RunState.PREPARING.value))
            sleeper = subprocess.Popen(["sleep", "30"])
            try:
                db.write_locks(["group:shared"], holder.run_id, sleeper.pid)
                other = db.insert_run(empty_run(job_id="other", provider="fake"))
                self.assertFalse(locks.acquire(["group:shared"], other.run_id))
            finally:
                sleeper.kill()
                sleeper.wait(timeout=5)
            self.assertTrue(locks.acquire(["group:shared"], other.run_id))
            self.assertEqual(db.lock_rows()[0]["run_id"], other.run_id)
            locks.release(other.run_id)
            self.assertEqual(db.lock_rows(), [])
            db.close()


if __name__ == "__main__":
    unittest.main()
