"""Morning report. Pure rendering so tests do not need a live provider."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field


@dataclass
class ReportInputs:
    run_id: str
    job_id: str
    description: str
    job_type: str
    state: str
    provider: str
    attempt: int
    max_attempts: int
    source_repo: str
    source_revision: str
    source_head: str
    base_ref: str
    workspace: str
    session_id: str
    started_at: str
    ended_at: str
    duration_seconds: float | None
    exit_code: int | None
    provider_exit_code: int | None
    verification_exit_code: int | None
    failure_reason: str
    findings: str
    commits: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    verification_commands: list[str] = field(default_factory=list)
    verification_output: str = ""
    success_criteria: list[str] = field(default_factory=list)
    measurements: dict[str, str] = field(default_factory=dict)
    host_info: dict[str, str] = field(default_factory=dict)
    uncertainty: list[str] = field(default_factory=list)
    artifacts_expected: list[str] = field(default_factory=list)
    artifacts_found: list[str] = field(default_factory=list)
    human_review_required: bool = True
    recovery_class: str = ""
    isolation: str = "clone"
    grok_home_scope: str = "per-run"
    source_read_isolation: str = "not-probed"
    operator_home_read_isolation: str = "not-probed"
    source_integrity: str = "not-probed"
    extension_audit: str = "not-probed"
    network_containment: str = "accepted limitation"
    inspect_containment: str = "not-probed"
    import_note: str = (
        "Nightshift does not merge or push. Review the isolated workspace and import commits by hand."
    )
    conclusion: str = ""


def provider_conclusion(text: str) -> str:
    """Join user-visible text events. Thought and usage records are omitted.

    Fragments are concatenated in order, so a reply split across several
    streaming events stays one conclusion. A literal `finding:` prefix is not
    required.
    """
    parts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "text":
            continue
        data = event.get("data")
        if isinstance(data, str):
            parts.append(data)
    return "".join(parts).strip()


def extract_findings(text: str) -> str:
    lines = [line for line in text.splitlines() if line.startswith("finding:")]
    if lines:
        return "\n".join(lines)
    return ""


def extract_metrics(text: str) -> dict[str, str]:
    metrics: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("NIGHTSHIFT_METRIC "):
            continue
        body = line.split(" ", 1)[1]
        if "=" not in body:
            continue
        key, value = body.split("=", 1)
        key = key.strip()
        if key:
            metrics[key] = value.strip()
    return metrics


def render_report(info: ReportInputs) -> str:
    succeeded = info.state == "SUCCEEDED"
    failed = info.state in {"FAILED", "INTERRUPTED", "BLOCKED", "CANCELLED"}
    success_lines = ["The run reached SUCCEEDED."] if succeeded else ["This run did not succeed."]
    if info.commits:
        success_lines.append(f"Local commits recorded: {len(info.commits)}.")
    if info.files_changed and succeeded:
        success_lines.append(f"Files changed: {len(info.files_changed)}.")
    if failed:
        fail_body = info.failure_reason.strip() or f"The run ended in {info.state}."
    else:
        fail_body = "Nothing failed."
    findings = info.findings.strip() or "(no provider findings were captured)"
    conclusion = info.conclusion.strip() or "(no provider conclusion was captured)"
    commands = info.verification_commands or ["(none)"]
    verification_result = info.verification_output.strip() or "(no verification output)"
    if info.verification_exit_code is not None:
        verification_result = f"exit {info.verification_exit_code}\n{verification_result}"
    verification_result = verification_result.replace("```", "'''")
    commit_lines = info.commits or ["(no local commits)"]
    file_lines = info.files_changed or ["(no file changes detected)"]
    criteria = info.success_criteria or ["(none recorded)"]
    uncertainty = list(info.uncertainty)
    if info.human_review_required and "A human should review the workspace before keeping or discarding it." not in uncertainty:
        uncertainty.append("A human should review the workspace before keeping or discarding it.")
    if not uncertainty:
        uncertainty.append("No extra uncertainty was recorded.")
    host = ", ".join(f"{key}={value}" for key, value in sorted(info.host_info.items())) or "(not recorded)"
    duration = "unknown" if info.duration_seconds is None else f"{info.duration_seconds:.3f}s"
    measurements = [
        f"- duration: {duration}",
        f"- exit status: {info.exit_code if info.exit_code is not None else 'unknown'}",
        f"- provider exit: {info.provider_exit_code if info.provider_exit_code is not None else 'unknown'}",
        f"- host: {host}",
    ]
    for key, value in sorted(info.measurements.items()):
        measurements.append(f"- {key}: {value}")
    expected = info.artifacts_expected or ["(none declared)"]
    found = info.artifacts_found or ["(none found)"]
    review = "yes" if info.human_review_required else "no"
    inspect = _inspect_commands(info)
    recovery = info.recovery_class or "(not a recovered run)"
    return "\n".join(
        [
            "# Nightshift report",
            "",
            "## What ran",
            f"- Run: {info.run_id}",
            f"- Job: {info.job_id} ({info.job_type})",
            f"- Description: {info.description or '(none)'}",
            f"- Provider: {info.provider or '(none)'}",
            f"- Session: {info.session_id or '(none)'}",
            f"- Attempt: {info.attempt} of {info.max_attempts}",
            f"- This report describes attempt {info.attempt}. Earlier attempt logs stay in the run directory.",
            f"- Recovery class: {recovery}",
            "",
            "## What succeeded",
            *[f"- {line}" for line in success_lines],
            "",
            "## What failed",
            fail_body,
            "",
            "### Findings",
            findings,
            "",
            "## Provider conclusion",
            conclusion,
            "",
            "## Repository",
            f"- Repository: {info.source_repo or '(unknown)'}",
            f"- Base ref: {info.base_ref or '(unknown)'}",
            f"- Original revision: {info.source_revision or '(unknown)'}",
            f"- Source HEAD at prepare: {info.source_head or '(unknown)'}",
            f"- Workspace: {info.workspace or '(not created)'}",
            "",
            "## What changed",
            "### Resulting local commits",
            *[f"- {line}" for line in commit_lines],
            "### Files changed",
            *[f"- {line}" for line in file_lines],
            "",
            "## Tests",
            "### Tests executed",
            *[f"- {line}" for line in commands],
            "### Test results",
            "```",
            verification_result,
            "```",
            "A verification exit of 0 means the declared check passed inside the isolated mutable workspace. It does not mean a model that can edit that workspace was unable to influence the check.",
            "",
            "## Measurements",
            *measurements,
            "",
            "## Uncertainty",
            *[f"- {line}" for line in uncertainty],
            "- Success criteria (not executed automatically):",
            *[f"  - {line}" for line in criteria],
            "- Expected artifacts:",
            *[f"  - {line}" for line in expected],
            "- Artifacts found:",
            *[f"  - {line}" for line in found],
            "",
            "## Human review",
            f"Human review required: {review}",
            f"- Isolation: {info.isolation or 'clone'}",
            f"- Import: {info.import_note}",
            "",
            "## Trust boundary",
            f"- runtime GROK_HOME scope: {info.grok_home_scope or 'per-run'}",
            f"- source read isolation: {info.source_read_isolation or 'not-probed'}",
            f"- operator-home read isolation: {info.operator_home_read_isolation or 'not-probed'}",
            f"- source integrity: {info.source_integrity or 'not-probed'}",
            f"- extension audit: {info.extension_audit or 'not-probed'}",
            f"- network containment: {info.network_containment or 'accepted limitation'}",
            f"- inspect containment: {info.inspect_containment or 'not-probed'}",
            "",
            "## Inspect",
            "These commands are read-only.",
            "```",
            *inspect,
            "```",
            "",
        ]
    )


def summarize_stream(text: str) -> str:
    """Pull text fields out of streaming JSON. Unknown shapes are ignored.

    The raw log stays on disk. Reports use this summary instead of scraping
    the whole provider transcript.
    """
    parts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        _collect_text(event, parts, depth=0)
        if sum(len(part) for part in parts) > 4000:
            break
    return "\n".join(parts)


def _collect_text(value, parts: list[str], *, depth: int) -> None:
    if depth > 6 or len(parts) > 40:
        return
    if isinstance(value, dict):
        if value.get("type") == "text" and isinstance(value.get("data"), str) and value["data"].strip():
            parts.append(value["data"].strip())
        for key in ("text", "message", "content", "result"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
        for item in value.values():
            if isinstance(item, (dict, list)):
                _collect_text(item, parts, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _collect_text(item, parts, depth=depth + 1)


def _inspect_commands(info: ReportInputs) -> list[str]:
    if not info.workspace:
        return ["# workspace was not created; inspect the run directory logs instead"]
    workspace = shlex.quote(info.workspace)
    revision = shlex.quote(info.source_revision) if info.source_revision else "HEAD"
    return [
        f"git -C {workspace} status --porcelain=v1",
        f"git -C {workspace} log --oneline --decorate -n 20",
        f"git -C {workspace} diff --stat {revision}",
        f"git -C {workspace} rev-parse HEAD",
    ]
