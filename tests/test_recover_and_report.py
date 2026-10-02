import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from nightshift.config import load_config
from nightshift.db import Database
from nightshift.locks import LockManager
from nightshift.integrity import capture, dumps
from nightshift.models import RunState, empty_run
from nightshift.providers.fake import FakeProvider
from nightshift.providers.base import ProviderRequest
from nightshift.report import ReportInputs, render_report
from nightshift.supervisor import classify_recovery, probe_run, recover_run, retry_run
from nightshift.supervisor import ProcessProbe
from nightshift.testkit import make_repo


def _job_snapshot(verification: list[str] | None = None) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "id": "recover-job",
            "description": "recovery fixture",
            "type": "note",
            "repository": "/tmp/unused",
            "base_ref": "HEAD",
            "provider": "fake",
            "model": None,
            "max_runtime_seconds": 30,
            "max_attempts": 2,
            "concurrency_group": "recover",
            "network": False,
            "write_scope": "workspace",
            "expected_artifacts": [],
            "verification": verification if verification is not None else [],
            "success_criteria": ["recorded"],
            "allow_bash": [],
            "prompt": "noop\n",
            "job_dir": "/tmp",
            "job_file": "/tmp/job.toml",
        }
    )


class ProcessTests(unittest.TestCase):
    def test_terminate_process_stops_the_child_group(self) -> None:
        import os
        import subprocess

        from nightshift.providers.base import terminate_process

        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            terminate_process(proc.pid, os.getpgid(proc.pid))
            self.assertIsNotNone(proc.wait(timeout=5))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


class ReportTests(unittest.TestCase):
    def test_report_contains_the_morning_sections(self) -> None:
        text = render_report(
            ReportInputs(
                run_id="ns-abc",
                job_id="audit",
                description="Audit the repo",
                job_type="audit",
                state="FAILED",
                provider="fake",
                attempt=1,
                max_attempts=1,
                source_repo="/repos/demo",
                source_revision="abc123",
                source_head="abc123",
                base_ref="HEAD",
                workspace="/work/ns-abc",
                session_id="sess",
                started_at="2026-10-02T00:00:00Z",
                ended_at="2026-10-02T00:05:00Z",
                duration_seconds=300,
                exit_code=1,
                provider_exit_code=1,
                verification_exit_code=1,
                failure_reason="verification failed with exit 1",
                findings="finding: the parser rejected an empty fixture\nfinding: no source edit was required",
                commits=[],
                files_changed=["src/app.py"],
                verification_commands=["pytest -q"],
                verification_output="1 failed",
                success_criteria=["tests pass"],
                measurements={"fake_result": "fail"},
                host_info={"os": "Darwin", "python": "3.14.0"},
                artifacts_expected=[],
                artifacts_found=[],
            )
        )
        for label in (
            "## What ran",
            "## What succeeded",
            "## What failed",
            "## Repository",
            "Original revision: abc123",
            "## What changed",
            "### Resulting local commits",
            "### Files changed",
            "src/app.py",
            "## Tests",
            "### Tests executed",
            "pytest -q",
            "### Test results",
            "## Measurements",
            "## Uncertainty",
            "## Human review",
            "Human review required: yes",
            "## Inspect",
            "git -C /work/ns-abc status",
        ):
            self.assertIn(label, text)
        self.assertIn("finding: the parser rejected an empty fixture", text)
        self.assertNotEqual(text.strip(), "failed")


class FakeProviderTests(unittest.TestCase):
    def test_success_commits_inside_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            make_repo(repo)
            stdout = root / "stdout.log"
            stderr = root / "stderr.log"
            request = ProviderRequest(
                run_id="ns-success",
                workspace=repo,
                prompt_path=root / "prompt.md",
                stdout_path=stdout,
                stderr_path=stderr,
                env={"PATH": __import__("os").environ.get("PATH", ""), "GIT_TERMINAL_PROMPT": "0"},
                timeout=30,
                write_scope="workspace",
                session_id="sess-success",
                model=None,
                max_turns=1,
            )
            result = FakeProvider().execute(request)
            self.assertEqual(result.exit_code, 0)
            self.assertIsNotNone(result.pid)
            self.assertIn("finding:", stdout.read_text(encoding="utf-8"))
            self.assertTrue((repo / "nightshift-notes" / "result.md").is_file())
            log = subprocess.check_output(["git", "-C", str(repo), "log", "--format=%s"], text=True)
            self.assertIn("nightshift: record fake provider note", log)

    def test_failure_keeps_findings_and_skips_the_commit(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            make_repo(repo)
            stdout = root / "stdout.log"
            stderr = root / "stderr.log"
            request = ProviderRequest(
                run_id="ns-fail",
                workspace=repo,
                prompt_path=root / "prompt.md",
                stdout_path=stdout,
                stderr_path=stderr,
                env={"PATH": __import__("os").environ.get("PATH", "")},
                timeout=30,
                write_scope="workspace",
                session_id="sess-fail",
                model=None,
                max_turns=1,
            )
            old = os.environ.get("NIGHTSHIFT_FAKE_RESULT")
            os.environ["NIGHTSHIFT_FAKE_RESULT"] = "fail"
            try:
                result = FakeProvider().execute(request)
            finally:
                if old is None:
                    os.environ.pop("NIGHTSHIFT_FAKE_RESULT", None)
                else:
                    os.environ["NIGHTSHIFT_FAKE_RESULT"] = old
            self.assertNotEqual(result.exit_code, 0)
            text = stdout.read_text(encoding="utf-8")
            self.assertIn("finding: the requested check could not be completed", text)
            self.assertIn("hypothesis:", text)
            log = subprocess.check_output(["git", "-C", str(repo), "log", "--format=%s"], text=True)
            self.assertNotIn("nightshift: record fake provider note", log)


class RecoverTests(unittest.TestCase):
    def test_each_recovery_class_and_no_provider_launch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = load_config(root)
            config.ensure_dirs()
            db = Database(config.db_path)
            locks = LockManager(db)
            workspace = root / "intact"
            make_repo(workspace)
            dead = subprocess.Popen(["true"])
            dead.wait(timeout=5)
            cases = {
                "alive": empty_run(
                    job_id="alive",
                    provider="fake",
                    state=RunState.RUNNING.value,
                    attempt=1,
                    max_attempts=2,
                    job_snapshot=_job_snapshot(),
                    workspace_path=str(workspace),
                    source_repo=str(workspace),
                    source_revision="abc",
                    run_dir=str(root / "runs" / "alive"),
                ),
            }
            # Alive is probed with a real sleep process below. The table drives
            # the other classes through the shipped recover_run function.
            del cases
            sleeper = subprocess.Popen(["sleep", "30"])
            try:
                alive = db.insert_run(
                    empty_run(
                        job_id="alive",
                        provider="fake",
                        state=RunState.RUNNING.value,
                        attempt=1,
                        max_attempts=2,
                        pid=sleeper.pid,
                        process_meta={"match": "sleep", "pid": sleeper.pid, "pgid": sleeper.pid},
                        job_snapshot=_job_snapshot(),
                        workspace_path=str(workspace),
                        run_dir=str(config.runs_dir / "alive"),
                    )
                )
                decision = classify_recovery(alive, probe_run(alive))
                self.assertEqual(decision.classification, "process_still_alive")
                self.assertFalse(decision.launch_provider)
                updated, applied = recover_run(config, db, locks, alive.run_id)
                self.assertEqual(applied.classification, "process_still_alive")
                self.assertEqual(updated.state, RunState.RUNNING.value)
                self.assertEqual(updated.attempt, 1)
                self.assertFalse(applied.launch_provider)
            finally:
                sleeper.kill()
                sleeper.wait(timeout=5)

            gone = db.insert_run(
                empty_run(
                    job_id="gone",
                    provider="fake",
                    state=RunState.RUNNING.value,
                    attempt=1,
                    max_attempts=2,
                    pid=dead.pid,
                    process_meta={"match": "nightshift-not-running", "pid": dead.pid},
                    job_snapshot=_job_snapshot(),
                    workspace_path=str(workspace),
                    source_repo=str(workspace),
                    source_revision="unused",
                    run_dir=str(config.runs_dir / "gone"),
                    provider_exit_code=None,
                )
            )
            updated, applied = recover_run(config, db, locks, gone.run_id)
            self.assertEqual(applied.classification, "process_gone_workspace_intact")
            self.assertEqual(updated.state, RunState.INTERRUPTED.value)
            self.assertEqual(updated.attempt, 1)
            self.assertFalse(applied.launch_provider)
            self.assertFalse((workspace / "nightshift-notes").exists())

            missing_path = root / "missing-workspace"
            self.assertFalse(missing_path.exists())
            missing = db.insert_run(
                empty_run(
                    job_id="missing",
                    provider="fake",
                    state=RunState.RUNNING.value,
                    attempt=1,
                    max_attempts=2,
                    pid=dead.pid,
                    process_meta={"match": "nightshift-not-running", "pid": dead.pid},
                    job_snapshot=_job_snapshot(),
                    workspace_path=str(missing_path),
                    source_repo=str(workspace),
                    source_revision="unused",
                    run_dir=str(config.runs_dir / "missing"),
                    provider_exit_code=None,
                )
            )
            updated, applied = recover_run(config, db, locks, missing.run_id)
            self.assertEqual(applied.classification, "process_gone_workspace_missing")
            self.assertNotIn("intact", applied.classification)
            self.assertEqual(updated.state, RunState.FAILED.value)
            self.assertEqual(updated.recovery_class, "process_gone_workspace_missing")
            self.assertEqual(updated.attempt, 1)
            self.assertFalse(applied.launch_provider)
            self.assertFalse(missing_path.exists())

            before = db.insert_run(
                empty_run(
                    job_id="before",
                    provider="fake",
                    state=RunState.PREPARING.value,
                    attempt=1,
                    max_attempts=2,
                    job_snapshot=_job_snapshot(),
                    run_dir=str(config.runs_dir / "before"),
                )
            )
            updated, applied = recover_run(config, db, locks, before.run_id)
            self.assertEqual(applied.classification, "interrupted_before_launch")
            self.assertEqual(updated.state, RunState.INTERRUPTED.value)
            self.assertFalse(applied.launch_provider)

            verify_ws = root / "verify-ws"
            make_repo(verify_ws)
            (verify_ws / "marker.txt").write_text("ok\n", encoding="utf-8")
            pending = db.insert_run(
                empty_run(
                    job_id="pending",
                    provider="fake",
                    state=RunState.RUNNING.value,
                    attempt=1,
                    max_attempts=2,
                    pid=dead.pid,
                    process_meta={"match": "gone-provider"},
                    provider_exit_code=0,
                    verification_ran=False,
                    job_snapshot=_job_snapshot(["test -f marker.txt"]),
                    workspace_path=str(verify_ws),
                    source_repo=str(verify_ws),
                    source_revision="unused",
                    run_dir=str(config.runs_dir / "pending"),
                    source_head="unused",
                    base_ref="HEAD",
                    source_integrity=dumps(capture(verify_ws)),
                )
            )
            updated, applied = recover_run(config, db, locks, pending.run_id)
            self.assertEqual(applied.classification, "verification_never_ran")
            self.assertFalse(applied.launch_provider)
            self.assertEqual(updated.state, RunState.SUCCEEDED.value)
            self.assertEqual(updated.attempt, 1)
            self.assertTrue(updated.verification_ran)

            done = db.insert_run(
                empty_run(
                    job_id="done",
                    provider="fake",
                    state=RunState.VERIFYING.value,
                    attempt=1,
                    max_attempts=2,
                    provider_exit_code=0,
                    verification_exit_code=0,
                    verification_ran=True,
                    job_snapshot=_job_snapshot(),
                    workspace_path=str(workspace),
                    run_dir=str(config.runs_dir / "done"),
                )
            )
            updated, applied = recover_run(config, db, locks, done.run_id)
            self.assertEqual(applied.classification, "provider_exited")
            self.assertFalse(applied.launch_provider)
            self.assertEqual(updated.state, RunState.SUCCEEDED.value)
            self.assertEqual(updated.attempt, 1)

            tainted = root / "tainted-ws"
            make_repo(tainted)
            (tainted / "marker.txt").write_text("ok\n", encoding="utf-8")
            tainted_run = db.insert_run(
                empty_run(
                    job_id="tainted",
                    provider="fake",
                    state=RunState.VERIFYING.value,
                    attempt=1,
                    max_attempts=2,
                    provider_exit_code=0,
                    verification_exit_code=0,
                    verification_ran=True,
                    job_snapshot=_job_snapshot(["test -f marker.txt"]),
                    workspace_path=str(tainted),
                    source_repo=str(tainted),
                    source_revision="unused",
                    run_dir=str(config.runs_dir / "tainted"),
                    source_head="unused",
                    base_ref="HEAD",
                    source_integrity=dumps(capture(tainted)),
                )
            )
            subprocess.check_call(["git", "-C", str(tainted), "config", "--local", "nightshift.tainted", "1"])
            updated, applied = recover_run(config, db, locks, tainted_run.run_id)
            self.assertEqual(applied.classification, "provider_exited")
            self.assertFalse(applied.launch_provider)
            self.assertNotEqual(updated.state, RunState.SUCCEEDED.value)
            self.assertIn("SOURCE_INTEGRITY_VIOLATION", updated.failure_reason)

            queued = db.insert_run(empty_run(job_id="queued", provider="fake", state=RunState.QUEUED.value))
            decision = classify_recovery(queued, ProcessProbe(False, False))
            self.assertEqual(decision.classification, "queued_not_started")
            self.assertFalse(decision.launch_provider)
            db.close()

    def test_recover_cannot_succeed_when_source_integrity_changed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = load_config(root)
            config.ensure_dirs()
            db = Database(config.db_path)
            locks = LockManager(db)
            repo = root / "repo"
            make_repo(repo)
            (repo / "marker.txt").write_text("ok\n", encoding="utf-8")
            dead = subprocess.Popen(["true"])
            dead.wait(timeout=5)
            run = db.insert_run(
                empty_run(
                    job_id="bypass",
                    provider="fake",
                    state=RunState.RUNNING.value,
                    attempt=1,
                    max_attempts=2,
                    pid=dead.pid,
                    process_meta={"match": "gone-provider"},
                    provider_exit_code=0,
                    verification_ran=False,
                    job_snapshot=_job_snapshot(["test -f marker.txt"]),
                    workspace_path=str(repo),
                    source_repo=str(repo),
                    source_revision="unused",
                    run_dir=str(config.runs_dir / "bypass"),
                    source_head="unused",
                    base_ref="HEAD",
                    source_integrity=dumps(capture(repo)),
                )
            )
            subprocess.check_call(["git", "-C", str(repo), "config", "--local", "nightshift.bypass", "1"])
            updated, applied = recover_run(config, db, locks, run.run_id)
            self.assertEqual(applied.classification, "verification_never_ran")
            self.assertFalse(applied.launch_provider)
            self.assertTrue(updated.verification_ran)
            self.assertEqual(updated.verification_exit_code, 0)
            self.assertNotEqual(updated.state, RunState.SUCCEEDED.value)
            self.assertIn("SOURCE_INTEGRITY_VIOLATION", updated.failure_reason)
            db.close()

    def test_retry_stops_at_max_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            db = Database(Path(raw) / "nightshift.db")
            run = db.insert_run(
                empty_run(
                    job_id="retry",
                    provider="fake",
                    state=RunState.INTERRUPTED.value,
                    attempt=1,
                    max_attempts=1,
                )
            )
            with self.assertRaises(Exception):
                retry_run(db, run.run_id)
            self.assertEqual(db.require_run(run.run_id).attempt, 1)
            self.assertEqual(db.require_run(run.run_id).state, RunState.INTERRUPTED.value)
            room = db.insert_run(
                empty_run(
                    job_id="retry-ok",
                    provider="fake",
                    state=RunState.FAILED.value,
                    attempt=1,
                    max_attempts=2,
                )
            )
            retried = retry_run(db, room.run_id)
            self.assertEqual(retried.state, RunState.QUEUED.value)
            self.assertEqual(retried.attempt, 2)
            db.close()


if __name__ == "__main__":
    unittest.main()
