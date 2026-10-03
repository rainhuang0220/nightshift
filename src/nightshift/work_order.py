"""Engineering Work Order v1. Standalone JSON boundary; no planner imports.

Each project ships its own small validator for the published wire contract.
Canonical serialization is UTF-8, sorted keys, two-space indentation, LF.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

MAX_BYTES = 1024 * 1024
SCHEMA = "engineering.work-order"
VERSION = 1
ACTIONS = frozenset({"read", "edit", "commit", "push", "publish", "deploy", "credentials", "external-write"})
REQUIRED_DENIALS = frozenset({"push", "publish", "deploy", "credentials", "external-write"})


class WorkOrderError(ValueError):
    pass


def timestamp(value: object, label: str) -> datetime:
    text = _text(value, label)
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkOrderError(f"{label} must be an RFC 3339 timestamp") from exc
    if result.tzinfo is None or "T" not in text:
        raise WorkOrderError(f"{label} must include a timezone")
    return result


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value or len(value) > 16000:
        raise WorkOrderError(f"{label} must be a nonempty string without NUL (at most 16000 characters)")
    return value


def _object(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise WorkOrderError(f"{label} must have exactly these fields: {', '.join(sorted(keys))}")
    return value


def _strings(value: object, label: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > 200 or (nonempty and not value):
        raise WorkOrderError(f"{label} must be an array of strings{' with at least one entry' if nonempty else ''} (at most 200)")
    for item in value:
        _text(item, label)
    return value


def validate(data: object) -> dict:
    order = _object(data, {"schema", "version", "task_id", "origin", "repository", "title", "objective",
                           "rationale", "evidence", "constraints", "validation", "actions", "references",
                           "created_at", "provenance"}, "work order")
    if order["schema"] != SCHEMA or type(order["version"]) is not int or order["version"] != VERSION:
        raise WorkOrderError("unsupported work-order schema/version; expected engineering.work-order version 1")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", _text(order["task_id"], "task_id")):
        raise WorkOrderError("invalid task_id")
    for key in ("title", "objective", "rationale"):
        _text(order[key], key)
    origin = _object(order["origin"], {"name", "reference"}, "origin")
    for key in origin:
        _text(origin[key], f"origin.{key}")
    repo = _object(order["repository"], {"identity", "path", "url", "base_revision"}, "repository")
    _text(repo["identity"], "repository.identity")
    if repo["path"] is not None:
        if not Path(_text(repo["path"], "repository.path")).is_absolute():
            raise WorkOrderError("repository.path must be absolute or null")
    if repo["url"] is not None:
        _text(repo["url"], "repository.url")
    if repo["path"] is None and repo["url"] is None:
        raise WorkOrderError("repository needs a local path or URL")
    revision = repo["base_revision"]
    if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision)):
        raise WorkOrderError("base_revision must be a full commit hash or null")
    created = timestamp(order["created_at"], "created_at")
    provenance = _object(order["provenance"], {"opportunity_id", "decision_at", "evidence_ids"}, "provenance")
    _text(provenance["opportunity_id"], "provenance.opportunity_id")
    decision = timestamp(provenance["decision_at"], "provenance.decision_at")
    if decision > created:
        raise WorkOrderError("decision_at must not be later than created_at")
    evidence = order["evidence"]
    if not isinstance(evidence, list) or not evidence or len(evidence) > 200:
        raise WorkOrderError("evidence must contain 1–200 records")
    ids: set[str] = set()
    observed_ids: set[str] = set()
    for item in evidence:
        _object(item, {"id", "kind", "summary", "source", "observed_at", "expires_at", "observation_ids"}, "evidence item")
        identifier = _text(item["id"], "evidence.id")
        if identifier in ids:
            raise WorkOrderError("duplicate evidence id")
        ids.add(identifier)
        for key in ("summary", "source"):
            _text(item[key], f"evidence.{key}")
        refs = _strings(item["observation_ids"], "observation_ids", nonempty=True)
        if item["kind"] == "observation":
            observed = timestamp(item["observed_at"], "observed_at")
            if observed > decision or refs != [identifier]:
                raise WorkOrderError("observation must precede decision and reference itself")
            observed_ids.add(identifier)
        elif item["kind"] == "inference":
            if item["observed_at"] is not None:
                raise WorkOrderError("inference cannot claim an observation timestamp")
        else:
            raise WorkOrderError("evidence.kind must be observation or inference")
        if timestamp(item["expires_at"], "expires_at") <= created:
            raise WorkOrderError("evidence was stale at creation")
    if not observed_ids:
        raise WorkOrderError("at least one observation is required")
    for item in evidence:
        if not set(item["observation_ids"]) <= observed_ids:
            raise WorkOrderError("inference lineage references a missing observation")
    cited = _strings(provenance["evidence_ids"], "provenance.evidence_ids", nonempty=True)
    if len(cited) != len(set(cited)) or set(cited) != ids:
        raise WorkOrderError("provenance must cite exactly the exported evidence ids")
    _strings(order["constraints"], "constraints")
    _strings(order["references"], "references")
    actions = _object(order["actions"], {"allowed", "forbidden"}, "actions")
    allowed = set(_strings(actions["allowed"], "actions.allowed", nonempty=True))
    forbidden = set(_strings(actions["forbidden"], "actions.forbidden", nonempty=True))
    if not allowed <= ACTIONS or not forbidden <= ACTIONS or allowed & forbidden or not REQUIRED_DENIALS <= forbidden:
        raise WorkOrderError("unsupported/conflicting actions or missing mandatory denials")
    if not allowed <= {"read", "edit", "commit"} or "read" not in allowed:
        raise WorkOrderError("v1 allows only local read/edit/commit")
    checks = order["validation"]
    if not isinstance(checks, list) or not checks or len(checks) > 32:
        raise WorkOrderError("validation must contain 1–32 structured checks")
    for step in checks:
        _object(step, {"argv", "expectation", "timeout_seconds"}, "validation step")
        _strings(step["argv"], "validation.argv", nonempty=True)
        _text(step["expectation"], "validation.expectation")
        if type(step["timeout_seconds"]) is not int or not 1 <= step["timeout_seconds"] <= 86400:
            raise WorkOrderError("validation timeout_seconds must be an integer in 1..86400")
    return order


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkOrderError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def loads(text: str) -> dict:
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise WorkOrderError("work order exceeds 1 MiB")
    try:
        value = json.loads(text, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(WorkOrderError("nonfinite JSON")))
    except (ValueError, RecursionError) as exc:
        raise WorkOrderError(f"invalid work-order JSON: {exc}") from exc
    return validate(value)


def load(path: Path) -> dict:
    try:
        with path.expanduser().open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise WorkOrderError("work order exceeds 1 MiB")
        return loads(raw.decode("utf-8"))
    except (OSError, UnicodeError) as exc:
        raise WorkOrderError(f"cannot read work order: {exc}") from exc


def dumps(data: dict) -> str:
    text = json.dumps(validate(data), sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise WorkOrderError('work order is too large; maximum 1 MiB')
    return text
