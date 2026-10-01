"""Helpers for the in-repo suite.

Lives inside the package so tests do not import a top-level ``tests`` module.
Nothing in the CLI imports this module.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        text=True,
        capture_output=True,
    )


def make_repo(path: Path, *, dirty: bool = False) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(["git", "init", "-b", "main"], cwd=path, stdout=subprocess.DEVNULL)
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test User")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "init")
    head = git(path, "rev-parse", "HEAD").stdout.strip()
    if dirty:
        (path / "DIRTY.txt").write_text("dirty\n", encoding="utf-8")
    return head


def snapshot(repo: Path) -> tuple[str, str]:
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    status = git(repo, "status", "--porcelain").stdout
    return head, status


def write_job(directory: Path, repository: Path, **overrides: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    prompt = overrides.pop("prompt", None)
    fields: dict[str, object] = {
        "schema_version": 1,
        "id": "sample-job",
        "description": "Temporary job used by tests",
        "type": "note",
        "repository": str(repository),
        "base_ref": "HEAD",
        "provider": "fake",
        "max_runtime_seconds": 60,
        "max_attempts": 1,
        "concurrency_group": "sample",
        "network": False,
        "write_scope": "workspace",
        "expected_artifacts": ["nightshift-notes/result.md"],
        "success_criteria": ["The isolated workspace contains a local note."],
        "verification": ["test -f nightshift-notes/result.md"],
    }
    fields.update(overrides)
    text = textwrap.dedent(
        f"""\
        schema_version = {int(fields["schema_version"])}
        id = {_toml_str(fields["id"])}
        description = {_toml_str(fields["description"])}
        type = {_toml_str(fields["type"])}
        repository = {_toml_str(fields["repository"])}
        base_ref = {_toml_str(fields["base_ref"])}
        provider = {_toml_str(fields["provider"])}
        max_runtime_seconds = {int(fields["max_runtime_seconds"])}
        max_attempts = {int(fields["max_attempts"])}
        concurrency_group = {_toml_str(fields["concurrency_group"])}
        network = {_toml_bool(fields["network"])}
        write_scope = {_toml_str(fields["write_scope"])}
        expected_artifacts = {_toml_list(fields["expected_artifacts"])}
        success_criteria = {_toml_list(fields["success_criteria"])}
        verification = {_toml_list(fields["verification"])}
        """
    )
    allow = fields.get("allow_bash")
    if allow:
        text += f"allow_bash = {_toml_list(allow)}\n"
    (directory / "job.toml").write_text(text, encoding="utf-8")
    if not isinstance(prompt, str):
        prompt = "Write a harmless note in the isolated workspace.\n"
    (directory / "prompt.md").write_text(prompt, encoding="utf-8")
    return directory


def _toml_str(value: object) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _toml_bool(value: object) -> str:
    if isinstance(value, str):
        return "true" if value.lower() == "true" else "false"
    return "true" if value else "false"


def _toml_list(value: object) -> str:
    items = value if isinstance(value, list) else []
    return "[" + ", ".join(_toml_str(item) for item in items) + "]"


def cli_env(**extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    env["NIGHTSHIFT_FORBID_GROK"] = "1"
    env.pop("NIGHTSHIFT_FAKE_RESULT", None)
    env.update(extra)
    return env


def run_cli(root: Path, args: list[str], **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "nightshift", "--root", str(root), *args],
        check=False,
        text=True,
        capture_output=True,
        env=cli_env(**extra),
        cwd=str(ROOT),
    )
