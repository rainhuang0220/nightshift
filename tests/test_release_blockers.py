"""Regressions for the 0.2.x release blockers. These tests do not call Grok."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from nightshift.config import load_config
from nightshift.containment import build_read_policy, contained_run, write_profile
from nightshift.db import Database
from nightshift.gitutil import add_detached_worktree
from nightshift.locks import LockManager, group_key, phase_process_alive, process_start_token, repo_key
from nightshift.models import IllegalTransition, NightshiftError, RunState, empty_run
from nightshift.priv import chmod_private_file, open_private, open_private_binary, write_private_text
from nightshift.providers.grok import build_grok_argv
from nightshift.policy import build_policy
from nightshift.queue import claim_next, enqueue
from nightshift.report import ReportInputs, provider_conclusion, render_report
from nightshift.runner import execute_run, provider_write_roots
from nightshift.supervisor import cancel_run, probe_run, recover_run, retry_run
from nightshift.testkit import make_repo, write_job
from nightshift.job import load_job
from nightshift.workspace import inspect_source, prepare_workspace


def _outside(root: Path, name: str) -> tuple[Path, Path]:
    target = root / "outside.txt"
    target.write_bytes(b"KEEP-ME")
    link = root / name
    link.symlink_to(target)
    return target, link


class SymlinkWriterTests(unittest.TestCase):
    def test_control_file_symlinks_are_not_followed(self) -> None:
        writers = {
            "report.md": lambda path: write_private_text(path, "TRUNCATED"),
            "verification.log": lambda path: write_private_text(path, "TRUNCATED"),
            "metadata.json": lambda path: write_private_text(path, "TRUNCATED"),
            "events.jsonl": lambda path: open_private(path, append=True).write("TRUNCATED\n"),
            "provider.stdout.log": lambda path: open_private_binary(path).write(b"TRUNCATED"),
            "provider.stderr.log": lambda path: open_private_binary(path).write(b"TRUNCATED"),
        }
        with __import__("tempfile").TemporaryDirectory() as raw:
            root = Path(raw)
            for name, writer in writers.items():
                with self.subTest(name=name):
                    target, link = _outside(root, name)
                    before = target.read_bytes()
                    mode = stat.S_IMODE(target.stat().st_mode)
                    with self.assertRaises(NightshiftError):
                        writer(link)
                    self.assertEqual(target.read_bytes(), before)
                    self.assertEqual(stat.S_IMODE(target.stat().st_mode), mode)
                    self.assertTrue(link.is_symlink())
                    link.unlink()

    def test_chmod_does_not_follow_a_symlink(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as raw:
            root = Path(raw)
            target, link = _outside(root, "report.md")
            os.chmod(target, 0o644)
            with self.assertRaises(NightshiftError):
                chmod_private_file(link)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
            self.assertEqual(target.read_bytes(), b"KEEP-ME")
            self.assertTrue(link.is_symlink())


class ContainmentBoundaryTests(unittest.TestCase):
    def test_provider_cannot_write_control_files_and_can_write_the_workspace(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir()
            workspace = root / "workspace"
            workspace.mkdir()
            attempt = root / "attempt-1"
            attempt.mkdir()
            (attempt / "runtime-home").mkdir()
            (attempt / "tmp").mkdir()
            (attempt / "grok-home").mkdir()
            prompt = attempt / "prompt.final.md"
            prompt.write_text("KEEP\n", encoding="utf-8")
            shim = attempt / "bin" / "git"
            shim.parent.mkdir()
            shim.write_text("#!/bin/sh\n", encoding="utf-8")
            launcher = attempt / "verify-runtime" / "launcher"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("KEEP\n", encoding="utf-8")
            report = root / "report.md"
            report.write_text("KEEP\n", encoding="utf-8")
            outside = root / "outside.txt"
            outside.write_bytes(b"KEEP-ME")
            profile = write_profile(
                attempt / "provider.sb",
                writable=provider_write_roots(workspace, attempt, write_scope="workspace"),
                network=False,
                read_policy=build_read_policy(
                    runtime_roots=[workspace, attempt],
                    source_root=source,
                ),
            )
            code, _output = contained_run(
                [
                    "/usr/bin/python3",
                    "-c",
                    (
                        "import os,sys\n"
                        "ok=True\n"
                        "for path in sys.argv[1:]:\n"
                        "    link=path+'.link'\n"
                        "    try:\n"
                        "        os.unlink(path); ok=False\n"
                        "    except OSError:\n"
                        "        pass\n"
                        "    try:\n"
                        "        open(path,'w').write('PWNED\\n'); ok=False\n"
                        "    except OSError:\n"
                        "        pass\n"
                        "    try:\n"
                        "        os.symlink(sys.argv[-1], link); ok=False\n"
                        "    except OSError:\n"
                        "        pass\n"
                        "sys.exit(0 if ok else 2)\n"
                    ),
                    str(prompt),
                    str(attempt / "provider.sb"),
                    str(shim),
                    str(launcher),
                    str(report),
                    str(outside),
                ],
                cwd=workspace,
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(attempt / "runtime-home"),
                    "TMPDIR": str(attempt / "tmp"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                profile=profile,
                timeout=30,
            )
            self.assertEqual(code, 0, "control-file writes were not denied")
            self.assertEqual(prompt.read_text(encoding="utf-8"), "KEEP\n")
            self.assertIn("(deny default)", (attempt / "provider.sb").read_text(encoding="utf-8"))
            self.assertEqual(shim.read_text(encoding="utf-8"), "#!/bin/sh\n")
            self.assertEqual(launcher.read_text(encoding="utf-8"), "KEEP\n")
            self.assertEqual(report.read_text(encoding="utf-8"), "KEEP\n")
            self.assertFalse(report.is_symlink())
            self.assertEqual(outside.read_bytes(), b"KEEP-ME")
            wrote, note = contained_run(
                ["/usr/bin/python3", "-c", "open('workspace-ok.txt','w').write('ok\\n')"],
                cwd=workspace,
                env={"PATH": "/usr/bin:/bin", "HOME": str(attempt / "runtime-home"), "TMPDIR": str(attempt / "tmp")},
                profile=profile,
                timeout=30,
            )
            self.assertEqual(wrote, 0, note)
            self.assertEqual((workspace / "workspace-ok.txt").read_text(encoding="utf-8"), "ok\n")

    def test_verification_runtime_is_created_after_the_provider(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            make_repo(source)
            job = write_job(root / "job", source, verification=[], expected_artifacts=[])
            os.environ["NIGHTSHIFT_FORBID_GROK"] = "1"
            config = load_config(root / "ns")
            config.ensure_dirs()
            db = Database(config.db_path)
            locks = LockManager(db)
            try:
                run = enqueue(db, job, provider="fake")
                finished = execute_run(config, db, locks, run.run_id)
            finally:
                db.close()
            self.assertEqual(finished.state, RunState.SUCCEEDED.value, finished.failure_reason)
            attempt = Path(finished.run_dir) / "attempt-1"
            provider = (attempt / "provider.sb").read_text(encoding="utf-8")
            verification = (attempt / "verification.sb").read_text(encoding="utf-8")
            self.assertNotIn("verify-runtime", provider)
            self.assertIn("verify-runtime", verification)
            self.assertIn("(deny network*)", verification)
            self.assertTrue((attempt / "verify-runtime" / "bin").is_dir())
            self.assertTrue((attempt / "verify-runtime" / "pythonpath" / "nightshift").is_dir())
            self.assertNotEqual((attempt / "bin").resolve(), (attempt / "verify-runtime" / "bin").resolve())


class AtomicLockTests(unittest.TestCase):
    def test_two_connections_have_one_winner(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as raw:
            root = Path(raw)
            for index in range(20):
                with self.subTest(index=index):
                    self.assertTrue(_one_winner(root / f"round-{index}"))

    def test_same_run_reacquire_and_partial_keys(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as raw:
            db = Database(Path(raw) / "nightshift.db")
            holder = db.insert_run(empty_run(job_id="holder", provider="fake", state=RunState.PREPARING.value))
            keys = ["group:shared", "repo:/same"]
            self.assertTrue(db.try_acquire_locks(keys, holder.run_id, os.getpid()))
            self.assertTrue(db.try_acquire_locks(keys, holder.run_id, os.getpid()))
            self.assertEqual({row["run_id"] for row in db.lock_rows()}, {holder.run_id})
            other = db.insert_run(empty_run(job_id="other", provider="fake", state=RunState.PREPARING.value))
            self.assertFalse(db.try_acquire_locks(["group:shared", "repo:/other"], other.run_id, os.getpid()))
            self.assertEqual([row["lock_key"] for row in db.lock_rows()], keys)
            self.assertTrue(all(row["run_id"] == holder.run_id for row in db.lock_rows()))
            db.close()

    def test_claim_next_uses_the_same_ownership(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            make_repo(repo)
            setup = Database(root / "nightshift.db")
            enqueue(setup, write_job(root / "job-a", repo, id="one", concurrency_group="shared"), provider="fake")
            enqueue(setup, write_job(root / "job-b", repo, id="two", concurrency_group="shared"), provider="fake")
            setup.close()
            barrier = threading.Barrier(2)
            claimed: list[str | None] = []

            def contend() -> None:
                db = Database(root / "nightshift.db")
                try:
                    barrier.wait(timeout=5)
                    run = claim_next(db)
                    claimed.append(run.run_id if run else None)
                finally:
                    db.close()

            threads = [threading.Thread(target=contend), threading.Thread(target=contend)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(sum(item is not None for item in claimed), 1)
            check = Database(root / "nightshift.db")
            try:
                owners = {row["run_id"] for row in check.lock_rows()}
                winner = next(item for item in claimed if item)
                self.assertEqual(owners, {winner})
                states = {run.run_id: run.state for run in check.list_runs()}
                self.assertEqual(states[winner], RunState.PREPARING.value)
                loser = next(run_id for run_id, state in states.items() if run_id != winner)
                self.assertEqual(states[loser], RunState.QUEUED.value)
                check.release_locks(winner)
                check.transition(winner, RunState.CANCELLED.value, "release the keys")
                follow = claim_next(check)
                self.assertIsNotNone(follow)
                assert follow is not None
                self.assertEqual(follow.run_id, loser)
            finally:
                check.close()


class PhaseAndCancelTests(unittest.TestCase):
    def test_live_preparing_is_not_recovered_as_dead(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as raw:
            root = Path(raw)
            config = load_config(root / "ns")
            config.ensure_dirs()
            db = Database(config.db_path)
            locks = LockManager(db)
            run = db.insert_run(empty_run(job_id="prep", provider="fake", state=RunState.PREPARING.value))
            identity = process_start_token(os.getpid())
            db.update_run(
                run.run_id,
                pid=os.getpid(),
                process_meta={
                    "phase": "preparing",
                    "pid": os.getpid(),
                    "match": run.run_id,
                    "identity": identity,
                },
            )
            self.assertTrue(locks.acquire([group_key("prep")], run.run_id))
            updated, decision = recover_run(config, db, locks, run.run_id)
            self.assertTrue(probe_run(updated).alive)
            self.assertEqual(decision.classification, "process_still_alive")
            self.assertEqual(decision.action, "leave")
            self.assertFalse(decision.launch_provider)
            self.assertEqual(updated.state, RunState.PREPARING.value)
            self.assertEqual(db.lock_rows()[0]["run_id"], run.run_id)
            db.close()

    def test_dead_preparing_becomes_interrupted(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as raw:
            root = Path(raw)
            config = load_config(root / "ns")
            config.ensure_dirs()
            db = Database(config.db_path)
            locks = LockManager(db)
            dead = subprocess.Popen(["true"])
            dead.wait(timeout=5)
            run = db.insert_run(empty_run(job_id="dead-prep", provider="fake", state=RunState.PREPARING.value))
            db.update_run(
                run.run_id,
                pid=dead.pid,
                process_meta={"phase": "preparing", "pid": dead.pid, "match": run.run_id, "identity": "gone"},
            )
            self.assertTrue(locks.acquire([group_key("dead")], run.run_id, pid=dead.pid))
            updated, decision = recover_run(config, db, locks, run.run_id)
            self.assertEqual(decision.classification, "interrupted_before_launch")
            self.assertFalse(decision.launch_provider)
            self.assertEqual(updated.state, RunState.INTERRUPTED.value)
            self.assertEqual(db.lock_rows(), [])
            db.close()

    def test_recycled_pid_is_not_owned(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as raw:
            meta = {"phase": "preparing", "pid": os.getpid(), "match": "controller", "identity": "not-this-process"}
            self.assertFalse(phase_process_alive(meta, fallback_pid=os.getpid()))
            config = load_config(Path(raw) / "ns")
            config.ensure_dirs()
            db = Database(config.db_path)
            locks = LockManager(db)
            run = db.insert_run(empty_run(job_id="recycled", provider="fake", state=RunState.PREPARING.value))
            db.update_run(run.run_id, pid=os.getpid(), process_meta=meta)
            updated, decision = recover_run(config, db, locks, run.run_id)
            self.assertEqual(updated.state, RunState.INTERRUPTED.value)
            self.assertFalse(decision.launch_provider)
            self.assertTrue(os.getpid())
            db.close()

    def test_live_verification_is_left_in_place(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as raw:
            root = Path(raw)
            config = load_config(root / "ns")
            config.ensure_dirs()
            db = Database(config.db_path)
            locks = LockManager(db)
            sleeper = subprocess.Popen(["sleep", "30"])
            try:
                identity = process_start_token(sleeper.pid)
                run = db.insert_run(
                    empty_run(
                        job_id="verify-live",
                        provider="fake",
                        state=RunState.VERIFYING.value,
                        verification_ran=False,
                        workspace_path=str(root / "workspace"),
                    )
                )
                (root / "workspace").mkdir()
                db.update_run(
                    run.run_id,
                    pid=sleeper.pid,
                    workspace_path=str(root / "workspace"),
                    process_meta={
                        "phase": "verifying",
                        "pid": sleeper.pid,
                        "pgid": sleeper.pid,
                        "match": run.run_id,
                        "identity": identity,
                    },
                )
                self.assertTrue(locks.acquire([group_key("verify")], run.run_id, pid=sleeper.pid))
                updated, decision = recover_run(config, db, locks, run.run_id)
                self.assertEqual(decision.classification, "verification_still_alive")
                self.assertEqual(decision.action, "leave")
                self.assertFalse(decision.launch_provider)
                self.assertEqual(updated.state, RunState.VERIFYING.value)
                self.assertFalse(updated.verification_ran)
                self.assertEqual(db.lock_rows()[0]["run_id"], run.run_id)
            finally:
                sleeper.kill()
                sleeper.wait(timeout=5)
                db.close()

    def test_cancel_during_provider_and_verification_cannot_succeed(self) -> None:
        self._cancel_while(
            hold=True,
            verification=["true"],
            phase="provider",
        )
        self._cancel_while(
            hold=False,
            verification=["sleep 30"],
            phase="verifying",
        )

    def _cancel_while(self, *, hold: bool, verification: list[str], phase: str) -> None:
        previous_result = os.environ.get("NIGHTSHIFT_FAKE_RESULT")
        previous_hold = os.environ.get("NIGHTSHIFT_FAKE_HOLD_SECONDS")
        previous_forbid = os.environ.get("NIGHTSHIFT_FORBID_GROK")
        if hold:
            os.environ["NIGHTSHIFT_FAKE_RESULT"] = "hold"
            os.environ["NIGHTSHIFT_FAKE_HOLD_SECONDS"] = "20"
        else:
            os.environ.pop("NIGHTSHIFT_FAKE_RESULT", None)
            os.environ.pop("NIGHTSHIFT_FAKE_HOLD_SECONDS", None)
        os.environ["NIGHTSHIFT_FORBID_GROK"] = "1"
        try:
            with __import__("tempfile").TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / "source"
                make_repo(source)
                job = write_job(
                    root / "job",
                    source,
                    verification=verification,
                    expected_artifacts=[],
                    success_criteria=["cancelled before success"],
                    max_runtime_seconds=60,
                )
                config = load_config(root / "ns")
                config.ensure_dirs()
                db = Database(config.db_path)
                locks = LockManager(db)
                run = enqueue(db, job, provider="fake")
                box: dict[str, object] = {}

                def work() -> None:
                    box["finished"] = execute_run(config, db, locks, run.run_id)

                worker = threading.Thread(target=work)
                worker.start()
                deadline = time.time() + 25
                seen = None
                try:
                    while time.time() < deadline:
                        current = db.get_run(run.run_id)
                        meta = current.process_meta if current and isinstance(current.process_meta, dict) else {}
                        if current and current.state in {RunState.RUNNING.value, RunState.VERIFYING.value} and meta.get("phase") == phase:
                            seen = current
                            break
                        time.sleep(0.05)
                    self.assertIsNotNone(seen, phase)
                    assert seen is not None
                    child = int(seen.process_meta["pid"])
                    cancelled = cancel_run(config, db, locks, run.run_id)
                    self.assertEqual(cancelled.state, RunState.CANCELLED.value)
                    worker.join(timeout=15)
                    self.assertFalse(worker.is_alive())
                    finished = box["finished"]
                    self.assertEqual(finished.state, RunState.CANCELLED.value)
                    gone = time.time() + 5
                    while time.time() < gone and _pid_alive(child):
                        time.sleep(0.05)
                    self.assertFalse(_pid_alive(child))
                    with self.assertRaises(IllegalTransition):
                        db.transition(run.run_id, RunState.SUCCEEDED.value, "must not succeed")
                    self.assertFalse(probe_run(db.require_run(run.run_id)).alive or False)
                finally:
                    if worker.is_alive():
                        current = db.get_run(run.run_id)
                        if current and current.pid:
                            subprocess.run(["kill", "-TERM", str(current.pid)], check=False)
                        worker.join(timeout=5)
                    db.close()
        finally:
            _restore("NIGHTSHIFT_FAKE_RESULT", previous_result)
            _restore("NIGHTSHIFT_FAKE_HOLD_SECONDS", previous_hold)
            _restore("NIGHTSHIFT_FORBID_GROK", previous_forbid)


class RetryAndReportTests(unittest.TestCase):
    def test_retry_is_a_new_attempt_and_keeps_the_old_evidence(self) -> None:
        previous = os.environ.get("NIGHTSHIFT_FAKE_RESULT")
        os.environ["NIGHTSHIFT_FAKE_RESULT"] = "fail"
        os.environ["NIGHTSHIFT_FORBID_GROK"] = "1"
        try:
            with __import__("tempfile").TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / "source"
                make_repo(source)
                job = write_job(
                    root / "job",
                    source,
                    max_attempts=2,
                    verification=["test -f nightshift-notes/result.md"],
                    expected_artifacts=[],
                )
                config = load_config(root / "ns")
                config.ensure_dirs()
                db = Database(config.db_path)
                locks = LockManager(db)
                try:
                    queued = enqueue(db, job, provider="fake")
                    first = execute_run(config, db, locks, queued.run_id)
                    self.assertEqual(first.state, RunState.FAILED.value, first.failure_reason)
                    self.assertEqual(first.attempt, 1)
                    attempt_one = Path(first.run_dir) / "attempt-1" / "provider.stdout.log"
                    evidence = attempt_one.read_bytes()
                    self.assertIn(b"could not be completed", evidence)
                    (Path(first.run_dir) / "attempt-1" / "grok-home" / "marker.txt").write_text("attempt-1\n", encoding="utf-8")
                    retried = retry_run(db, first.run_id)
                    self.assertEqual(retried.attempt, 2)
                    self.assertEqual(retried.state, RunState.QUEUED.value)
                    self.assertEqual(retried.session_id, "")
                    self.assertEqual(retried.started_at, "")
                    self.assertEqual(retried.process_meta, {})
                    self.assertEqual(retried.invocation, {})
                    self.assertEqual(retried.provider_argv, [])
                    self.assertEqual(attempt_one.read_bytes(), evidence)
                    os.environ["NIGHTSHIFT_FAKE_RESULT"] = "success"
                    second = execute_run(config, db, locks, first.run_id)
                finally:
                    db.close()
                self.assertEqual(second.attempt, 2)
                self.assertNotEqual(second.session_id, first.session_id)
                self.assertTrue(second.session_id)
                self.assertNotEqual(second.started_at, first.started_at)
                self.assertEqual(attempt_one.read_bytes(), evidence)
                attempt_two = Path(second.run_dir) / "attempt-2" / "provider.stdout.log"
                self.assertIn(b"fake_result=success", attempt_two.read_bytes())
                self.assertNotIn(b"could not be completed", attempt_two.read_bytes())
                self.assertTrue((Path(second.run_dir) / "attempt-1" / "grok-home" / "marker.txt").is_file())
                self.assertFalse((Path(second.run_dir) / "attempt-2" / "grok-home" / "marker.txt").exists())
                self.assertNotIn("--resume", second.provider_argv)
                report = (Path(second.run_dir) / "report.md").read_text(encoding="utf-8")
                self.assertIn("attempt 2", report)
                policy = build_policy(load_job(job))
                fresh = build_grok_argv(
                    binary="grok",
                    cwd=source,
                    prompt_file=source / "prompt.md",
                    session_id=second.session_id,
                    model=None,
                    max_turns=2,
                    policy=policy,
                    resume=False,
                )
                self.assertNotIn("--resume", fresh)
                self.assertIn("--session-id", fresh)
        finally:
            _restore("NIGHTSHIFT_FAKE_RESULT", previous)

    def test_fragmented_text_events_become_the_provider_conclusion(self) -> None:
        raw = (
            '{"type":"thought","data":"hidden chain"}\n'
            '{"type":"text","data":"The ripwire parser "}\n'
            '{"type":"text","data":"drops a bare reply."}\n'
            '{"type":"usage","usage":{"output_tokens":1}}\n'
        )
        conclusion = provider_conclusion(raw)
        self.assertEqual(conclusion, "The ripwire parser drops a bare reply.")
        self.assertNotIn("hidden", conclusion)
        text = render_report(
            ReportInputs(
                run_id="ns-conclusion",
                job_id="audit",
                description="Audit",
                job_type="audit",
                state="SUCCEEDED",
                provider="fake",
                attempt=2,
                max_attempts=2,
                source_repo="/repos/demo",
                source_revision="abc",
                source_head="abc",
                base_ref="HEAD",
                workspace="/work/ns",
                session_id="sess",
                started_at="t0",
                ended_at="t1",
                duration_seconds=1,
                exit_code=0,
                provider_exit_code=0,
                verification_exit_code=0,
                failure_reason="",
                findings="",
                conclusion=conclusion,
            )
        )
        self.assertIn("## Provider conclusion", text)
        self.assertIn("The ripwire parser drops a bare reply.", text)
        self.assertIn("This report describes attempt 2.", text)
        self.assertIn("declared check passed inside the isolated mutable workspace", text)
        self.assertNotIn("hidden chain", text)
        self.assertNotIn("finding:", text.split("## Provider conclusion", 1)[1])


class CheckoutHookTests(unittest.TestCase):
    def test_clone_and_worktree_ignore_ambient_hooks(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source"
            make_repo(source)
            hooks = root / "hooks"
            hooks.mkdir()
            marker = root / "hook-ran"
            hook = hooks / "post-checkout"
            hook.write_text("#!/bin/sh\necho ran > " + str(marker) + "\n", encoding="utf-8")
            hook.chmod(0o755)
            gitconfig = root / "gitconfig"
            gitconfig.write_text("[core]\n\thooksPath = " + str(hooks) + "\n", encoding="utf-8")
            user_config = Path.home() / ".gitconfig"
            before = user_config.read_bytes() if user_config.is_file() else None
            previous = os.environ.get("GIT_CONFIG_GLOBAL")
            os.environ["GIT_CONFIG_GLOBAL"] = str(gitconfig)
            try:
                prepare_workspace(inspect_source(source, "HEAD"), root / "clone", "clone")
                self.assertFalse(marker.exists())
                self.assertTrue((root / "clone" / ".git").exists())
                add_detached_worktree(source, root / "worktree", "HEAD")
                self.assertFalse(marker.exists())
                self.assertTrue((root / "worktree" / ".git").exists())
            finally:
                _restore("GIT_CONFIG_GLOBAL", previous)
                subprocess.run(
                    ["git", "-C", str(source), "worktree", "remove", "--force", str(root / "worktree")],
                    check=False,
                    capture_output=True,
                )
            after = user_config.read_bytes() if user_config.is_file() else None
            self.assertEqual(before, after)


def _one_winner(directory: Path) -> bool:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "nightshift.db"
    setup = Database(path)
    left = setup.insert_run(empty_run(job_id="left", provider="fake", state=RunState.PREPARING.value))
    right = setup.insert_run(empty_run(job_id="right", provider="fake", state=RunState.PREPARING.value))
    left_id, right_id = left.run_id, right.run_id
    setup.close()
    barrier = threading.Barrier(2)
    results: list[tuple[str, bool]] = []

    def contend(run_id: str) -> None:
        db = Database(path)
        try:
            barrier.wait(timeout=5)
            results.append((run_id, db.try_acquire_locks(["group:shared", repo_key("/same")], run_id, os.getpid())))
        finally:
            db.close()

    threads = [threading.Thread(target=contend, args=(left_id,)), threading.Thread(target=contend, args=(right_id,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    if len(results) != 2 or sum(won for _, won in results) != 1:
        return False
    check = Database(path)
    try:
        winner = next(run_id for run_id, won in results if won)
        loser = next(run_id for run_id, won in results if not won)
        rows = check.lock_rows()
        if len(rows) != 2 or any(row["run_id"] != winner for row in rows):
            return False
        check.release_locks(winner)
        if check.lock_rows():
            return False
        if not check.try_acquire_locks(["group:shared", repo_key("/same")], loser, os.getpid()):
            return False
        owned = check.lock_rows()
        return len(owned) == 2 and all(row["run_id"] == loser for row in owned)
    finally:
        check.close()


_LIVE_SNAPSHOT = (
    '{"schema_version":1,"id":"live-auth","description":"live","type":"note",'
    '"repository":"/tmp/unused","base_ref":"HEAD","provider":"fake","model":null,'
    '"max_runtime_seconds":30,"max_attempts":1,"concurrency_group":"live-auth",'
    '"network":false,"write_scope":"none","expected_artifacts":[],"verification":[],'
    '"success_criteria":["kept"],"allow_bash":[],"prompt":"noop\\n","job_dir":"/tmp",'
    '"job_file":"/tmp/job.toml"}'
)


class LiveRecoveryAuthTests(unittest.TestCase):
    def test_live_preparing_keeps_attempt_auth(self) -> None:
        self._live(
            state=RunState.PREPARING.value,
            phase="preparing",
            classification="process_still_alive",
            use_self=True,
        )

    def test_live_inspect_keeps_attempt_auth(self) -> None:
        self._live(
            state=RunState.PREPARING.value,
            phase="inspect",
            classification="process_still_alive",
            use_self=False,
        )

    def test_live_provider_keeps_attempt_auth(self) -> None:
        self._live(
            state=RunState.RUNNING.value,
            phase="provider",
            classification="process_still_alive",
            use_self=False,
        )

    def test_live_verifying_does_not_mutate_runtime(self) -> None:
        self._live(
            state=RunState.VERIFYING.value,
            phase="verifying",
            classification="verification_still_alive",
            use_self=False,
        )

    def test_dead_preparing_removes_attempt_auth(self) -> None:
        self._dead(state=RunState.PREPARING.value, phase="preparing", expect=RunState.INTERRUPTED.value)

    def test_dead_provider_removes_attempt_auth(self) -> None:
        self._dead(state=RunState.RUNNING.value, phase="provider", expect=RunState.INTERRUPTED.value)

    def test_live_child_then_dead_recovery_removes_stale_auth(self) -> None:
        token = "nightshift-canary-token"
        child = _phase_child(token)
        try:
            with __import__("tempfile").TemporaryDirectory() as raw:
                config, db, locks, run, auth, payload, note, store = self._case(
                    Path(raw),
                    state=RunState.RUNNING.value,
                    phase="provider",
                    pid=child.pid,
                    token=token,
                    identity=process_start_token(child.pid),
                    workspace=True,
                )
                try:
                    updated, decision = recover_run(config, db, locks, run.run_id)
                    self.assertEqual(decision.classification, "process_still_alive")
                    self.assertEqual(decision.action, "leave")
                    self.assertEqual(updated.state, RunState.RUNNING.value)
                    self.assertEqual(auth.read_bytes(), payload)
                    self.assertEqual(note.read_text(encoding="utf-8"), "keep\n")
                    self.assertEqual(db.lock_rows()[0]["run_id"], run.run_id)
                    self.assertEqual(store.read_bytes(), b"synthetic-persistent-store\n")
                    child.kill()
                    child.wait(timeout=5)
                    updated, decision = recover_run(config, db, locks, run.run_id)
                    self.assertEqual(updated.state, RunState.INTERRUPTED.value)
                    self.assertEqual(decision.action, "mark")
                    self.assertFalse(decision.launch_provider)
                    self.assertFalse(auth.exists())
                    self.assertEqual(db.lock_rows(), [])
                    self.assertEqual(store.read_bytes(), b"synthetic-persistent-store\n")
                finally:
                    db.close()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def _live(self, *, state: str, phase: str, classification: str, use_self: bool) -> None:
        token = "nightshift-live-token"
        child = None if use_self else _phase_child(token)
        try:
            pid = os.getpid() if child is None else child.pid
            with __import__("tempfile").TemporaryDirectory() as raw:
                config, db, locks, run, auth, payload, note, store = self._case(
                    Path(raw),
                    state=state,
                    phase=phase,
                    pid=pid,
                    token=token,
                    identity=process_start_token(pid),
                    workspace=True,
                )
                # run_token needs the inserted run id for preparing. Rebuild when required.
                if phase == "preparing":
                    meta = dict(run.process_meta)
                    meta["match"] = run.run_id
                    run = db.update_run(run.run_id, process_meta=meta)
                before_meta = dict(run.process_meta)
                try:
                    updated, decision = recover_run(config, db, locks, run.run_id)
                    self.assertEqual(decision.classification, classification)
                    self.assertEqual(decision.action, "leave")
                    self.assertFalse(decision.launch_provider)
                    self.assertEqual(updated.state, state)
                    self.assertEqual(updated.verification_ran, False)
                    self.assertEqual(updated.process_meta, before_meta)
                    self.assertEqual(auth.read_bytes(), payload)
                    self.assertEqual(note.read_text(encoding="utf-8"), "keep\n")
                    self.assertEqual(db.lock_rows()[0]["run_id"], run.run_id)
                    self.assertEqual(store.read_bytes(), b"synthetic-persistent-store\n")
                finally:
                    db.close()
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def _dead(self, *, state: str, phase: str, expect: str) -> None:
        dead = subprocess.Popen(["true"])
        dead.wait(timeout=5)
        with __import__("tempfile").TemporaryDirectory() as raw:
            config, db, locks, run, auth, _payload, _note, store = self._case(
                Path(raw),
                state=state,
                phase=phase,
                pid=dead.pid,
                token="gone-token",
                identity="gone",
                workspace=True,
            )
            try:
                updated, decision = recover_run(config, db, locks, run.run_id)
                self.assertEqual(updated.state, expect)
                self.assertFalse(decision.launch_provider)
                self.assertFalse(auth.exists())
                self.assertEqual(db.lock_rows(), [])
                self.assertEqual(store.read_bytes(), b"synthetic-persistent-store\n")
            finally:
                db.close()

    def _case(
        self,
        root: Path,
        *,
        state: str,
        phase: str,
        pid: int,
        token: str,
        identity: str,
        workspace: bool,
    ):
        config = load_config(root / "ns")
        config.ensure_dirs()
        store = config.auth_store()
        store.mkdir(parents=True, exist_ok=True)
        store_file = store / "auth.json"
        store_file.write_bytes(b"synthetic-persistent-store\n")
        db = Database(config.db_path)
        locks = LockManager(db)
        run = db.insert_run(
            empty_run(
                job_id="live-auth",
                provider="fake",
                state=state,
                attempt=1,
                max_attempts=1,
                job_snapshot=_LIVE_SNAPSHOT,
                verification_ran=False,
            )
        )
        run_dir = config.runs_dir / run.run_id
        auth = run_dir / "attempt-1" / "grok-home" / "auth.json"
        auth.parent.mkdir(parents=True)
        payload = b"synthetic-attempt-auth\n"
        auth.write_bytes(payload)
        note = auth.parent / "runtime-note"
        note.write_text("keep\n", encoding="utf-8")
        workspace_path = root / "workspace"
        if workspace:
            workspace_path.mkdir()
        meta = {"phase": phase, "pid": pid, "pgid": pid, "match": token, "identity": identity}
        run = db.update_run(
            run.run_id,
            run_dir=str(run_dir),
            pid=pid,
            workspace_path=str(workspace_path),
            process_meta=meta,
        )
        self.assertTrue(locks.acquire([group_key(phase)], run.run_id, pid=pid))
        return config, db, locks, run, auth, payload, note, store_file


def _phase_child(token: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", token],
        start_new_session=True,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _restore(name: str, previous: str | None) -> None:
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


if __name__ == "__main__":
    unittest.main()
