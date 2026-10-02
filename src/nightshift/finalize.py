"""The only success gate.

Normal execution, verification recovery, and recorded-state finalization all
call `decide_final`. A missing integrity baseline is not a success when the
caller marks integrity as failed. Recovery never launches a provider; this
module only classifies a result that is already on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nightshift.integrity import compare, loads
from nightshift.models import RunState

SOURCE_INTEGRITY_VIOLATION = "SOURCE_INTEGRITY_VIOLATION"
BLOCKED_EXTENSION_SURFACE = "BLOCKED_EXTENSION_SURFACE"


@dataclass(frozen=True)
class FinalDecision:
    state: str
    reason: str


@dataclass(frozen=True)
class IntegrityCheck:
    ok: bool
    detail: str


def decide_final(
    *,
    provider_exit_code: int | None,
    verification_ran: bool,
    verification_exit_code: int | None,
    workspace_exists: bool,
    integrity_ok: bool,
    safety_audit_ok: bool,
    integrity_detail: str = "",
) -> FinalDecision:
    """Return SUCCEEDED only when every required invariant holds.

    `provider_exit_code is None` is not acceptable. Callers that have no
    source repository pass `integrity_ok=True` themselves. This function does
    not treat a missing repository as a special case.
    """
    if not workspace_exists:
        return FinalDecision(RunState.FAILED.value, "workspace missing")
    if integrity_ok is not True:
        if integrity_detail.startswith(SOURCE_INTEGRITY_VIOLATION):
            return FinalDecision(RunState.FAILED.value, integrity_detail)
        return FinalDecision(RunState.FAILED.value, SOURCE_INTEGRITY_VIOLATION)
    if safety_audit_ok is not True:
        return FinalDecision(RunState.FAILED.value, BLOCKED_EXTENSION_SURFACE)
    if provider_exit_code != 0:
        return FinalDecision(RunState.FAILED.value, f"provider exited {provider_exit_code}")
    if not verification_ran or verification_exit_code != 0:
        shown = verification_exit_code if verification_ran else "not run"
        return FinalDecision(RunState.FAILED.value, f"verification exit {shown}")
    return FinalDecision(RunState.SUCCEEDED.value, "")


def check_stored_integrity(source_repo: str, stored: str) -> IntegrityCheck:
    """Compare a stored snapshot with the repository on disk.

    An empty `source_repo` means the caller has nothing to protect and the
    check is ok. A repository without a stored snapshot fails closed.
    """
    if not source_repo:
        return IntegrityCheck(True, "")
    if not stored:
        return IntegrityCheck(False, SOURCE_INTEGRITY_VIOLATION)
    try:
        before = loads(stored)
        from nightshift.integrity import capture

        after = capture(Path(source_repo))
        result = compare(before, after)
    except Exception:
        return IntegrityCheck(False, SOURCE_INTEGRITY_VIOLATION)
    if result.ok:
        return IntegrityCheck(True, "")
    detail = SOURCE_INTEGRITY_VIOLATION + ": " + ",".join(result.changed_categories)
    return IntegrityCheck(False, detail)


def extension_audit_ok(provider: str, invocation: dict | None) -> bool:
    """Fake runs have no Grok extension surface. Grok runs need a recorded pass."""
    if provider != "grok":
        return True
    audit = (invocation or {}).get("extension_audit")
    return isinstance(audit, dict) and audit.get("ok") is True
