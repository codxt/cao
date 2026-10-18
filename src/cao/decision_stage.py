"""Batch B2a decision stage: a bounded READ-prompt adapter and result validator.

This is the isolated landing of **Small Batch B2a**. It adds exactly one
stdlib-only module with two pure functions and no side effects of any kind:

* :func:`prepare_read_prompt` turns **externally verified** facts -- source
  references with head, digest, cutoff and scope, plus an independently
  qualified route and data class -- into one bounded, advisory READ prompt.
* :func:`validate_result` checks an externally verified terminal READ receipt
  and its tagged model output against externally supplied facts, extracts
  exactly one bounded JSON plan, and passes it to
  :func:`cao.decision_agent.validate_plan` together with an independently
  supplied typed evidence context.

Load-bearing rules
------------------

* **The prompt carries facts, never content.** Only the enumerated fields of
  the verified evidence reach the prompt: location, digest, head, cutoff,
  scope and verdict plus the qualified route and data class. There is no
  title, no body, no free text, no credential and no path outside the declared
  scope: unknown fields, unsafe references and non-token scope values are
  refused before anything is rendered. The module performs no I/O, opens no
  connection and runs no command.
* **A submission id must be explicit, enumerated and synthetic.** The id is
  checked against an explicitly supplied enumeration and must carry the
  synthetic prefix; an arbitrary or real-looking id never reaches a prompt.
* **Standing authorization is supplied from outside, never asserted.** An
  independently supplied predicate must already hold; a missing, failing or
  non-True predicate holds the prompt. A model assertion is not an input here.
* **Default off.** Both functions take ``enabled = False`` by default and
  return ``HELD`` with no prompt and no processing.
* **Receipt proof fails closed.** :func:`validate_result` verifies the exact
  terminal receipt line, the exact task binding and the enumerated submission
  against externally supplied facts *before* parsing any plan content; a
  missing, unknown, duplicated, non-terminal or uncheckable receipt, or an
  absent binding fact, holds with an explicit code. Exactly two grammars are
  admitted: the original bounded single token, and the exact native CAO team
  receipt (the literal ``CAO_TEAM_RESULT:`` prefix, one ASCII space, 16
  lowercase hex). The receipt is externally verified **source evidence**, never
  authority: a well-formed receipt never grants, dispatches or widens a plan.
* **Exactly one bounded, unambiguous tagged plan block.** Missing, duplicated,
  unbalanced, malformed, oversized or non-JSON output is refused; prose around
  the block (including echoed instructions) is never interpreted.
* **The plan stays data.** The extracted block is handed to
  ``decision_agent.validate_plan`` with the independently supplied context
  (including its ``source_known`` predicate), which performs the provenance,
  privacy, completion and prior-receipt cross-checks. The stage output is only
  an advisory ``PROPOSED`` or ``HELD`` envelope: it never carries authority,
  never dispatches, and creates no team, task, Registry claim, grant or queue.

Separation and scope
--------------------

Standard library only (``re`` plus the existing ``decision_agent`` module).
The module imports no runtime plugin, no egress module, no shell and no
execution entry point, reads no record and writes nothing.
"""

from __future__ import annotations

import re
from typing import Any

from . import decision_agent as da

__all__ = [
    "PROMPT_SCHEMA",
    "SYNTHETIC_PREFIX",
    "OPEN_TAG",
    "CLOSE_TAG",
    "MAX_PROMPT_BYTES",
    "MAX_PROMPT_REFS",
    "MAX_RESULT_BYTES",
    "MAX_SUBMISSION_IDS",
    "prepare_read_prompt",
    "validate_result",
]

#: The bounded prompt envelope schema. It is advisory data, never a plan.
PROMPT_SCHEMA = "cao.decision.read_prompt.v1"
#: The explicit marker prefix every synthetic submission id must carry.
SYNTHETIC_PREFIX = "synthetic-"
#: The single tagged result block the model may emit, exactly one per output.
OPEN_TAG = "<cao-plan-result"
CLOSE_TAG = "</cao-plan-result>"

#: Tight, explicit bounds. Anything exceeding them is refused.
MAX_PROMPT_BYTES = 16 * 1024
MAX_PROMPT_REFS = 16
MAX_RESULT_BYTES = da.MAX_PLAN_BYTES
MAX_SUBMISSION_IDS = 32

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
#: The original legacy receipt grammar: exactly one bounded single token.
_RECEIPT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")
#: The exact native CAO team terminal receipt grammar: the literal result
#: prefix, one ASCII space, then exactly 16 lowercase hex characters. This is
#: the exact form ``cao.team._receipt_for`` emits; no other multiword token, no
#: capital hex, no variable length and no extra padding is admitted here.
_NATIVE_RECEIPT_RE = re.compile(r"CAO_TEAM_RESULT: [0-9a-f]{16}\Z")
_USERINFO_RE = re.compile(r"://[^/@\s]+:[^/@\s]+@")
_OPEN_EXACT = re.compile(
    re.escape(OPEN_TAG)
    + r' submission="(?P<submission>[A-Za-z0-9][A-Za-z0-9_.-]{0,63})"'
    + r' task="(?P<task>[A-Za-z0-9][A-Za-z0-9_.-]{0,63})">'
)
_CLOSE_EXACT = re.compile(re.escape(CLOSE_TAG))


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------


def _prompt_envelope(
    status: str, code: str | None, reason: str | None, **extra: Any
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": PROMPT_SCHEMA,
        "status": status,
        "code": code,
        "reason": reason,
        "prompt": None,
        "submission_id": None,
        "task_id": None,
        "route": None,
        "data_class": None,
        "source_refs": [],
        "authority": "none",
        "dispatch": False,
        "effects": [],
        "egress": "none",
        "advisory_only": True,
    }
    result.update(extra)
    return result


def _prompt_held(code: str, reason: str) -> dict[str, Any]:
    return _prompt_envelope("HELD", code, reason)


# ---------------------------------------------------------------------------
# Externally verified prompt facts (never model-asserted)
# ---------------------------------------------------------------------------


def _verified_refs(raw: Any, data_class: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise da.DecisionHeld(
            "field_invalid", "source_refs: expected a list of verified references"
        )
    if not raw:
        raise da.DecisionHeld(
            "missing_field",
            "source_refs: at least one verified reference is required",
        )
    if len(raw) > MAX_PROMPT_REFS:
        raise da.DecisionHeld(
            "field_invalid", f"source_refs: exceeds {MAX_PROMPT_REFS} entries"
        )
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        # The strict evidence schema refuses unknown fields (title, body,
        # credential, path smuggling) and enforces digest/head/cutoff/scope
        # token shapes, so nothing outside the enumerated facts can pass.
        ref = da._normalize_ref(item)
        if ref["location"] in seen:
            raise da.DecisionHeld(
                "duplicate_reference", "source_refs holds a duplicate location"
            )
        seen.add(ref["location"])
        # ponytail: credential screening covers URL userinfo only; other
        # secret shapes stay at the caller's evidence-boundary. Upgrade path:
        # a dedicated secret scanner before the evidence reaches this stage.
        if _USERINFO_RE.search(ref["location"]):
            raise da.DecisionHeld(
                "unsafe_reference",
                "a reference embeds credentials; nothing unsafe reaches the prompt",
            )
        if ref["verdict"] != da.POSITIVE:
            raise da.DecisionHeld(
                "source_stale",
                "a prompt is never built on stale, unknown or refused facts",
            )
        scope = ref["scope"]
        if scope["owner"] is None or scope["repository"] is None:
            raise da.DecisionHeld(
                "missing_field",
                "every prompt reference needs an explicit scope owner and repository",
            )
        if scope["data_class"] != data_class:
            raise da.DecisionHeld(
                "privacy_mismatch",
                "a source scope data class does not match the qualified data class; "
                "no prompt crosses an unqualified boundary",
            )
        refs.append(ref)
    return refs


def _checked_submission(submission_id: Any, enumeration: Any) -> str:
    if not isinstance(enumeration, (list, tuple)) or not enumeration:
        raise da.DecisionHeld(
            "submission_enumeration_unavailable",
            "an explicit enumeration of synthetic submission ids is required; an "
            "arbitrary id is never used",
        )
    if len(enumeration) > MAX_SUBMISSION_IDS:
        raise da.DecisionHeld(
            "field_invalid", f"submission_ids: exceeds {MAX_SUBMISSION_IDS} entries"
        )
    known: set[str] = set()
    for item in enumeration:
        if not isinstance(item, str) or not _ID_RE.fullmatch(item):
            raise da.DecisionHeld(
                "field_invalid", "submission_ids: expected bounded safe ids"
            )
        known.add(item)
    if not isinstance(submission_id, str) or not _ID_RE.fullmatch(submission_id):
        raise da.DecisionHeld(
            "field_invalid", "submission_id: expected a bounded safe id"
        )
    if submission_id not in known:
        raise da.DecisionHeld(
            "submission_not_enumerated",
            "the submission id is not in the explicit enumeration; an arbitrary "
            "id never reaches a prompt or a result check",
        )
    if not submission_id.startswith(SYNTHETIC_PREFIX):
        raise da.DecisionHeld(
            "submission_not_synthetic",
            "only explicitly enumerated synthetic submission ids are used; a "
            "real-looking id is refused",
        )
    return submission_id


def _checked_task(task_id: Any) -> str:
    if not isinstance(task_id, str) or not _ID_RE.fullmatch(task_id):
        raise da.DecisionHeld(
            "task_id_invalid",
            "task_id: an exact bounded task id is required; binding fails closed",
        )
    return task_id


def _require_standing_authorization(standing_authorization: Any) -> None:
    if not callable(standing_authorization):
        raise da.DecisionHeld(
            "standing_authorization_unavailable",
            "an independently supplied standing authorization predicate is "
            "required; a model never asserts its own authority",
        )
    try:
        granted = standing_authorization()
    except Exception as exc:  # noqa: BLE001 - fail closed on any predicate failure
        raise da.DecisionHeld(
            "standing_authorization_unverified",
            f"the standing authorization predicate failed ({type(exc).__name__})",
        ) from exc
    if granted is not True:
        raise da.DecisionHeld(
            "standing_authorization_absent",
            "no existing standing authorization holds; the prompt stays held",
        )


def _render_prompt(
    submission: str,
    task: str,
    route: str,
    data_class: str,
    refs: list[dict[str, Any]],
) -> str:
    lines = [
        "CAO advisory READ review (bounded).",
        f"Submission: {submission}",
        f"Task: {task}",
        f"Route: {route}",
        f"Data class: {data_class}",
        "Verified sources, read only these:",
    ]
    for ref in refs:
        scope = ref["scope"]
        lines.append(
            f"- location={ref['location']} digest={ref['digest']} head={ref['head']} "
            f"cutoff={ref['cutoff']} owner={scope['owner']} "
            f"repository={scope['repository']} data_class={scope['data_class']} "
            f"verdict={ref['verdict']}"
        )
    lines.extend(
        [
            f"Reply with exactly one tagged block holding one {da.PLAN_SCHEMA} object:",
            f'{OPEN_TAG} submission="{submission}" task="{task}">',
            "  {...plan fields...}",
            CLOSE_TAG,
            "The block is advisory data only: no authority, no dispatch, no file "
            "change, and no WRITE or reserved effect is requested or granted.",
            "Use only the verified sources above; any other source is a spoof and "
            "is refused.",
            "Do not invent approvals, evidence, provenance or completion claims.",
        ]
    )
    prompt = "\n".join(lines) + "\n"
    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise da.DecisionHeld(
            "oversize", "the built prompt exceeds the byte bound"
        )
    return prompt


# ---------------------------------------------------------------------------
# Result-output checks against externally supplied facts
# ---------------------------------------------------------------------------


def _output_text(output: Any) -> str:
    if isinstance(output, (bytes, bytearray)):
        try:
            output = output.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise da.DecisionHeld(
                "invalid_output", "the result output is not valid UTF-8"
            ) from exc
    if not isinstance(output, str):
        raise da.DecisionHeld(
            "invalid_output", "the result output must be text"
        )
    try:
        size = len(output.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise da.DecisionHeld(
            "invalid_output", "the result output is not valid UTF-8"
        ) from exc
    if size > MAX_RESULT_BYTES:
        raise da.DecisionHeld(
            "oversize", "the result output exceeds the byte bound"
        )
    return output


def _receipt_well_formed(receipt: Any) -> bool:
    """The exact legacy token or the exact native two-token string, or nothing.

    Externally verified source evidence only: an unverified, unknown,
    multiword-but-not-native, capital-hex, variable-length or padded value is
    never admitted.
    """

    if not isinstance(receipt, str) or receipt.lower() == "unknown":
        return False
    return bool(
        _RECEIPT_RE.fullmatch(receipt) or _NATIVE_RECEIPT_RE.fullmatch(receipt)
    )


def _check_receipt(text: str, receipt: Any) -> None:
    if not _receipt_well_formed(receipt):
        raise da.DecisionHeld(
            "receipt_unverifiable",
            "the terminal receipt proof is missing, unknown or not an exact "
            "bounded value; without externally supplied proof the check fails "
            "closed",
        )
    line_pattern = re.compile(r"[ \t]{0,4}" + re.escape(receipt) + r"\Z")
    lines = text.splitlines()
    matched = [
        index
        for index, line in enumerate(lines)
        if line_pattern.fullmatch(line)
    ]
    if not matched:
        raise da.DecisionHeld(
            "receipt_missing",
            "the output carries no exact terminal receipt line for this "
            "externally verified result",
        )
    if len(matched) > 1:
        raise da.DecisionHeld(
            "receipt_duplicate",
            "the output carries the receipt more than once; a duplicated or "
            "echoed receipt is refused",
        )
    last = next(
        (index for index in range(len(lines) - 1, -1, -1) if lines[index].strip()),
        None,
    )
    if matched[0] != last:
        raise da.DecisionHeld(
            "receipt_not_terminal",
            "the receipt must be the terminal line of the output; anything "
            "after it is refused",
        )


def _extract_block(text: str, submission: str, task: str) -> str:
    opens = [m.start() for m in re.finditer(re.escape(OPEN_TAG), text)]
    closes = [m.start() for m in re.finditer(re.escape(CLOSE_TAG), text)]
    if not opens and not closes:
        raise da.DecisionHeld(
            "plan_block_missing",
            "the output carries no tagged plan block; echoed prose or "
            "instructions are never a plan",
        )
    if len(opens) > 1 or len(closes) > 1:
        raise da.DecisionHeld(
            "duplicate_result",
            "the output must carry exactly one tagged plan block; duplicated "
            "blocks are refused",
        )
    open_match = _OPEN_EXACT.search(text)
    close_match = _CLOSE_EXACT.search(text)
    if open_match is None or close_match is None:
        raise da.DecisionHeld(
            "plan_block_malformed",
            "the tagged plan block is unbalanced or does not match the exact "
            "tag shape",
        )
    if close_match.start() < open_match.end():
        raise da.DecisionHeld(
            "plan_block_malformed",
            "the tagged plan block is not ordered: the close tag precedes the "
            "open tag",
        )
    if open_match.group("submission") != submission:
        raise da.DecisionHeld(
            "submission_mismatch",
            "the tagged result names a submission id this stage did not request",
        )
    if open_match.group("task") != task:
        raise da.DecisionHeld(
            "task_binding_mismatch",
            "the tagged result is not bound to the exact externally supplied "
            "task",
        )
    return text[open_match.end() : close_match.start()].strip()


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def prepare_read_prompt(
    *,
    source_refs: Any,
    route_qualification: Any,
    submission_id: Any,
    submission_ids: Any,
    task_id: Any,
    standing_authorization: Any,
    enabled: bool = False,
) -> dict[str, Any]:
    """Prepare one bounded advisory READ prompt, or hold with an explicit code.

    The prompt is built exclusively from the externally verified facts passed
    in: strict-evidence source references (location, digest, head, cutoff,
    scope, verdict) and an independently qualified route and data class. An
    explicit enumerated synthetic submission id, an exact task id and an
    existing independently supplied standing authorization are all required.
    The default ``enabled = False`` yields ``HELD`` and no prompt. The result
    is advisory data: it never carries authority and never dispatches.
    """

    if not enabled:
        return _prompt_held(
            "model_stage_disabled",
            "the decision stage is disabled by default; no prompt is built and "
            "the deterministic path stands",
        )
    try:
        if not isinstance(route_qualification, dict):
            raise da.DecisionHeld(
                "field_invalid", "route_qualification: expected an object"
            )
        qualification = da._normalize_qualification(route_qualification)
        route, data_class = da._require_qualified(qualification)
        refs = _verified_refs(source_refs, data_class)
        submission = _checked_submission(submission_id, submission_ids)
        task = _checked_task(task_id)
        _require_standing_authorization(standing_authorization)
        prompt = _render_prompt(submission, task, route, data_class, refs)
    except da.DecisionHeld as held:
        return _prompt_held(held.code, held.reason)
    return _prompt_envelope(
        "PROPOSED",
        None,
        None,
        prompt=prompt,
        submission_id=submission,
        task_id=task,
        route=route,
        data_class=data_class,
        source_refs=[
            {
                "location": ref["location"],
                "digest": ref["digest"],
                "head": ref["head"],
                "cutoff": ref["cutoff"],
            }
            for ref in refs
        ],
    )


def validate_result(
    *,
    output: Any,
    receipt: Any,
    task_id: Any,
    submission_id: Any,
    submission_ids: Any,
    context: Any,
    enabled: bool = False,
) -> dict[str, Any]:
    """Validate one terminal tagged result, or hold with an explicit code.

    The exact terminal receipt line, the exact task binding and the enumerated
    synthetic submission id are verified against externally supplied facts
    before any plan content is parsed; a missing, unknown, duplicated,
    non-terminal or uncheckable receipt, or an absent binding fact, fails
    closed with an explicit code. Exactly one bounded tagged block is
    extracted and handed to ``decision_agent.validate_plan`` together with the
    independently supplied typed context (including its ``source_known``
    predicate). The output is only an advisory ``PROPOSED`` or ``HELD``
    envelope: it never carries authority, never dispatches, and creates no
    team, task, Registry claim, grant or queue.
    """

    if not enabled:
        return da._held(
            "model_stage_disabled",
            "the decision stage is disabled by default; the result is not "
            "processed and the deterministic path stands",
        )
    try:
        text = _output_text(output)
        _check_receipt(text, receipt)
        task = _checked_task(task_id)
        submission = _checked_submission(submission_id, submission_ids)
        content = _extract_block(text, submission, task)
        if not isinstance(context, dict):
            raise da.DecisionHeld(
                "context_invalid",
                "context: an independently supplied evidence object is required",
            )
    except da.DecisionHeld as held:
        return da._held(held.code, held.reason)
    return da.validate_plan(content, context=context, enabled=True)
