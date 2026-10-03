"""Parse and validate a versionable job.toml + prompt.md pair."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import tomllib

from nightshift.models import Job

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_PROVIDERS = {"grok", "fake"}
_WRITE_SCOPES = {"none", "workspace"}
_REQUIRED = (
    "schema_version",
    "id",
    "description",
    "type",
    "repository",
    "base_ref",
    "provider",
    "max_runtime_seconds",
    "max_attempts",
    "concurrency_group",
    "network",
    "write_scope",
    "expected_artifacts",
    "verification",
    "success_criteria",
)
_OPTIONAL = {"model", "allow_bash", "isolation", "allow_legacy_shell_verification"}
_ISOLATION = {"clone", "worktree"}
_STEP_KEYS = {"argv", "timeout_seconds"}
_FORBIDDEN_ALLOW = (
    "git push",
    "sudo",
    "rm -rf",
    "gh pr create",
    "gh pr merge",
    "--global",
    "kaggle",
)


class JobValidationError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def job_toml_path(path: Path) -> Path:
    candidate = path.expanduser()
    if candidate.is_dir():
        candidate = candidate / "job.toml"
    return candidate


def load_job(path: Path) -> Job:
    job_file = job_toml_path(path)
    errors: list[str] = []
    if not job_file.is_file():
        raise JobValidationError([f"job manifest not found: {job_file}"])
    try:
        data = tomllib.loads(job_file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise JobValidationError([f"invalid TOML: {exc}"]) from exc
    if not isinstance(data, dict):
        raise JobValidationError(["job manifest must be a TOML table"])
    errors.extend(_validate(data))
    prompt_path = job_file.parent / "prompt.md"
    prompt = ""
    if not prompt_path.is_file():
        errors.append(f"prompt.md is missing next to {job_file.name}")
    else:
        prompt = prompt_path.read_text(encoding="utf-8")
        if not prompt.strip():
            errors.append("prompt.md is empty")
    if errors:
        raise JobValidationError(errors)
    model = data.get("model")
    display, steps = _verification_value(data["verification"])
    return Job(
        schema_version=int(data["schema_version"]),
        id=str(data["id"]),
        description=str(data["description"]).strip(),
        type=str(data["type"]).strip(),
        repository=str(data["repository"]).strip(),
        base_ref=str(data["base_ref"]).strip(),
        provider=str(data["provider"]).strip(),
        model=str(model).strip() if model else None,
        max_runtime_seconds=int(data["max_runtime_seconds"]),
        max_attempts=int(data["max_attempts"]),
        concurrency_group=str(data["concurrency_group"]).strip(),
        network=bool(data["network"]),
        write_scope=str(data["write_scope"]).strip(),
        expected_artifacts=[str(x) for x in data["expected_artifacts"]],
        verification=display,
        success_criteria=[str(x) for x in data["success_criteria"]],
        allow_bash=[str(x) for x in data.get("allow_bash", [])],
        prompt=prompt,
        job_dir=str(job_file.parent.resolve()),
        job_file=str(job_file.resolve()),
        isolation=str(data.get("isolation") or "clone"),
        allow_legacy_shell=bool(data.get("allow_legacy_shell_verification", False)),
        verification_steps=steps,
    )


def resolved_repository(job: Job) -> Path:
    raw = Path(job.repository).expanduser()
    if not raw.is_absolute():
        raw = Path(job.job_dir) / raw
    return raw.resolve()


def _validate(data: dict) -> list[str]:
    errors: list[str] = []
    unknown = sorted(set(data) - set(_REQUIRED) - _OPTIONAL)
    for key in unknown:
        errors.append(f"unknown field {key!r}")
    for key in _REQUIRED:
        if key not in data:
            errors.append(f"missing required field {key!r}")
    if errors:
        return errors
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        errors.append("schema_version must be 1")
    if not isinstance(data["id"], str) or not _ID_RE.match(data["id"]):
        errors.append("id must match [A-Za-z0-9][A-Za-z0-9._-]{0,80}")
    if not isinstance(data["description"], str) or not data["description"].strip():
        errors.append("description must be a non-empty string")
    if not isinstance(data["type"], str) or not data["type"].strip():
        errors.append("type must be a non-empty string")
    if not isinstance(data["repository"], str) or not data["repository"].strip():
        errors.append("repository must be a non-empty path string")
    if not isinstance(data["base_ref"], str) or not data["base_ref"].strip():
        errors.append("base_ref must be a non-empty string")
    provider = data["provider"]
    if provider == "cursor":
        errors.append("provider 'cursor' is not implemented")
    elif not isinstance(provider, str) or provider not in _PROVIDERS:
        errors.append("provider must be 'grok' or 'fake'")
    if "model" in data and data["model"] is not None and not isinstance(data["model"], str):
        errors.append("model must be a string when set")
    errors.extend(_require_int(data, "max_runtime_seconds", minimum=1, maximum=86400))
    errors.extend(_require_int(data, "max_attempts", minimum=1, maximum=10))
    if not isinstance(data["concurrency_group"], str) or not data["concurrency_group"].strip():
        errors.append("concurrency_group must be a non-empty string")
    if not isinstance(data["network"], bool):
        errors.append("network must be a boolean")
    if not isinstance(data["write_scope"], str) or data["write_scope"] not in _WRITE_SCOPES:
        errors.append("write_scope must be 'none' or 'workspace'")
    if "isolation" in data and (not isinstance(data["isolation"], str) or data["isolation"] not in _ISOLATION):
        errors.append("isolation must be 'clone' or 'worktree'")
    if "allow_legacy_shell_verification" in data and not isinstance(data["allow_legacy_shell_verification"], bool):
        errors.append("allow_legacy_shell_verification must be a boolean")
    errors.extend(_string_list(data, "expected_artifacts", allow_empty=True))
    errors.extend(_verification(data))
    errors.extend(_string_list(data, "success_criteria", allow_empty=False))
    if "allow_bash" in data:
        errors.extend(_string_list(data, "allow_bash", allow_empty=True))
        for item in data["allow_bash"] if isinstance(data["allow_bash"], list) else []:
            text = str(item).lower()
            for snippet in _FORBIDDEN_ALLOW:
                if snippet in text:
                    errors.append(f"allow_bash entry refuses forbidden pattern {snippet!r}: {item}")
                    break
    return errors


def _require_int(data: dict, key: str, *, minimum: int, maximum: int) -> list[str]:
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        return [f"{key} must be an integer"]
    if value < minimum or value > maximum:
        return [f"{key} must be between {minimum} and {maximum}"]
    return []


def _verification(data: dict) -> list[str]:
    value = data["verification"]
    if not isinstance(value, list):
        return ["verification must be an array of strings or argv tables"]
    if not value:
        return []
    if all(isinstance(item, str) for item in value):
        return _string_list(data, "verification", allow_empty=True)
    if any(isinstance(item, str) for item in value):
        return ["verification must not mix shell strings and argv tables"]
    errors: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"verification[{index}] must be a string or a table")
            continue
        unknown = sorted(set(item) - _STEP_KEYS)
        for key in unknown:
            errors.append(f"verification[{index}] has unknown field {key!r}")
        argv = item.get("argv")
        if not isinstance(argv, list) or not argv:
            errors.append(f"verification[{index}].argv must be a non-empty array of strings")
        else:
            for arg_index, arg in enumerate(argv):
                if not isinstance(arg, str) or not arg.strip():
                    errors.append(f"verification[{index}].argv[{arg_index}] must be a non-empty string")
                elif "\x00" in arg:
                    errors.append(f"verification[{index}].argv[{arg_index}] must not contain NUL")
        if "timeout_seconds" in item:
            timeout = item["timeout_seconds"]
            if isinstance(timeout, bool) or not isinstance(timeout, int):
                errors.append(f"verification[{index}].timeout_seconds must be an integer")
            elif timeout < 1 or timeout > 86400:
                errors.append(f"verification[{index}].timeout_seconds must be between 1 and 86400")
    return errors


def _verification_value(value: list) -> tuple[list[str], list[dict]]:
    if not value:
        return [], []
    if all(isinstance(item, str) for item in value):
        display = [str(item) for item in value]
        steps = [
            {"argv": ["/bin/sh", "-c", command], "shell": True, "display": command, "timeout_seconds": None}
            for command in display
        ]
        return display, steps
    display = []
    steps = []
    for item in value:
        argv = [str(arg) for arg in item["argv"]]
        timeout = item.get("timeout_seconds")
        steps.append({"argv": argv, "shell": False, "display": shlex.join(argv), "timeout_seconds": timeout})
        display.append(shlex.join(argv))
    return display, steps


def verification_plan(job: Job) -> list[dict]:
    """Structured steps, or legacy shell steps synthesized from display strings."""
    if job.verification_steps:
        return list(job.verification_steps)
    steps = []
    for command in job.verification:
        steps.append(
            {"argv": ["/bin/sh", "-c", command], "shell": True, "display": command, "timeout_seconds": None}
        )
    return steps


def _string_list(data: dict, key: str, *, allow_empty: bool) -> list[str]:
    value = data[key]
    if not isinstance(value, list):
        return [f"{key} must be an array of strings"]
    if not allow_empty and not value:
        return [f"{key} must contain at least one entry"]
    errors = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{key}[{index}] must be a non-empty string")
        elif "\n" in item or "\x00" in item:
            errors.append(f"{key}[{index}] must be a single line")
    return errors
