import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from nightshift.providers.base import run_subprocess


class CallbackCleanupTests(unittest.TestCase):
    def test_callback_failure_stops_the_owned_process(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pids = []
            def remember(pid, pgid):
                pids.append(pid)
            def stop():
                raise RuntimeError('controller callback broke')
            try:
                with self.assertRaisesRegex(RuntimeError, 'callback'):
                    run_subprocess([sys.executable, '-c', 'import time; time.sleep(60)'],
                        cwd=root, env=dict(os.environ), stdout_path=root / 'out', stderr_path=root / 'err',
                        timeout=10, on_pid=remember, poll_stop=stop)
                state = subprocess.run(['ps', '-o', 'state=', '-p', str(pids[0])], capture_output=True, text=True).stdout.strip()
                self.assertTrue(not state or state.startswith('Z'), state)
            finally:
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
