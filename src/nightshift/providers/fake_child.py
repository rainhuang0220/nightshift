"""In-process child used by FakeProvider. This is not an AI call."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    run_id = args[0] if args else "unknown"
    mode = os.environ.get("NIGHTSHIFT_FAKE_RESULT", "success")
    write_scope = os.environ.get("NIGHTSHIFT_WRITE_SCOPE", "workspace")
    print(f"fake provider start run={run_id} mode={mode} write_scope={write_scope}", flush=True)
    if mode == "hold":
        print("finding: fake provider is holding for cancel tests", flush=True)
        time.sleep(int(os.environ.get("NIGHTSHIFT_FAKE_HOLD_SECONDS", "30")))
        print("NIGHTSHIFT_METRIC fake_result=hold", flush=True)
        return 0
    if mode == "fail":
        print("finding: the requested check could not be completed", flush=True)
        print("finding: hypothesis: the expected marker was absent, so no source edit was required", flush=True)
        print("NIGHTSHIFT_METRIC fake_result=fail", flush=True)
        return 1
    workspace = Path.cwd()
    if write_scope == "workspace":
        note = workspace / "nightshift-notes" / "result.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(
            "Nightshift fake provider wrote this harmless note inside the isolated worktree.\n",
            encoding="utf-8",
        )
        _git(["add", "nightshift-notes/result.md"])
        _git(
            [
                "-c",
                "user.email=nightshift@localhost",
                "-c",
                "user.name=Nightshift",
                "commit",
                "-m",
                "nightshift: record fake provider note",
            ]
        )
        print("finding: wrote nightshift-notes/result.md and committed it locally", flush=True)
    else:
        print("finding: read-only fake provider left the workspace unchanged", flush=True)
    print("NIGHTSHIFT_METRIC fake_result=success", flush=True)
    return 0


def _git(args: list[str]) -> None:
    proc = subprocess.run(["git", *args], check=False, text=True, capture_output=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout or "git failed\n")
        raise SystemExit(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
