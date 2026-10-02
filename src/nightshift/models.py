"""Run states, job records, and the only legal transition table."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


class RunState(enum.StrEnum):
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES = frozenset(
    {
        RunState.SUCCEEDED,
        RunState.FAILED,
        RunState.INTERRUPTED,
        RunState.BLOCKED,
        RunState.CANCELLED,
    }
)

ACTIVE_STATES = frozenset(
    {
        RunState.QUEUED,
        RunState.PREPARING,
        RunState.RUNNING,
        RunState.VERIFYING,
    }
)

# Explicit lifecycle. Callers must not invent edges outside this table.
LEGAL_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.QUEUED: frozenset({RunState.PREPARING, RunState.CANCELLED}),
    RunState.PREPARING: frozenset(
        {
            RunState.RUNNING,
            RunState.FAILED,
            RunState.INTERRUPTED,
            RunState.BLOCKED,
            RunState.CANCELLED,
        }
    ),
    RunState.RUNNING: frozenset(
        {
            RunState.VERIFYING,
            RunState.FAILED,
            RunState.INTERRUPTED,
            RunState.CANCELLED,
        }
    ),
    RunState.VERIFYING: frozenset(
        {
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.INTERRUPTED,
            RunState.CANCELLED,
        }
    ),
    RunState.SUCCEEDED: frozenset(),
    RunState.FAILED: frozenset({RunState.QUEUED}),
    RunState.INTERRUPTED: frozenset({RunState.QUEUED}),
    RunState.BLOCKED: frozenset(),
    RunState.CANCELLED: frozenset(),
}

RETRYABLE_STATES = frozenset({RunState.FAILED, RunState.INTERRUPTED})


class IllegalTransition(Exception):
    """Raised when a run would leave the legal transition table."""


class NightshiftError(Exception):
    """Operator-facing error with a process exit code."""

    exit_code = 1

    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class NotFoundError(NightshiftError):
    exit_code = 3


class UsageError(NightshiftError):
    exit_code = 2


class BlockedError(NightshiftError):
    exit_code = 4


def ensure_transition(current: str, new: str) -> None:
    try:
        src = RunState(current)
        dst = RunState(new)
    except ValueError as exc:
        raise IllegalTransition(f"unknown state transition {current!r} -> {new!r}") from exc
    allowed = LEGAL_TRANSITIONS[src]
    if dst not in allowed:
        raise IllegalTransition(f"illegal transition {src.value} -> {dst.value}")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_run_id() -> str:
    return "ns-" + uuid.uuid4().hex[:12]


def new_session_id() -> str:
    return str(uuid.uuid4())


EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_BLOCKED = 4
EXIT_INTERRUPTED = 5


def exit_code_for_state(state: str) -> int:
    mapping = {
        RunState.SUCCEEDED.value: EXIT_OK,
        RunState.FAILED.value: EXIT_FAILED,
        RunState.BLOCKED.value: EXIT_BLOCKED,
        RunState.INTERRUPTED.value: EXIT_INTERRUPTED,
        RunState.CANCELLED.value: EXIT_FAILED,
    }
    return mapping.get(state, EXIT_FAILED)


@dataclass
class Job:
    schema_version: int
    id: str
    description: str
    type: str
    repository: str
    base_ref: str
    provider: str
    model: str | None
    max_runtime_seconds: int
    max_attempts: int
    concurrency_group: str
    network: bool
    write_scope: str
    expected_artifacts: list[str]
    verification: list[str]
    success_criteria: list[str]
    allow_bash: list[str]
    prompt: str
    job_dir: str
    job_file: str
    isolation: str = "clone"
    allow_legacy_shell: bool = False
    verification_steps: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "description": self.description,
            "type": self.type,
            "repository": self.repository,
            "base_ref": self.base_ref,
            "provider": self.provider,
            "model": self.model,
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_attempts": self.max_attempts,
            "concurrency_group": self.concurrency_group,
            "network": self.network,
            "write_scope": self.write_scope,
            "expected_artifacts": list(self.expected_artifacts),
            "verification": list(self.verification),
            "success_criteria": list(self.success_criteria),
            "allow_bash": list(self.allow_bash),
            "prompt": self.prompt,
            "job_dir": self.job_dir,
            "job_file": self.job_file,
            "isolation": self.isolation,
            "allow_legacy_shell_verification": self.allow_legacy_shell,
            "verification_steps": list(self.verification_steps),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        return cls(
            schema_version=int(data["schema_version"]),
            id=str(data["id"]),
            description=str(data["description"]),
            type=str(data["type"]),
            repository=str(data["repository"]),
            base_ref=str(data["base_ref"]),
            provider=str(data["provider"]),
            model=data.get("model"),
            max_runtime_seconds=int(data["max_runtime_seconds"]),
            max_attempts=int(data["max_attempts"]),
            concurrency_group=str(data["concurrency_group"]),
            network=bool(data["network"]),
            write_scope=str(data["write_scope"]),
            expected_artifacts=list(data.get("expected_artifacts") or []),
            verification=list(data.get("verification") or []),
            success_criteria=list(data.get("success_criteria") or []),
            allow_bash=list(data.get("allow_bash") or []),
            prompt=str(data.get("prompt") or ""),
            job_dir=str(data.get("job_dir") or ""),
            job_file=str(data.get("job_file") or ""),
            isolation=str(data.get("isolation") or "clone"),
            allow_legacy_shell=bool(
                data.get("allow_legacy_shell_verification", data.get("allow_legacy_shell", False))
            ),
            verification_steps=list(data.get("verification_steps") or []),
        )


@dataclass
class RunRecord:
    run_id: str
    job_id: str
    job_path: str
    job_snapshot: str
    description: str
    job_type: str
    source_repo: str
    source_revision: str
    source_head: str
    base_ref: str
    workspace_path: str
    provider: str
    model: str
    session_id: str
    pid: int | None
    process_meta: dict
    state: str
    attempt: int
    max_attempts: int
    concurrency_group: str
    network: bool
    write_scope: str
    max_runtime_seconds: int
    created_at: str
    updated_at: str
    started_at: str
    ended_at: str
    heartbeat_at: str
    exit_code: int | None
    provider_exit_code: int | None
    verification_exit_code: int | None
    verification_ran: bool
    failure_reason: str
    artifact_paths: list
    report_path: str
    run_dir: str
    host_info: dict
    duration_seconds: float | None
    recovery_class: str
    source_porcelain: str
    source_porcelain_after: str = ""
    provider_argv: list = field(default_factory=list)
    source_integrity: str = ""
    invocation: dict = field(default_factory=dict)


def empty_run(**kwargs) -> RunRecord:
    now = utc_now()
    base = dict(
        run_id=new_run_id(),
        job_id="",
        job_path="",
        job_snapshot="",
        description="",
        job_type="",
        source_repo="",
        source_revision="",
        source_head="",
        base_ref="",
        workspace_path="",
        provider="",
        model="",
        session_id="",
        pid=None,
        process_meta={},
        state=RunState.QUEUED.value,
        attempt=1,
        max_attempts=1,
        concurrency_group="",
        network=False,
        write_scope="workspace",
        max_runtime_seconds=600,
        created_at=now,
        updated_at=now,
        started_at="",
        ended_at="",
        heartbeat_at="",
        exit_code=None,
        provider_exit_code=None,
        verification_exit_code=None,
        verification_ran=False,
        failure_reason="",
        artifact_paths=[],
        report_path="",
        run_dir="",
        host_info={},
        duration_seconds=None,
        recovery_class="",
        source_porcelain="",
        source_porcelain_after="",
        provider_argv=[],
        source_integrity="",
        invocation={},
    )
    base.update(kwargs)
    return RunRecord(**base)
