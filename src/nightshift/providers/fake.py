"""Deterministic provider for tests and dry runs. Never calls a model."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from nightshift.policy import package_pythonpath
from nightshift.providers.base import ProviderResult, run_subprocess


class FakeProvider:
    name = "fake"

    def build_argv(self, *, run_id: str, cwd: Path, prompt_file: Path) -> list[str]:
        del cwd, prompt_file
        return [sys.executable, "-m", "nightshift.providers.fake_child", run_id]

    def execute(self, request, *, on_pid=None, poll_stop=None, heartbeat=None) -> ProviderResult:
        argv = self.build_argv(run_id=request.run_id, cwd=request.workspace, prompt_file=request.prompt_path)
        if request.session_id:
            argv.append(request.session_id)
        env = dict(request.env)
        root = package_pythonpath()
        current = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = root if not current else root + os.pathsep + current
        mode = os.environ.get("NIGHTSHIFT_FAKE_RESULT", "success")
        env["NIGHTSHIFT_FAKE_RESULT"] = mode
        env["NIGHTSHIFT_WRITE_SCOPE"] = request.write_scope
        if os.environ.get("NIGHTSHIFT_FAKE_HOLD_SECONDS"):
            env["NIGHTSHIFT_FAKE_HOLD_SECONDS"] = os.environ["NIGHTSHIFT_FAKE_HOLD_SECONDS"]
        return run_subprocess(
            argv,
            cwd=request.workspace,
            env=env,
            stdout_path=request.stdout_path,
            stderr_path=request.stderr_path,
            timeout=request.timeout,
            on_pid=on_pid,
            poll_stop=poll_stop,
            heartbeat=heartbeat,
        )

    def terminate(self, pid: int | None, pgid: int | None = None) -> None:
        from nightshift.providers.base import terminate_process

        terminate_process(pid, pgid)
