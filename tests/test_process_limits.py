import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from nightshift.providers.base import run_subprocess


class ProcessLimitsTests(unittest.TestCase):
    def launch(self, root, script, **kwargs):
        return run_subprocess([sys.executable, '-c', script], cwd=root, env=dict(os.environ),
                              stdout_path=root / 'out', stderr_path=root / 'err', timeout=5, **kwargs)

    def test_large_output_is_drained_but_bounded(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = self.launch(root, "import sys; sys.stdout.write('x'*2000000)", max_output_bytes=4096)
            self.assertEqual(result.exit_code, 0)
            self.assertLessEqual((root / 'out').stat().st_size, 4096)
            self.assertIn('truncated', (root / 'out').read_text())

    def test_split_credentials_are_redacted_before_writing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            script = "import sys; sys.stdout.write('x'*8187+'token=very-secret-value\\nAuthorization: Bearer secret-bearer\\n')"
            self.launch(root, script)
            output = (root / 'out').read_text()
            self.assertNotIn('very-secret-value', output)
            self.assertNotIn('secret-bearer', output)

    def test_exited_leader_does_not_leave_a_child_in_its_group(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            script = "import os,time; pid=os.fork(); open('pid','w').write(str(pid)) if pid else time.sleep(60)"
            started = time.monotonic()
            result = self.launch(root, script)
            pid = int((root / 'pid').read_text())
            try:
                state = __import__('subprocess').run(['ps', '-o', 'state=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
                self.assertTrue(not state or state.startswith('Z'), state)
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual(result.exit_code, 0)
            finally:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

class VerificationLimitsTests(unittest.TestCase):
    def test_contained_output_is_bounded_and_times_out(self):
        from nightshift.containment import build_read_policy, contained_run, write_profile
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            profile = write_profile(root / 'verify.sb', writable=[root], network=False,
                                    read_policy=build_read_policy(runtime_roots=[root]))
            code, output = contained_run([sys.executable, '-c', "print('x'*3000000)"], cwd=root,
                                        env={'PATH': os.environ['PATH']}, profile=profile, timeout=5,
                                        max_output_bytes=4096)
            self.assertEqual(code, 0)
            self.assertLessEqual(len(output.encode()), 8192)
            self.assertIn('truncated', output)
            code, output = contained_run([sys.executable, '-c', 'import time; time.sleep(30)'], cwd=root,
                                        env={'PATH': os.environ['PATH']}, profile=profile, timeout=.2)
            self.assertEqual(code, 124)
            self.assertIn('timed out', output)

    def test_controller_report_reads_are_bounded(self):
        from nightshift.runner import _read
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'legacy.log'
            path.write_bytes(b'x' * (10 * 1024 * 1024))
            output = _read(path)
            self.assertLess(len(output), 9 * 1024 * 1024)
            self.assertIn('truncated', output)
