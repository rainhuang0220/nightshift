"""Grok Build CLI process adapter.

Uses the flags accepted by the installed Grok 1.0.46 binary. This module does
not choose what the model should do. `grok agent headless` is a WebSocket
relay and is not used. `--worktree` is not used; Nightshift owns isolation.
`--always-approve` and `--permission-mode bypassPermissions` are never emitted.
`--no-memory` and `--no-auto-update` are accepted by this binary even though
the help summary omits them. `--sandbox` is accepted by the binary and works
when Grok is not already seatbelted. Inside Nightshift's seatbelt, applying
that profile fails with EPERM and Grok refuses to start, so the flag is not
emitted. The seatbelt is the containment.
"""

from __future__ import annotations

import os
import json
import shutil
from pathlib import Path

from nightshift.policy import Policy
from nightshift.providers.base import ProviderResult, run_subprocess, terminate_process


def build_grok_argv(
    *,
    binary: str,
    cwd: Path,
    prompt_file: Path,
    session_id: str,
    model: str | None,
    max_turns: int,
    policy: Policy,
    resume: bool = False,
) -> list[str]:
    if policy.permission_mode in {"bypassPermissions", "always-approve", "always_approve"}:
        raise ValueError("refusing to build a Grok command that bypasses approvals")
    argv = [
        binary,
        "--cwd",
        str(cwd),
        "--prompt-file",
        str(prompt_file),
        "--output-format",
        "streaming-json",
        "--permission-mode",
        policy.permission_mode,
        "--max-turns",
        str(max_turns),
        "--no-subagents",
        "--no-memory",
        "--no-auto-update",
        "--rules",
        "Follow the Nightshift policy in the prompt. Do not push, open PRs, or leave the workspace.",
    ]
    if resume:
        argv.extend(["--resume", session_id])
    else:
        argv.extend(["--session-id", session_id])
    if model:
        argv.extend(["--model", model])
    if policy.disable_web_search:
        argv.append("--disable-web-search")
    for rule in policy.deny:
        argv.extend(["--deny", rule])
    for rule in policy.allow:
        argv.extend(["--allow", rule])
    _reject_unsafe(argv)
    return argv


def _reject_unsafe(argv: list[str]) -> None:
    if "--always-approve" in argv or "--worktree" in argv or "bypassPermissions" in argv:
        raise ValueError("unsafe Grok flag in argv")
    if "headless" in argv:
        raise ValueError("grok agent headless is not the prompt API")


class GrokProvider:
    name = "grok"

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or shutil.which("grok") or "grok"

    def build_argv(self, request, policy: Policy, *, resume: bool = False) -> list[str]:
        return build_grok_argv(
            binary=self.binary,
            cwd=request.workspace,
            prompt_file=request.prompt_path,
            session_id=request.session_id,
            model=request.model,
            max_turns=request.max_turns,
            policy=policy,
            resume=resume,
        )

    def execute(self, request, *, policy: Policy, on_pid=None, poll_stop=None, heartbeat=None) -> ProviderResult:
        if os.environ.get("NIGHTSHIFT_FORBID_GROK") == "1":
            return ProviderResult(
                exit_code=126,
                failure_reason="refusing to launch grok because NIGHTSHIFT_FORBID_GROK=1",
                argv=[],
            )
        if shutil.which(self.binary) is None and not Path(self.binary).is_file():
            return ProviderResult(exit_code=127, failure_reason=f"grok CLI not found: {self.binary}")
        argv = self.build_argv(request, policy, resume=bool(request.resume))
        result = run_subprocess(
            argv,
            cwd=request.workspace,
            env=request.env,
            stdout_path=request.stdout_path,
            stderr_path=request.stderr_path,
            timeout=request.timeout,
            on_pid=on_pid,
            poll_stop=poll_stop,
            heartbeat=heartbeat,
            containment_profile=request.containment_profile,
        )
        if result.exit_code == 0:
            reason = _completion_reason(request.stdout_path)
            if reason not in {'completed', 'stop', 'end_turn'}:
                result.exit_code = 1
                result.failure_reason = f'Grok completion was not confirmed: {reason or "missing end event"}'
        return result

    def terminate(self, pid: int | None, pgid: int | None = None) -> None:
        terminate_process(pid, pgid)


def _completion_reason(path: Path) -> str | None:
    """Inspect bounded retained stream events; shell exit zero is insufficient."""
    from nightshift.providers.base import MAX_LOG_BYTES, MAX_LINE_BYTES
    from nightshift.priv import read_private_bytes
    from nightshift.models import NightshiftError
    try:
        data = read_private_bytes(path, max_bytes=MAX_LOG_BYTES + 1)
    except (OSError, NightshiftError):
        return None
    if len(data) > MAX_LOG_BYTES:
        return None
    reason = None
    for line in data.splitlines():
        if len(line) > MAX_LINE_BYTES:
            continue
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(event, dict) and event.get('type') == 'end':
            reason = event.get('stopReason')
    return reason if isinstance(reason, str) else None
