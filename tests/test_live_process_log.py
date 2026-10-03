import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from nightshift.providers.base import run_subprocess


class LiveProcessLogTests(unittest.TestCase):
    def test_small_complete_lines_are_visible_before_child_exit(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stop = threading.Event()
            outcomes = []
            def execute():
                outcomes.append(run_subprocess(
                    [sys.executable, '-c', 'import time; print("ready", flush=True); time.sleep(30)'],
                    cwd=root, env=os.environ.copy(), stdout_path=root / 'out', stderr_path=root / 'err',
                    timeout=10, poll_stop=lambda: 'interrupt' if stop.is_set() else None))
            worker = threading.Thread(target=execute)
            worker.start()
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if (root / 'out').exists() and 'ready\n' in (root / 'out').read_text():
                        break
                    time.sleep(.02)
                self.assertTrue(worker.is_alive())
                self.assertIn('ready\n', (root / 'out').read_text())
            finally:
                stop.set()
                worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes[0].failure_reason, 'interrupt')
