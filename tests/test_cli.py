import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from nightshift.testkit import ROOT, cli_env, make_repo, run_cli, snapshot, write_job


class CliTests(unittest.TestCase):
    def test_daemon_runs_one_queued_job_then_stops(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            make_repo(source)
            home = root / "home"
            job = write_job(root / "job", source, id="daemon-job", concurrency_group="daemon")
            queued = run_cli(home, ["queue", "add", str(job), "--provider", "fake"])
            self.assertEqual(queued.returncode, 0, queued.stderr)
            proc = subprocess.Popen(
                [sys.executable, "-m", "nightshift", "--root", str(home), "daemon"],
                cwd=str(ROOT),
                env=cli_env(),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 20
                seen = ""
                while time.monotonic() < deadline:
                    listed = run_cli(home, ["queue", "list"])
                    seen = listed.stdout
                    if "SUCCEEDED" in seen:
                        break
                    time.sleep(0.3)
                self.assertIn("SUCCEEDED", seen, proc.stderr.read() if proc.poll() is not None else seen)
            finally:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()

    def test_validate_example_and_reject_invalid(self) -> None:
        ok = run_cli(ROOT, ["job", "validate", "jobs/examples/repo_audit"])
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("repo-audit-example", ok.stdout)
        with tempfile.TemporaryDirectory() as raw:
            bad = Path(raw) / "job.toml"
            bad.write_text('schema_version = 1\ndescription = "missing id"\n', encoding="utf-8")
            (Path(raw) / "prompt.md").write_text("x\n", encoding="utf-8")
            failed = run_cli(ROOT, ["job", "validate", raw])
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("id", failed.stderr)

    def test_fake_run_leaves_a_dirty_source_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            head = make_repo(source, dirty=True)
            before_head, before_status = snapshot(source)
            job = write_job(root / "job", source, id="e2e-job", concurrency_group="e2e")
            state = root / "nightshift-home"
            result = run_cli(state, ["run", str(job), "--provider", "fake"])
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("SUCCEEDED", result.stdout)
            after_head, after_status = snapshot(source)
            self.assertEqual(after_head, before_head)
            self.assertEqual(after_status, before_status)
            self.assertEqual(after_head, head)
            self.assertTrue((source / "DIRTY.txt").is_file())
            self.assertEqual((source / "DIRTY.txt").read_text(encoding="utf-8"), "dirty\n")
            run_id = result.stdout.split()[1]
            run_dir = state / "runs" / run_id
            for name in ("metadata.json", "events.jsonl", "report.md"):
                self.assertTrue((run_dir / name).is_file(), name)
            attempt = run_dir / "attempt-1"
            for name in (
                "prompt.final.md",
                "provider.stdout.log",
                "provider.stderr.log",
                "verification.log",
            ):
                self.assertTrue((attempt / name).is_file(), name)
            report = (run_dir / "report.md").read_text(encoding="utf-8")
            self.assertIn(str(source), report)
            self.assertIn(head, report)
            self.assertIn("nightshift: record fake provider note", report)
            self.assertIn("Human review required: yes", report)
            self.assertGreater(len(report.strip()), 200)
            listed = run_cli(state, ["queue", "list"])
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertIn(run_id, listed.stdout)
            self.assertIn("SUCCEEDED", listed.stdout)
            status = run_cli(state, ["status"])
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn(run_id, status.stdout)
            logs = run_cli(state, ["logs", run_id])
            self.assertEqual(logs.returncode, 0, logs.stderr)
            self.assertIn(run_id, logs.stdout)
            self.assertIn("SUCCEEDED", logs.stdout)
            shown = run_cli(state, ["report", run_id])
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertIn(head, shown.stdout)
            missing = run_cli(state, ["logs", "ns-does-not-exist"])
            self.assertNotEqual(missing.returncode, 0)
            self.assertNotEqual(missing.returncode, result.returncode)

    def test_failed_fake_run_preserves_findings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            make_repo(source)
            job = write_job(root / "job", source, id="fail-job", verification=[])
            result = run_cli(root / "home", ["run", str(job), "--provider", "fake"], NIGHTSHIFT_FAKE_RESULT="fail")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("FAILED", result.stdout)
            run_id = result.stdout.split()[1]
            report = (root / "home" / "runs" / run_id / "report.md").read_text(encoding="utf-8")
            self.assertIn("finding:", report)
            self.assertIn("hypothesis:", report)
            self.assertTrue((root / "home" / "worktrees" / run_id).is_dir())

    def test_recover_dead_pid_and_cancel_queued(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            make_repo(source)
            home = root / "home"
            job = write_job(root / "job", source, id="cancel-me", concurrency_group="cancel")
            queued = run_cli(home, ["queue", "add", str(job), "--provider", "fake"])
            self.assertEqual(queued.returncode, 0, queued.stderr)
            queued_id = queued.stdout.split()[1]
            cancelled = run_cli(home, ["cancel", queued_id])
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
            self.assertIn("CANCELLED", cancelled.stdout)
            listed = run_cli(home, ["queue", "list"])
            self.assertIn("CANCELLED", listed.stdout)

            dead = subprocess.Popen(["true"])
            dead.wait(timeout=5)
            from nightshift.config import load_config
            from nightshift.db import Database
            from nightshift.models import RunState, empty_run

            config = load_config(home)
            db = Database(config.db_path)
            run = db.insert_run(
                empty_run(
                    job_id="stale",
                    provider="fake",
                    state=RunState.RUNNING.value,
                    attempt=1,
                    max_attempts=2,
                    pid=dead.pid,
                    process_meta={"match": "not-a-live-nightshift-process"},
                    workspace_path=str(source),
                    source_repo=str(source),
                    source_revision="unknown",
                    run_dir=str(config.runs_dir / "stale"),
                    job_snapshot=(
                        '{"schema_version":1,"id":"stale","description":"stale","type":"note",'
                        '"repository":"/tmp/unused","base_ref":"HEAD","provider":"fake","model":null,'
                        '"max_runtime_seconds":30,"max_attempts":2,"concurrency_group":"stale",'
                        '"network":false,"write_scope":"none","expected_artifacts":[],"verification":[],'
                        '"success_criteria":["kept"],"allow_bash":[],"prompt":"noop\\n","job_dir":"/tmp",'
                        '"job_file":"/tmp/job.toml"}'
                    ),
                )
            )
            db.close()
            recovered = run_cli(home, ["recover"])
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertIn(run.run_id, recovered.stdout)
            self.assertIn("INTERRUPTED", recovered.stdout)
            self.assertIn("process_gone_workspace_intact", recovered.stdout)
            self.assertIn("attempt=1", recovered.stdout)
            self.assertNotIn("fake provider start", recovered.stdout)


if __name__ == "__main__":
    unittest.main()
