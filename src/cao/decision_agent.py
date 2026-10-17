"""Advisory Decision Agent: a deterministic validator for model plans (Batch B).

This is the isolated first landing of **Batch B**. It adds one bounded,
stdlib-only module that turns an untrusted, model-produced judgement into a
structured, **deterministically validated, advisory-only** plan. It is not a
runtime, not a scheduler, not an execution path, not a manager and not a second
ledger. A valid plan is **data**, never execution.

Load-bearing rules
------------------

* **Model output is data, never instructions.** The model stage may emit only
  the schema below; every field is validated and the result is always
  ``status = "PROPOSED"`` (advisory) or ``"HELD"`` (fail closed). The validator
  never authorizes, creates a team, task or Registry claim, edits a file or
  dispatches anything -- it has no such capability and imports no surface that
  would grant one.
* **The model cannot assert provenance, evidence or approval.** Source
  references are checked against independently supplied references *and* a
  separately supplied "source known" predicate; a claimed reference the
  independent evidence does not know is a spoof and is refused. Required
  approvals are computed from the declared action class, so an under-declared or
  invented gate is refused, and any unknown field (for example a self-asserted
  approval or a ``dispatch`` flag) is refused outright.
* **Privacy and route are qualified independently.** The model stage is
  ``enabled = False`` by default. When enabled, eligibility is decided *only*
  from independently supplied allowlist/privacy evidence -- never by calling a
  model, reading Intake content, or trusting the plan's own declaration. An
  unlisted route, an unqualified provider, an unqualified data class or a
  non-zero-retention route leaves the stage held.
* **Mixed requests are decomposed, not downgraded or frozen.** A plan spanning a
  routine READ and a scoped WRITE or reserved effect keeps the more
  consequential class, holds that portion with a precise reason, and exposes the
  separately eligible READ portion as an advisory review. The read portion is
  never silently promoted into the reserved scope and the reserved portion is
  never silently downgraded to a READ.
* **Unknown cannot round up.** Missing, stale, contradictory, spoofed or
  unqualified evidence holds with an explicit code. A lost prior completion
  receipt holds ``UNKNOWN`` and never replays a prompt.

Separation and scope
--------------------

Standard library only. This module imports no runtime plugin, no egress module,
no shell and no execution entry point; it reads no Intake record and writes
nothing. The existing managed READ team path remains the **sole** model
execution path, and final authority plus the Work Registry stay separate and
outside this module.

DEFERRED (separate reviewed change): the boundary-separation rule for this
module in ``tests/test_boundaries.py`` and any Batch B integration into the
daemon/team cadence. This landing is deliberately isolated and touches no shared
interface; integration waits for the overlapping writers to settle and is not
performed here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from . import evidence

__all__ = [
    "PLAN_SCHEMA",
    "DECISION_SCHEMA",
    "CONTEXT_SCHEMA",
    "ACTION_CLASSES",
    "REQUIRED_APPROVALS",
    "APPROVAL_TOKENS",
    "DATA_CLASSES",
    "VERDICTS",
    "RETENTION_CLASSES",
    "JUDGMENT_KINDS",
    "COMPLETION_VERDICTS",
    "MAX_PLAN_BYTES",
    "MAX_TEXT",
    "DecisionHeld",
    "eligibility",
    "validate_plan",
]

#: The advisory model-plan schema this module validates. The model stage may
#: emit only this structure; every field is required unless marked optional.
PLAN_SCHEMA = "cao.decision.plan.v1"
#: The validator's own advisory verdict envelope. It carries a ``status`` of
#: ``PROPOSED`` or ``HELD`` and is never a plan, an approval or a dispatch.
DECISION_SCHEMA = "cao.decision.plan.review.v1"
#: The schema of the independently supplied evidence document.
CONTEXT_SCHEMA = "cao.decision.evidence.v1"

#: The bounded action classes, least to most consequential. A plan declares
#: exactly one, and a subtask declares exactly one.
ACTION_CLASSES = ("READ", "WRITE", "RESERVED")
_ACTION_ORDER = {"READ": 0, "WRITE": 1, "RESERVED": 2}

#: The deterministic approval requirements per action class. A model plan may
#: not under-declare these; the validator emits them authoritatively regardless
#: of what the model declared, so a declared approval never satisfies a gate.
REQUIRED_APPROVALS = {
    "READ": ("operator_read_gate",),
    "WRITE": (
        "operator_read_gate",
        "approved_manifest",
        "live_registry_preflight",
        "atomic_claim",
    ),
    "RESERVED": ("operator_consequential_authorization",),
}
APPROVAL_TOKENS = frozenset(
    token for tokens in REQUIRED_APPROVALS.values() for token in tokens
)

#: Bounded data-class vocabulary. Absent or unknown is refused.
DATA_CLASSES = frozenset({"synthetic", "source_attested"})
#: The frozen evidence verdict vocabulary. Only ``SUPPORTED`` is positive.
VERDICTS = frozenset(item.value for item in evidence.Verdict)
POSITIVE = evidence.Verdict.SUPPORTED.value
UNKNOWN = evidence.Verdict.UNKNOWN.value
#: A route must carry an explicit zero-data-retention declaration; anything
#: else is unqualified and leaves the model stage held.
RETENTION_CLASSES = ("zero",)
#: Independent completion verdicts. Only ``ALREADY_COMPLETE`` proves the work is
#: done; anything else is not a duplicate.
COMPLETION_VERDICTS = frozenset({"ALREADY_COMPLETE", "ABSENT"}) | VERDICTS
#: Bounded judgment-event kinds. ``recovery`` requires a verified prior receipt.
JUDGMENT_KINDS = ("review", "work", "recovery")

#: Tight, explicit bounds. A plan exceeding any of them is refused.
MAX_PLAN_BYTES = 256 * 1024
MAX_TEXT = 4000
MAX_SHORT = 200
MAX_SUBTASKS = 64
MAX_REFS = 64
MAX_TESTS = 64
MAX_APPROVALS = 16

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HEAD_RE = re.compile(r"[0-9a-f]{40}\Z")
_UTC_TIME_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z\Z"
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")
_ROLE_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_REPO_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,98}/[A-Za-z0-9][A-Za-z0-9._-]{0,98}\Z"
)
_ROUTE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,150}\Z")
_REF_RE = re.compile(r"(?:urn:|git:|https://)[^\s\x00-\x1f]{1,499}\Z")

#: The exact model-plan keys. Unknown keys are refused; ``accepted_plan_ref`` is
#: the only optional key.
_PLAN_REQUIRED = frozenset({
    "schema",
    "source_refs",
    "plan",
    "subtasks",
    "dependencies",
    "owner",
    "capability",
    "privacy_and_model_qualification",
    "confidence",
    "uncertainty",
    "action_class",
    "tests",
    "approval_requirements",
})
_PLAN_OPTIONAL = frozenset({"accepted_plan_ref"})
_PLAN_ALLOWED = _PLAN_REQUIRED | _PLAN_OPTIONAL
_MODEL_REF_KEYS = frozenset({"location", "digest"})
_SUBTASK_KEYS = frozenset({"id", "summary", "action_class"})
_DEPENDENCY_KEYS = frozenset({"id", "depends_on"})
_CAPABILITY_KEYS = frozenset({"kind", "performer"})
_DECLARED_PRIVACY_KEYS = frozenset({"data_class", "route"})

#: The exact independently supplied evidence keys.
_CONTEXT_KEYS = frozenset({
    "schema",
    "source_refs",
    "source_known",
    "route_qualification",
    "registry_preflight",
    "completed_work",
    "prior_receipt",
    "accepted_plan",
    "judgment",
})
_SOURCE_REF_KEYS = frozenset(
    {"location", "digest", "cutoff", "head", "scope", "verdict"}
)
_REF_SCOPE_KEYS = frozenset({"owner", "repository", "data_class"})
_QUALIFICATION_KEYS = frozenset({
    "route",
    "allowlist",
    "data_class",
    "qualified_data_classes",
    "provider_qualified",
    "retention",
})
_PREFLIGHT_KEYS = frozenset({"verdict", "code", "reason"})
_COMPLETED_KEYS = frozenset({"verdict", "refs"})
_RECEIPT_KEYS = frozenset({"verdict", "plan_ref"})
_ACCEPTED_KEYS = frozenset({"verdict", "plan_ref"})
_JUDGMENT_KEYS = frozenset({"kind", "read_only"})


class DecisionHeld(Exception):
    """A fail-closed hold or refusal, carrying an explicit reason code."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


# ---------------------------------------------------------------------------
# Bounded, strict field validation
# ---------------------------------------------------------------------------


def _require_keys(
    value: Any, allowed: frozenset[str], required: frozenset[str], what: str
) -> dict:
    if type(value) is not dict:
        raise DecisionHeld("plan_not_object", f"{what}: expected an object")
    unknown = set(value) - allowed
    if unknown:
        raise DecisionHeld(
            "unexpected_field", f"{what}: unsupported field(s) {sorted(unknown)}"
        )
    missing = required - set(value)
    if missing:
        raise DecisionHeld(
            "missing_field", f"{what}: missing required field(s) {sorted(missing)}"
        )
    return value


def _text(value: Any, field: str, limit: int, *, allow_none: bool = True) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise DecisionHeld("missing_field", f"{field}: required text is absent")
    if not isinstance(value, str) or not value.strip():
        raise DecisionHeld("field_invalid", f"{field}: expected non-empty text")
    if len(value) > limit:
        raise DecisionHeld("field_invalid", f"{field}: text exceeds {limit} characters")
    if any(ord(ch) < 0x20 and ch not in "\n\t" for ch in value):
        raise DecisionHeld("field_invalid", f"{field}: text contains a control character")
    return value


def _token(value: Any, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise DecisionHeld("field_invalid", f"{field}: not a bounded safe token")
    return value


def _digest(value: Any, field: str, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise DecisionHeld("missing_field", f"{field}: a content digest is required")
        return None
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise DecisionHeld("field_invalid", f"{field}: expected a sha256 content digest")
    return value


def _head(value: Any, field: str, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise DecisionHeld("missing_field", f"{field}: a source SHA is required")
        return None
    if not isinstance(value, str) or not _HEAD_RE.fullmatch(value):
        raise DecisionHeld("field_invalid", f"{field}: expected a 40-hex commit id")
    return value


def _utc_time(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _UTC_TIME_RE.fullmatch(value):
        raise DecisionHeld(
            "field_invalid", f"{field}: expected an RFC 3339 UTC timestamp ending Z"
        )
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:  # pragma: no cover - the regex already constrains shape
        raise DecisionHeld("field_invalid", f"{field}: invalid UTC timestamp") from exc
    if moment.utcoffset() != timedelta(0):
        raise DecisionHeld("field_invalid", f"{field}: timestamp must be UTC")
    return value


def _safe_ref(value: Any, field: str) -> str:
    text = _text(value, field, 500, allow_none=False)
    assert text is not None
    if not _REF_RE.fullmatch(text):
        raise DecisionHeld(
            "field_invalid", f"{field}: expected a urn:, git: or safe https reference"
        )
    return text


def _route_token(value: Any, field: str) -> str:
    token = _token(value, field, _ROUTE_RE)
    if ".." in token or "//" in token:
        raise DecisionHeld("field_invalid", f"{field}: unsafe route token")
    base = token.split("#", 1)[0]
    if "/" not in base or base.startswith("/") or base.endswith("/"):
        raise DecisionHeld("field_invalid", f"{field}: expected a provider/model route")
    return base


def _bounded_list(value: Any, field: str, limit: int) -> list:
    if not isinstance(value, list):
        raise DecisionHeld("field_invalid", f"{field}: expected a list")
    if len(value) > limit:
        raise DecisionHeld("field_invalid", f"{field}: exceeds {limit} entries")
    return value


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionHeld("field_invalid", "plan.confidence: expected a number")
    if not 0.0 <= float(value) <= 1.0:
        raise DecisionHeld("field_invalid", "plan.confidence: must be within [0, 1]")
    return float(value)


# ---------------------------------------------------------------------------
# The model plan (untrusted input)
# ---------------------------------------------------------------------------


def _reject_constant(name: str) -> float:
    raise ValueError(f"non-finite number: {name}")


def _decode(model_output: Any) -> dict:
    if isinstance(model_output, (bytes, bytearray)):
        try:
            model_output = model_output.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DecisionHeld("invalid_json", "model output is not valid UTF-8") from exc
    if isinstance(model_output, str):
        try:
            output_size = len(model_output.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise DecisionHeld(
                "invalid_json", "model output is not valid UTF-8"
            ) from exc
        if output_size > MAX_PLAN_BYTES:
            raise DecisionHeld("oversize", "model output exceeds the byte bound")
        try:
            parsed = json.loads(model_output, parse_constant=_reject_constant)
        except (ValueError, TypeError, RecursionError) as exc:
            raise DecisionHeld("invalid_json", "model output is not valid JSON") from exc
        model_output = parsed
    if isinstance(model_output, Mapping):
        node = dict(model_output)
    else:
        raise DecisionHeld("plan_not_object", "model output must be a JSON object")
    try:
        size = len(evidence.canonical_json(node).encode("utf-8"))
    except (ValueError, TypeError, RecursionError) as exc:
        raise DecisionHeld(
            "invalid_json", "model plan is not canonicalizable JSON"
        ) from exc
    if size > MAX_PLAN_BYTES:
        raise DecisionHeld("oversize", "model plan exceeds the byte bound")
    return node


def _normalize_model_refs(raw: Any) -> list[dict[str, str]]:
    entries = _bounded_list(raw, "plan.source_refs", MAX_REFS)
    if not entries:
        raise DecisionHeld(
            "missing_field", "plan.source_refs: at least one reference is required"
        )
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in entries:
        node = _require_keys(item, _MODEL_REF_KEYS, _MODEL_REF_KEYS, "plan.source_ref")
        location = _safe_ref(node["location"], "plan.source_ref.location")
        if location in seen:
            raise DecisionHeld("duplicate_reference", "plan.source_refs holds a duplicate")
        seen.add(location)
        refs.append(
            {
                "location": location,
                "digest": _digest(node["digest"], "plan.source_ref.digest", required=True),
            }
        )
    return refs


def _normalize_subtasks(raw: Any) -> list[dict[str, str]]:
    entries = _bounded_list(raw, "plan.subtasks", MAX_SUBTASKS)
    subtasks: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in entries:
        node = _require_keys(item, _SUBTASK_KEYS, _SUBTASK_KEYS, "plan.subtask")
        sid = _token(node["id"], "plan.subtask.id", _ID_RE)
        if sid in seen:
            raise DecisionHeld("duplicate_subtask", "plan.subtasks holds a duplicate id")
        seen.add(sid)
        action = node["action_class"]
        if action not in ACTION_CLASSES:
            raise DecisionHeld(
                "action_class_invalid", "plan.subtask.action_class is out of vocabulary"
            )
        subtasks.append(
            {
                "id": sid,
                "summary": _text(
                    node["summary"], "plan.subtask.summary", MAX_SHORT, allow_none=False
                ),
                "action_class": action,
            }
        )
    return subtasks


def _normalize_dependencies(raw: Any, subtasks: list[dict[str, str]]) -> list[dict[str, Any]]:
    entries = _bounded_list(raw, "plan.dependencies", MAX_SUBTASKS)
    ids = [subtask["id"] for subtask in subtasks]
    known = set(ids)
    nodes: list[dict[str, Any]] = []
    declared: set[str] = set()
    for item in entries:
        node = _require_keys(item, _DEPENDENCY_KEYS, _DEPENDENCY_KEYS, "plan.dependency")
        sid = _token(node["id"], "plan.dependency.id", _ID_RE)
        if sid not in known:
            raise DecisionHeld(
                "dangling_dependency", "plan.dependency names an unknown subtask"
            )
        if sid in declared:
            raise DecisionHeld(
                "duplicate_dependency", "plan.dependencies repeats a subtask id"
            )
        declared.add(sid)
        depends = _bounded_list(node["depends_on"], "plan.dependency.depends_on", MAX_SUBTASKS)
        resolved: list[str] = []
        for target in depends:
            target_id = _token(target, "plan.dependency.depends_on", _ID_RE)
            if target_id not in known:
                raise DecisionHeld(
                    "dangling_dependency", "a depends_on id names an unknown subtask"
                )
            if target_id not in resolved:
                resolved.append(target_id)
        nodes.append({"id": sid, "depends_on": resolved})
    _check_acyclic(ids, nodes)
    return nodes


def _check_acyclic(ids: list[str], nodes: list[dict[str, Any]]) -> None:
    graph: dict[str, list[str]] = {sid: [] for sid in ids}
    for node in nodes:
        graph[node["id"]].extend(node["depends_on"])
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        if node in done:
            return
        if node in visiting:
            raise DecisionHeld(
                "dependency_cycle", "plan.dependencies contains a cycle"
            )
        visiting.add(node)
        for nxt in graph[node]:
            visit(nxt)
        visiting.discard(node)
        done.add(node)

    for sid in ids:
        visit(sid)


def _normalize_capability(raw: Any) -> dict[str, str]:
    node = _require_keys(raw, _CAPABILITY_KEYS, _CAPABILITY_KEYS, "plan.capability")
    return {
        "kind": _token(node["kind"], "plan.capability.kind", _TOKEN_RE),
        "performer": _token(node["performer"], "plan.capability.performer", _TOKEN_RE),
    }


def _normalize_declared_privacy(raw: Any) -> dict[str, str]:
    node = _require_keys(
        raw, _DECLARED_PRIVACY_KEYS, _DECLARED_PRIVACY_KEYS, "plan.privacy"
    )
    data_class = node["data_class"]
    if not isinstance(data_class, str) or data_class not in DATA_CLASSES:
        raise DecisionHeld("field_invalid", "plan.privacy.data_class: unsupported class")
    return {
        "data_class": data_class,
        "route": _route_token(node["route"], "plan.privacy.route"),
    }


def _normalize_approvals(raw: Any, action_class: str) -> list[str]:
    entries = _bounded_list(raw, "plan.approval_requirements", MAX_APPROVALS)
    declared = set()
    for item in entries:
        token = _token(item, "plan.approval_requirements", _ID_RE)
        if token not in APPROVAL_TOKENS:
            raise DecisionHeld(
                "unexpected_field", "plan.approval_requirements names an unknown gate"
            )
        declared.add(token)
    required = set(REQUIRED_APPROVALS[action_class])
    if not required <= declared:
        raise DecisionHeld(
            "approval_requirements_insufficient",
            "plan under-declares the gates the action class requires; a model "
            "never satisfies or reduces a gate",
        )
    return sorted(declared)


def _max_action(classes: list[str], declared: str) -> str:
    if not classes:
        return declared
    return max(classes, key=lambda item: _ACTION_ORDER[item])


def _normalize_plan(raw: Any) -> dict[str, Any]:
    node = _require_keys(raw, _PLAN_ALLOWED, _PLAN_REQUIRED, "plan")
    if node["schema"] != PLAN_SCHEMA:
        raise DecisionHeld("schema_invalid", f"plan: schema is not {PLAN_SCHEMA}")
    refs = _normalize_model_refs(node["source_refs"])
    subtasks = _normalize_subtasks(node["subtasks"])
    dependencies = _normalize_dependencies(node["dependencies"], subtasks)
    action_class = node["action_class"]
    if not isinstance(action_class, str) or action_class not in ACTION_CLASSES:
        raise DecisionHeld("action_class_invalid", "plan.action_class is out of vocabulary")
    computed = _max_action([subtask["action_class"] for subtask in subtasks], action_class)
    if computed != action_class:
        raise DecisionHeld(
            "action_class_downgrade",
            "plan.action_class does not match its most consequential subtask; a "
            "model never widens or downgrades the class",
        )
    tests = _bounded_list(node["tests"], "plan.tests", MAX_TESTS)
    normalized_tests = [
        _text(item, "plan.tests", MAX_SHORT, allow_none=False) for item in tests
    ]
    if action_class != "READ" and not normalized_tests:
        raise DecisionHeld(
            "missing_field", "plan.tests: a non-READ plan requires at least one test"
        )
    approvals = _normalize_approvals(node["approval_requirements"], action_class)
    accepted = node.get("accepted_plan_ref")
    if accepted is not None:
        accepted = _text(accepted, "plan.accepted_plan_ref", MAX_SHORT, allow_none=False)
    return {
        "schema": PLAN_SCHEMA,
        "source_refs": refs,
        "plan": _text(node["plan"], "plan.plan", MAX_TEXT, allow_none=False),
        "subtasks": subtasks,
        "dependencies": dependencies,
        "owner": _token(node["owner"], "plan.owner", _ROLE_RE),
        "capability": _normalize_capability(node["capability"]),
        "privacy_and_model_qualification": _normalize_declared_privacy(
            node["privacy_and_model_qualification"]
        ),
        "confidence": _confidence(node["confidence"]),
        "uncertainty": _text(
            node["uncertainty"], "plan.uncertainty", MAX_TEXT, allow_none=False
        ),
        "action_class": action_class,
        "tests": normalized_tests,
        "approval_requirements": approvals,
        "accepted_plan_ref": accepted,
    }


# ---------------------------------------------------------------------------
# The independently supplied evidence document (never model-asserted)
# ---------------------------------------------------------------------------


def _normalize_ref_scope(raw: Any) -> dict[str, str | None]:
    node = _require_keys(raw, _REF_SCOPE_KEYS, frozenset({"data_class"}), "scope")
    data_class = node["data_class"]
    if not isinstance(data_class, str) or data_class not in DATA_CLASSES:
        raise DecisionHeld("field_invalid", "scope.data_class: unsupported class")
    owner = node.get("owner")
    repository = node.get("repository")
    return {
        "owner": None if owner is None else _token(owner, "scope.owner", _TOKEN_RE),
        "repository": (
            None
            if repository is None
            else _token(repository, "scope.repository", _REPO_RE)
        ),
        "data_class": data_class,
    }


def _normalize_ref(raw: Any) -> dict[str, Any]:
    node = _require_keys(raw, _SOURCE_REF_KEYS, _SOURCE_REF_KEYS, "context.source_ref")
    verdict = node["verdict"]
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        raise DecisionHeld("field_invalid", "context.source_ref.verdict: unsupported verdict")
    return {
        "location": _safe_ref(node["location"], "context.source_ref.location"),
        "digest": _digest(node["digest"], "context.source_ref.digest", required=True),
        "cutoff": _utc_time(node["cutoff"], "context.source_ref.cutoff"),
        "head": _head(node["head"], "context.source_ref.head", required=True),
        "scope": _normalize_ref_scope(node["scope"]),
        "verdict": verdict,
    }


def _normalize_qualification(raw: Any) -> dict[str, Any]:
    node = _require_keys(
        raw, _QUALIFICATION_KEYS, _QUALIFICATION_KEYS, "context.route_qualification"
    )
    route = _route_token(node["route"], "context.route_qualification.route")
    allow = _bounded_list(node["allowlist"], "context.route_qualification.allowlist", 32)
    allowlist = frozenset(
        _route_token(item, "context.route_qualification.allowlist") for item in allow
    )
    data_class = node["data_class"]
    if not isinstance(data_class, str) or data_class not in DATA_CLASSES:
        raise DecisionHeld(
            "field_invalid", "context.route_qualification.data_class: unsupported class"
        )
    qualified = _bounded_list(
        node["qualified_data_classes"],
        "context.route_qualification.qualified_data_classes",
        8,
    )
    qualified_classes = frozenset(
        _data_class(item, "context.route_qualification.qualified_data_classes")
        for item in qualified
    )
    provider = node["provider_qualified"]
    if type(provider) is not bool:
        raise DecisionHeld(
            "field_invalid", "context.route_qualification.provider_qualified: expected bool"
        )
    retention = node["retention"]
    if not isinstance(retention, str) or not retention:
        raise DecisionHeld(
            "field_invalid", "context.route_qualification.retention: expected a bounded class"
        )
    return {
        "route": route,
        "allowlist": allowlist,
        "data_class": data_class,
        "qualified_data_classes": qualified_classes,
        "provider_qualified": provider,
        "retention": retention,
    }


def _data_class(value: Any, field: str) -> str:
    if not isinstance(value, str) or value not in DATA_CLASSES:
        raise DecisionHeld("field_invalid", f"{field}: unsupported class")
    return value


def _normalize_preflight(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    node = _require_keys(raw, _PREFLIGHT_KEYS, frozenset({"verdict"}), "context.registry_preflight")
    verdict = node["verdict"]
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        raise DecisionHeld(
            "field_invalid", "context.registry_preflight.verdict: unsupported verdict"
        )
    return {
        "verdict": verdict,
        "code": _text(node.get("code"), "context.registry_preflight.code", MAX_SHORT),
        "reason": _text(node.get("reason"), "context.registry_preflight.reason", MAX_TEXT),
    }


def _normalize_completed(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    node = _require_keys(raw, _COMPLETED_KEYS, _COMPLETED_KEYS, "context.completed_work")
    verdict = node["verdict"]
    if not isinstance(verdict, str) or verdict not in COMPLETION_VERDICTS:
        raise DecisionHeld(
            "field_invalid", "context.completed_work.verdict: unsupported verdict"
        )
    refs = _bounded_list(node["refs"], "context.completed_work.refs", MAX_REFS)
    return {
        "verdict": verdict,
        "refs": [_safe_ref(item, "context.completed_work.refs") for item in refs],
    }


def _normalize_receipt(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    node = _require_keys(raw, _RECEIPT_KEYS, frozenset({"verdict"}), "context.prior_receipt")
    verdict = node["verdict"]
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        raise DecisionHeld(
            "field_invalid", "context.prior_receipt.verdict: unsupported verdict"
        )
    plan_ref = node.get("plan_ref")
    return {
        "verdict": verdict,
        "plan_ref": _text(plan_ref, "context.prior_receipt.plan_ref", MAX_SHORT),
    }


def _normalize_accepted(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    node = _require_keys(raw, _ACCEPTED_KEYS, _ACCEPTED_KEYS, "context.accepted_plan")
    verdict = node["verdict"]
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        raise DecisionHeld(
            "field_invalid", "context.accepted_plan.verdict: unsupported verdict"
        )
    return {
        "verdict": verdict,
        "plan_ref": _text(
            node["plan_ref"], "context.accepted_plan.plan_ref", MAX_SHORT, allow_none=False
        ),
    }


def _normalize_judgment(raw: Any) -> dict[str, Any]:
    node = _require_keys(raw, _JUDGMENT_KEYS, _JUDGMENT_KEYS, "context.judgment")
    kind = node["kind"]
    if not isinstance(kind, str) or kind not in JUDGMENT_KINDS:
        raise DecisionHeld("field_invalid", "context.judgment.kind: unsupported kind")
    read_only = node["read_only"]
    if type(read_only) is not bool:
        raise DecisionHeld("field_invalid", "context.judgment.read_only: expected bool")
    return {"kind": kind, "read_only": read_only}


def _normalize_context(raw: Any) -> dict[str, Any]:
    node = _require_keys(raw, _CONTEXT_KEYS, _CONTEXT_KEYS, "context")
    if node["schema"] != CONTEXT_SCHEMA:
        raise DecisionHeld("evidence_schema_invalid", f"context: schema is not {CONTEXT_SCHEMA}")
    refs_raw = _bounded_list(node["source_refs"], "context.source_refs", MAX_REFS)
    refs = [_normalize_ref(item) for item in refs_raw]
    checker = node["source_known"]
    if not callable(checker):
        raise DecisionHeld(
            "source_check_unavailable",
            "context.source_known: a separately supplied predicate is required; the "
            "model never asserts its own provenance",
        )
    return {
        "schema": CONTEXT_SCHEMA,
        "source_refs": refs,
        "source_known": checker,
        "route_qualification": _normalize_qualification(node["route_qualification"]),
        "registry_preflight": _normalize_preflight(node["registry_preflight"]),
        "completed_work": _normalize_completed(node["completed_work"]),
        "prior_receipt": _normalize_receipt(node["prior_receipt"]),
        "accepted_plan": _normalize_accepted(node["accepted_plan"]),
        "judgment": _normalize_judgment(node["judgment"]),
    }


# ---------------------------------------------------------------------------
# Deterministic eligibility and cross-checks
# ---------------------------------------------------------------------------


def _require_qualified(qualification: Mapping[str, Any]) -> tuple[str, str]:
    if qualification["provider_qualified"] is not True:
        raise DecisionHeld(
            "unqualified_provider",
            "the provider is not independently qualified; the model stage stays held",
        )
    if qualification["route"] not in qualification["allowlist"]:
        raise DecisionHeld(
            "unqualified_route",
            "the route is not in the independently supplied allowlist; no substitute "
            "route is chosen",
        )
    if qualification["data_class"] not in qualification["qualified_data_classes"]:
        raise DecisionHeld(
            "unqualified_data_class",
            "the data class is not independently qualified for this route",
        )
    if qualification["retention"] != "zero":
        raise DecisionHeld(
            "unqualified_retention",
            "a non-zero-data-retention route is unqualified; the model stage stays held",
        )
    return qualification["route"], qualification["data_class"]


def _check_completed(completed: Mapping[str, Any] | None) -> None:
    if completed is not None and completed["verdict"] == "ALREADY_COMPLETE":
        raise DecisionHeld(
            "already_complete",
            "independently proven work already completes this scope; a duplicate is "
            "never re-dispatched",
        )


def _check_receipt(
    judgment: Mapping[str, Any], receipt: Mapping[str, Any] | None
) -> None:
    if judgment["kind"] != "recovery":
        return
    if receipt is None or receipt["verdict"] != POSITIVE:
        raise DecisionHeld(
            "receipt_unknown",
            "no verified prior completion receipt; the plan holds UNKNOWN and never "
            "replays a prompt or invents a receipt",
        )


def _check_registry(preflight: Mapping[str, Any] | None) -> None:
    if preflight is None:
        return
    if preflight["verdict"] == "REFUSED":
        raise DecisionHeld(
            "registry_conflict",
            "the independently supplied Registry preflight refuses an overlapping "
            "claim; no claim, task or team is created",
        )
    if preflight["verdict"] != POSITIVE:
        raise DecisionHeld(
            "registry_preflight_unknown",
            "the Registry preflight could not be established; fail closed",
        )


def _check_sources(plan: Mapping[str, Any], context: Mapping[str, Any]) -> None:
    known = {ref["location"]: ref for ref in context["source_refs"]}
    checker: Callable[[Mapping[str, Any]], Any] = context["source_known"]
    for claimed in plan["source_refs"]:
        ref = known.get(claimed["location"])
        if ref is None or ref["digest"] != claimed["digest"]:
            raise DecisionHeld(
                "source_spoofed",
                "the plan claims a source reference the independent evidence does "
                "not know; the model never asserts provenance",
            )
        if ref["verdict"] != POSITIVE:
            raise DecisionHeld(
                "source_stale",
                "an independent source reference is not positively supported; the "
                "plan is never grounded on stale or unknown facts",
            )
        try:
            observed = checker(dict(ref))
        except DecisionHeld:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed on any checker failure
            raise DecisionHeld(
                "source_check_failed",
                f"the source-known predicate failed ({type(exc).__name__})",
            ) from exc
        if observed is not True:
            raise DecisionHeld(
                "source_unknown",
                "the source-known predicate does not confirm the reference; UNKNOWN "
                "never rounds up",
            )


def _check_privacy(
    plan: Mapping[str, Any], route: str, data_class: str
) -> None:
    declared = plan["privacy_and_model_qualification"]
    if declared["route"] != route or declared["data_class"] != data_class:
        raise DecisionHeld(
            "privacy_mismatch",
            "the plan's declared route or data class does not match the independently "
            "qualified evidence",
        )


def _check_accepted_plan(
    plan: Mapping[str, Any], accepted: Mapping[str, Any] | None
) -> None:
    ref = plan["accepted_plan_ref"]
    if ref is None:
        return
    if accepted is None or accepted["verdict"] != POSITIVE or accepted["plan_ref"] != ref:
        raise DecisionHeld(
            "accepted_plan_unverified",
            "the plan names an accepted-plan reference that no independent checker "
            "confirms; a string is never trusted as acceptance",
        )


def _check_review_only(judgment: Mapping[str, Any], plan: Mapping[str, Any]) -> None:
    if judgment["read_only"] and plan["action_class"] != "READ":
        raise DecisionHeld(
            "review_only",
            "the judgment event is review-only; only a READ/review plan is safe and a "
            "WRITE or reserved effect is never proposed",
        )


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


def _envelope(status: str, code: str | None, reason: str | None, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": DECISION_SCHEMA,
        "plan_schema": PLAN_SCHEMA,
        "status": status,
        "code": code,
        "reason": reason,
        "authority": "none",
        "dispatch": False,
        "effects": [],
        "egress": "none",
        "advisory_only": True,
        "action_class": None,
        "required_approvals": [],
        "plan": None,
        "read_portion": None,
        "reserved": [],
    }
    result.update(extra)
    return result


def _held(code: str, reason: str) -> dict[str, Any]:
    return _envelope("HELD", code, reason)


def _proposed(plan: Mapping[str, Any], route: str, data_class: str) -> dict[str, Any]:
    action_class = plan["action_class"]
    read_subtasks = [
        dict(subtask) for subtask in plan["subtasks"] if subtask["action_class"] == "READ"
    ]
    reserved = [
        subtask["id"] for subtask in plan["subtasks"] if subtask["action_class"] != "READ"
    ]
    read_portion = (
        {"subtasks": read_subtasks, "action_class": "READ", "advisory": True}
        if read_subtasks
        else None
    )
    required = list(REQUIRED_APPROVALS[action_class])
    if action_class == "READ":
        return _envelope(
            "PROPOSED",
            None,
            None,
            action_class=action_class,
            required_approvals=required,
            plan=dict(plan),
            read_portion=read_portion,
            reserved=[],
            route=route,
            data_class=data_class,
        )
    if action_class == "WRITE":
        code = "write_held_for_authorization"
        reason = (
            "a scoped WRITE is held: it requires a separate approved manifest, a live "
            "Registry preflight and an atomic claim, and a model plan never grants them"
        )
    else:
        code = "reserved_effect_held"
        reason = (
            "a reserved consequential effect is preserved, not downgraded or frozen: "
            "it is held for a distinct accountable operator authorization"
        )
    return _envelope(
        "HELD",
        code,
        reason,
        action_class=action_class,
        required_approvals=required,
        plan=dict(plan),
        read_portion=read_portion,
        reserved=reserved,
        route=route,
        data_class=data_class,
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def eligibility(
    context: Any, *, enabled: bool = False
) -> dict[str, Any]:
    """Deterministic route + data-class eligibility from independent evidence.

    This never calls a model and never reads Intake content. With the default
    ``enabled = False`` the model stage is held; otherwise eligibility depends
    solely on the independently supplied allowlist/privacy evidence.
    """

    if not enabled:
        return {
            "eligible": False,
            "code": "model_stage_disabled",
            "reason": "the model stage is disabled by default; the deterministic path stands",
            "route": None,
            "data_class": None,
        }
    try:
        qualification = _normalize_qualification(
            _require_keys(context, _CONTEXT_KEYS, _CONTEXT_KEYS, "context")[
                "route_qualification"
            ]
        )
        route, data_class = _require_qualified(qualification)
    except DecisionHeld as held:
        return {
            "eligible": False,
            "code": held.code,
            "reason": held.reason,
            "route": None,
            "data_class": None,
        }
    return {
        "eligible": True,
        "code": None,
        "reason": None,
        "route": route,
        "data_class": data_class,
    }


def validate_plan(
    model_output: Any,
    *,
    context: Any,
    enabled: bool = False,
) -> dict[str, Any]:
    """Validate one model plan against its schema and the independent evidence.

    Returns an advisory envelope with ``status`` of ``PROPOSED`` or ``HELD``.
    The result is data: it never authorizes, creates a claim, team or task,
    edits a file or dispatches. A malformed plan, a spoofed source, an
    unqualified route or any missing, stale or contradictory evidence holds
    with an explicit code.
    """

    if not enabled:
        return _held(
            "model_stage_disabled",
            "the model stage is disabled by default; the deterministic path stands "
            "and the plan is not processed",
        )
    try:
        plan = _normalize_plan(_decode(model_output))
        node = _normalize_context(context)
        route, data_class = _require_qualified(node["route_qualification"])
        _check_completed(node["completed_work"])
        _check_receipt(node["judgment"], node["prior_receipt"])
        _check_registry(node["registry_preflight"])
        _check_sources(plan, node)
        _check_privacy(plan, route, data_class)
        _check_accepted_plan(plan, node["accepted_plan"])
        _check_review_only(node["judgment"], plan)
    except DecisionHeld as held:
        return _held(held.code, held.reason)
    return _proposed(plan, route, data_class)
