"""Real macOS release gate: intentionally RED until a whole-task boundary exists.

Run explicitly with PYTHONPATH=src NIGHTSHIFT_FORBID_GROK=1 python3
 tests/macos/task_lifetime_gate.py --evidence /absolute/output.json
No expectedFailure/skip, model, network, PID scan or arbitrary-process signal.
All descendant payloads expire themselves; only unreaped direct children are
signalled by this harness. Production supervision and Seatbelt are exercised.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from nightshift.config import load_config
from nightshift.containment import build_read_policy, write_profile
from nightshift.db import Database
from nightshift.locks import LockManager, execution_lease
from nightshift.providers.base import run_subprocess
from nightshift.queue import enqueue
from nightshift.runner import execute_run
from nightshift.supervisor import recover_run
from nightshift.testkit import make_repo, snapshot, write_job

LIFETIME = 4.0
OBSERVATIONS: dict[str, dict] = {}


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('bounded handshake did not complete')


def size(path):
    return path.stat().st_size if path.exists() else 0


def writer_code():
    # Keep an already-open writable descriptor; chmod-only ideas must not pass.
    return (
        "import os,time,json\n"
        "from pathlib import Path\n"
        "fd=os.open('writes',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)\n"
        f"deadline=time.monotonic()+{LIFETIME!r}\n"
        "Path('identity').write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'sid':os.getsid(0),'expires_at':deadline}))\n"
        "while time.monotonic()<deadline:\n"
        " os.write(fd,b'x'); time.sleep(.01)\n"
        "os.close(fd); Path('writer-finished').touch()\n"
    )


def leader_code(*, detached=False, regroup=False, grandchild=False, finish='exit'):
    child = writer_code()
    if regroup:
        child = 'import os\nos.setpgid(0,0)\n' + child
    if grandchild:
        child = ("import subprocess,sys,time\n"
                 f"subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session={detached!r})\n"
                 f"time.sleep({LIFETIME + .1!r})\n")
        direct_detach = False
    else:
        direct_detach = detached
    return (
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        f"subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session={direct_detach!r})\n"
        "deadline=time.monotonic()+3\n"
        "while (not Path('identity').exists() or Path('writes').stat().st_size==0) and time.monotonic()<deadline: time.sleep(.01)\n"
        "assert Path('identity').exists() and Path('writes').stat().st_size>0\n"
        + (f"time.sleep({LIFETIME + .1!r})\n" if finish != 'exit' else '')
    )


class PayloadProvider:
    """A bounded test provider; only adapter selection is substituted."""
    def execute(self, request, **kwargs):
        return run_subprocess(
            [sys.executable, '-c', leader_code(detached=True), request.session_id],
            cwd=request.workspace, env=request.env, stdout_path=request.stdout_path,
            stderr_path=request.stderr_path, timeout=request.timeout,
            containment_profile=request.containment_profile, **kwargs,
        )


class TaskLifetimeGate(unittest.TestCase):
    def setUp(self):
        if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
            self.fail('real macOS Seatbelt is required; no mock or skip substitutes for this gate')
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.profile = write_profile(
            self.root / 'profile.sb', writable=[self.workspace], network=False,
            read_policy=build_read_policy(runtime_roots=[self.workspace]),
        )
        self.witness = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.started = time.monotonic()
        self.observation = {'actual_seatbelt': True}

    def tearDown(self):
        # Do not signal escaped descendants. Their payload self-expires. Waiting
        # here is test hygiene AFTER measuring the invariant, never a proposed fix.
        identity = self.workspace / 'identity'
        if identity.exists():
            remaining = json.loads(identity.read_text())['expires_at'] + .2 - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        self.observation['unrelated_direct_child_alive'] = self.witness.poll() is None
        OBSERVATIONS[self.id().split('.')[-1]] = self.observation
        if self.witness.poll() is None:
            self.witness.terminate()  # Unreaped direct child: PID cannot be reused.
        self.witness.wait(timeout=5)
        self.temp.cleanup()

    def invoke(self, *, detached=False, regroup=False, grandchild=False, finish='exit'):
        def stop():
            return 'cancel' if finish == 'cancel' and size(self.workspace / 'writes') else None
        result = run_subprocess(
            [sys.executable, '-c', leader_code(detached=detached, regroup=regroup, grandchild=grandchild, finish=finish)],
            cwd=self.workspace, env={'PATH': os.environ.get('PATH', '/usr/bin:/bin')},
            stdout_path=self.root / 'stdout', stderr_path=self.root / 'stderr',
            timeout=.4 if finish == 'timeout' else 5,
            containment_profile=self.profile, poll_stop=stop,
        )
        self.observation.update(exit_code=result.exit_code, failure_reason=result.failure_reason,
                                leader_pid=result.pid, leader_pgid=result.pgid,
                                writer=json.loads((self.workspace / 'identity').read_text()))
        self.assertIsNone(self.witness.poll(), 'unrelated process was killed')
        return result

    def assert_no_writes(self):
        expiry = json.loads((self.workspace / 'identity').read_text())['expires_at']
        self.assertLess(time.monotonic() + .25, expiry,
                        'inconclusive: finite payload expired before cleanup observation')
        self.assertFalse((self.workspace / 'writer-finished').exists(),
                         'inconclusive: payload expired naturally, not proved cleanup')
        before = size(self.workspace / 'writes')
        # A write after the boundary is decisive; absence is checked for a
        # bounded window, not presented as a general kernel lifetime proof.
        deadline = time.monotonic() + .25
        while time.monotonic() < deadline and size(self.workspace / 'writes') == before:
            time.sleep(.01)
        after = size(self.workspace / 'writes')
        self.observation['writes_after_boundary'] = [before, after]
        self.assertLess(time.monotonic(), expiry,
                        'inconclusive: payload expired during cleanup observation')
        self.assertFalse((self.workspace / 'writer-finished').exists(),
                         'inconclusive: payload expired during cleanup observation')
        self.assertEqual(after, before, 'terminal boundary retains workspace write capability')

    def test_normal_child_cleanup(self):
        self.assertEqual(self.invoke().exit_code, 0)
        self.assert_no_writes()

    def test_child_creates_another_process(self):
        self.assertEqual(self.invoke(grandchild=True).exit_code, 0)
        self.assert_no_writes()

    def test_setsid_descendant_cannot_escape(self):
        self.assertEqual(self.invoke(detached=True, grandchild=True).exit_code, 0)
        self.assert_no_writes()

    def test_setpgid_descendant_cannot_escape(self):
        self.assertEqual(self.invoke(regroup=True).exit_code, 0)
        self.assert_no_writes()

    def test_leader_exits_normally(self):
        self.assertEqual(self.invoke(detached=True).exit_code, 0)
        self.assert_no_writes()

    def test_provider_timeout(self):
        self.assertEqual(self.invoke(detached=True, finish='timeout').failure_reason, 'timed out')
        self.assert_no_writes()

    def test_provider_cancellation(self):
        self.assertEqual(self.invoke(detached=True, finish='cancel').failure_reason, 'cancel')
        self.assert_no_writes()

    def test_no_unrelated_process_is_killed(self):
        self.invoke()
        self.assertIsNone(self.witness.poll())

    def test_terminal_attempt_cannot_write(self):
        source = self.root / 'source'
        make_repo(source)
        before = snapshot(source)
        job = write_job(self.root / 'job', source, expected_artifacts=[], verification=['true'])
        config = load_config(self.root / 'control')
        config.ensure_dirs()
        db = Database(config.db_path)
        try:
            run = enqueue(db, job, provider='fake')
            with patch('nightshift.runner.get_provider', return_value=PayloadProvider()):
                result = execute_run(config, db, LockManager(db), run.run_id)
            self.assertEqual(result.state, 'SUCCEEDED', result.failure_reason)
            self.assertEqual(snapshot(source), before)
            self.workspace = Path(result.workspace_path)
            self.observation.update(state=result.state, attempt=result.attempt,
                                    source_unchanged=True, run_id=result.run_id)
            self.assert_no_writes()
        finally:
            db.close()

    def crash_controller(self):
        source = self.root / 'source'
        make_repo(source)
        before = snapshot(source)
        job = write_job(self.root / 'job', source, expected_artifacts=[], verification=['true'])
        config = load_config(self.root / 'control')
        config.ensure_dirs()
        db = Database(config.db_path)
        run = enqueue(db, job, provider='fake')
        self.workspace = config.worktrees_dir / run.run_id
        # The adapter has a subsecond deadline. Its payload lives for four
        # seconds; without a controller, even this finite test outlives it.
        script = (
            "from pathlib import Path\nfrom nightshift.config import load_config\n"
            "from nightshift.db import Database\nfrom nightshift.locks import execution_lease\n"
            "from nightshift.containment import build_read_policy,write_profile\n"
            "from nightshift.providers.base import run_subprocess\nfrom nightshift.runner import _mark_phase\n"
            f"config=load_config(Path({str(config.root)!r}))\n"
            "db=Database(config.db_path)\n"
            f"run_id={run.run_id!r}\nworkspace=Path({str(self.workspace)!r}); workspace.mkdir()\n"
            "profile=write_profile(config.root/'profile.sb',writable=[workspace],network=False,read_policy=build_read_policy(runtime_roots=[workspace]))\n"
            "with execution_lease(config.state_dir,run_id) as owned:\n"
            " assert owned\n"
            " db.transition(run_id,'PREPARING','probe')\n"
            " db.transition(run_id,'RUNNING','probe')\n"
            " db.update_run(run_id,workspace_path=str(workspace))\n"
            f" run_subprocess([{sys.executable!r},'-c',{writer_code()!r},'lifetime-provider'],cwd=workspace,env={{'PATH':'/usr/bin:/bin'}},stdout_path=config.root/'stdout',stderr_path=config.root/'stderr',timeout=.4,containment_profile=profile,on_pid=lambda pid,pgid:_mark_phase(db,run_id,'provider',pid,pgid=pgid,match='lifetime-provider'))\n"
        )
        controller = subprocess.Popen([sys.executable, '-c', script], env=dict(os.environ),
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for(lambda: size(self.workspace / 'writes') > 0)
            controller.kill()  # Unreaped direct child, no identifier search.
            controller.wait(timeout=5)
            with execution_lease(config.state_dir, run.run_id) as owned:
                self.assertTrue(owned, 'controller lease remained owned')
            self.observation.update(controller_returncode=controller.returncode,
                                    automatic_state=db.require_run(run.run_id).state,
                                    source_unchanged=snapshot(source)==before)
            return config, db, run
        except BaseException:
            db.close()
            raise
        finally:
            if controller.poll() is None:
                controller.kill()
            controller.wait(timeout=5)

    def test_controller_sigkill_preserves_deadline(self):
        config, db, run = self.crash_controller()
        try:
            # No recovery is invoked before the deadline observation.
            time.sleep(.65)
            self.assert_no_writes()
        finally:
            db.close()

    def test_repeated_recovery_is_idempotent(self):
        config, db, run = self.crash_controller()
        try:
            # Identified phase/controller metadata authorizes existing recovery;
            # it still never launches a model or repeats verification.
            result, first = recover_run(config, db, LockManager(db), run.run_id)
            repeated, second = recover_run(config, db, LockManager(db), run.run_id)
            self.observation.update(recovered_state=result.state, first=first.classification,
                                    second=second.classification, attempt=repeated.attempt,
                                    verification_ran=repeated.verification_ran)
            self.assertEqual(result.state, 'INTERRUPTED')
            self.assertEqual(repeated.state, result.state)
            self.assertEqual(repeated.attempt, 1)
            self.assertFalse(repeated.verification_ran)
            self.assertFalse(first.launch_provider or second.launch_provider)
        finally:
            db.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--case', action='append', default=[])
    args=parser.parse_args()
    os.environ['NIGHTSHIFT_FORBID_GROK']='1'
    all_cases=unittest.defaultTestLoader.getTestCaseNames(TaskLifetimeGate)
    selected=args.case or all_cases
    suite=(unittest.TestSuite(TaskLifetimeGate(case) for case in args.case) if args.case
           else unittest.defaultTestLoader.loadTestsFromTestCase(TaskLifetimeGate))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    payload={'platform':platform.platform(),'baseline':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
             'tests_run':result.testsRun,'failures':[test.id() for test,_ in result.failures],
             'errors':[{'test':test.id(),'detail':detail} for test,detail in result.errors],
             'observations':OBSERVATIONS,'selected_cases':selected,
             'full_gate_coverage':sorted(selected)==all_cases,
             'selected_cases_passed':result.wasSuccessful(),
             'release_gate_passed':sorted(selected)==all_cases and result.wasSuccessful()}
    args.evidence.write_text(json.dumps(payload,indent=2)+'\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
