"""Pure, bounded classifier for CAO team recovery decisions (R3).

This module is deterministic and performs no I/O. It maps one fixed set of
durable evidence fields to exactly one recovery classification and decision.
It never starts, prompts, stops, releases or claims anything: the caller
applies a decision, and only a READ resume whose every gate this classifier
proves may be applied automatically.

Rules this module encodes:

* Unknown, missing or contradictory evidence always yields ``HOLD`` (or
  ``BLOCK`` for a security/denial failure) with a reason and a next action;
  nothing is rounded up to a safe transition.
* A WRITE execution is never automatically replayed, retried, stopped,
  released or re-claimed.
* A recoverable READ transport failure may be resumed **only** on the same
  original execution identity, with the original agent/pane/prompt, a verified
  active READ Registry row, an available runtime, and an unchanged pinned Git
  HEAD and branch. It never issues a new prompt or starts a new agent, and at
  most one bounded resume per original execution identity is admitted.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

SCHEMA = "cao.team.recovery.v1"

#: How many per-task decisions a single team checkpoint retains.
MAX_DECISIONS = 64

# -- classifications --------------------------------------------------------

RUNNING_PROGRESS = "RUNNING_PROGRESS"
TERMINAL = "TERMINAL"
TERMINAL_RECEIPT_MISSING = "TERMINAL_RECEIPT_MISSING"
INTERACTIVE_GATE = "INTERACTIVE_GATE"
READ_TRANSPORT = "READ_TRANSPORT"
WRITE_UNVERIFIED = "WRITE_COMMITTED_BUT_UNVERIFIED"
REGISTRY_CONFLICT = "REGISTRY_CONFLICT"
GIT_DRIFT = "GIT_DRIFT"
SECURITY_FAILURE = "SECURITY_FAILURE"
PR_CI_PENDING = "PR_CI_PENDING"

CLASSIFICATIONS = (
    RUNNING_PROGRESS,
    TERMINAL,
    TERMINAL_RECEIPT_MISSING,
    INTERACTIVE_GATE,
    READ_TRANSPORT,
    WRITE_UNVERIFIED,
    REGISTRY_CONFLICT,
    GIT_DRIFT,
    SECURITY_FAILURE,
    PR_CI_PENDING,
)

# -- decisions --------------------------------------------------------------

CONTINUE = "CONTINUE"
RESUME_READ = "RESUME_READ"
HOLD = "HOLD"
BLOCK = "BLOCK"
OBSERVE = "OBSERVE"

DECISIONS = (CONTINUE, RESUME_READ, HOLD, BLOCK, OBSERVE)

#: A task whose durable slot reached one of these can no longer change.
_TERMINAL_SLOT_STATUSES = ("complete", "stopped")

_RECEIPT_ERRORS = frozenset({
    "TEAM_RESULT_UNVERIFIED",
    "TEAM_DURABLE_RESULT_UNVERIFIED",
    "TASK_RELEASE_UNVERIFIED",
})

#: A failure of the read transport (not of the model answer) may be reclaimed
#: on the identical execution identity once.
_TRANSPORT_ERRORS = frozenset({
    "TEAM_RESULT_UNREADABLE",
    "TEAM_PROMPT_NOT_VERIFIED",
    "TEAM_READ_PANE_NOT_CLOSED",
    "TEAM_READ_TASK_RECORD_ABSENT",
    "TASK_STATUS_UNVERIFIED",
})

#: A provider/interactive gate that needs an explicit operator decision.
_INTERACTIVE_ERRORS = frozenset({
    "TEAM_AGENT_BLOCKED",
    "TEAM_PROMPT_INVALID",
    "TASK_REFUSED",
    "PRODUCT_TASK_UNAVAILABLE",
    "PRODUCT_TASK_AMBIGUOUS",
    "PROVIDER_DENIED",
    "PROVIDER_UNAVAILABLE",
    "MODEL_UNAVAILABLE",
    "TEAM_MODEL_NOT_ALLOWED",
    "TEAM_ROUTE_IDENTITY_MISMATCH",
    "TEAM_APPROVAL_REQUIRED",
    "TEAM_ADMISSION_CHANGED",
})

#: A platform/tool-layer refusal. It is reported and never bypassed.
_SECURITY_ERRORS = frozenset({
    "TEAM_TOOL_DENIED",
    "TEAM_PERMISSION_DENIED",
    "TASK_TOOL_DENIED",
    "TASK_PERMISSION_DENIED",
    "TASK_PLATFORM_TOOL_DENIAL",
    "TASK_SECURITY_FAILURE",
    "TOOL_REFUSED",
})

_SECURITY_MARKERS = (
    "TOOL_DENIED",
    "TOOL_REFUSAL",
    "TOOL_REFUSED",
    "PERMISSION",
    "SECURITY",
    "FORBIDDEN",
    "SANDBOX",
)

_WRITE_UNVERIFIED_ERRORS = frozenset({
    "TASK_WRITE_RESULT_UNVERIFIED",
    "TASK_WRITE_SCOPE_VIOLATION",
    "TASK_CLAIM_RELEASE_REJECTED",
})

_PR_CI_PENDING = "PENDING"

_NEXT = {
    RUNNING_PROGRESS: "none",
    TERMINAL: "none",
    TERMINAL_RECEIPT_MISSING: "operator_retry_read_after_stop",
    INTERACTIVE_GATE: "operator_route_or_permission_decision",
    READ_TRANSPORT: "resume_same_read_execution",
    WRITE_UNVERIFIED: "operator_verify_committed_head",
    REGISTRY_CONFLICT: "operator_reconcile_registry",
    GIT_DRIFT: "operator_review_head_drift",
    SECURITY_FAILURE: "operator_platform_refusal_inspection",
    PR_CI_PENDING: "await_exact_head_ci",
}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _head(value: Any) -> str | None:
    if isinstance(value, str) and len(value) == 40:
        return value
    return None


def _bool(value: Any) -> bool:
    return value is True


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _decision(*, task_id: str, classification: str, decision: str,
              reason: str, execution_id: str | None,
              evidence: Mapping[str, Any], auto_resume: bool = False,
              next_action: str | None = None) -> dict[str, Any]:
    claim_id = evidence.get("claim_id")
    if not isinstance(claim_id, int) or isinstance(claim_id, bool):
        claim_id = None
    source_head = _head(evidence.get("head")) or _head(evidence.get("pinned_head"))
    provenance = {
        "task_id": task_id,
        "phase": _text(evidence.get("phase")) or None,
        "status": _text(evidence.get("status")) or None,
        "source_head": source_head,
        "observed_head": _head(evidence.get("observed_head")),
        "claim_id": claim_id,
        "nonce_state": _text(evidence.get("receipt")) or "UNKNOWN",
        "timestamp": _text(evidence.get("now")) or None,
    }
    return {
        "schema": SCHEMA,
        "task_id": task_id,
        "classification": classification,
        "decision": decision,
        "auto_resume": bool(auto_resume),
        "reason": reason,
        "next_action": next_action or _NEXT.get(classification, "operator_inspection"),
        "execution_id": execution_id,
        "provenance": provenance,
    }


def fingerprint(decision: Mapping[str, Any]) -> str:
    """A stable idempotency fingerprint for one decision.

    The capture timestamp is excluded so a repeated tick over unchanged
    evidence reproduces the same fingerprint instead of rewriting the receipt.
    """

    payload = {
        key: value for key, value in decision.items()
        if key != "provenance"
    }
    payload["provenance"] = {
        key: value for key, value in (decision.get("provenance") or {}).items()
        if key != "timestamp"
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _claim_verified(evidence: Mapping[str, Any]) -> tuple[bool, str]:
    """Verify the durable claim and its live shared-Registry row agree exactly."""

    if _bool(evidence.get("registry_unreadable")):
        return False, "the shared Registry row could not be read"
    durable = _mapping(evidence.get("durable_claim"))
    if durable is None:
        return False, "the execution has no durable claim"
    if _bool(durable.get("released")):
        return False, "the durable claim was already released"
    claim_id = durable.get("claim_id")
    if not isinstance(claim_id, int) or isinstance(claim_id, bool):
        return False, "the durable claim has no claim id"
    row = _mapping(evidence.get("registry_claim"))
    if row is None:
        return False, "no open shared Registry row matches the durable claim"
    if _bool(row.get("released")):
        return False, "the shared Registry row is already released"
    if row.get("claim_id") != claim_id:
        return False, "the shared Registry claim id does not match"
    if str(row.get("mode") or "").upper() != "READ":
        return False, "the shared Registry claim is not a READ claim"
    if str(durable.get("mode") or "").upper() != "READ":
        return False, "the durable claim is not a READ claim"
    expected_owner = evidence.get("expected_owner")
    if isinstance(expected_owner, str) and row.get("owner") != expected_owner:
        return False, "the shared Registry owner does not match"
    for key in ("run_id", "work_key"):
        expected = evidence.get("expected_" + key)
        if isinstance(expected, str) and row.get(key) != expected:
            return False, f"the shared Registry {key} does not match"
    return True, ""


def _git_verified(evidence: Mapping[str, Any]) -> tuple[bool, str]:
    """Verify the pinned HEAD and branch are unchanged and both readable."""

    pinned_head = _head(evidence.get("pinned_head"))
    accepted_head = _head(evidence.get("head"))
    observed_head = _head(evidence.get("observed_head"))
    if pinned_head is None or observed_head is None:
        return False, "the checkout HEAD could not be verified against its pin"
    if accepted_head is not None and accepted_head != observed_head:
        return False, "the checkout HEAD no longer matches the accepted revision"
    if observed_head != pinned_head:
        return False, "the checkout HEAD drifted from its dispatch pin"
    pinned_branch = evidence.get("pinned_branch")
    observed_branch = evidence.get("observed_branch")
    if not isinstance(pinned_branch, str) or not isinstance(observed_branch, str):
        return False, "the checkout branch could not be verified against its pin"
    if observed_branch != pinned_branch:
        return False, "the checkout branch drifted from its dispatch pin"
    return True, ""


def classify(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Classify one stable snapshot of durable team recovery evidence.

    Every field is optional. A missing field is treated as unproven and can
    never upgrade a decision; an unknown/contradictory snapshot yields HOLD.
    """

    e: Mapping[str, Any] = evidence if isinstance(evidence, Mapping) else {}
    task_id = _text(e.get("task_id"))
    mode = _text(e.get("mode")).upper()
    status = _text(e.get("status")) or "unknown"
    error = _text(e.get("error")) or None
    execution_id = _text(e.get("execution_id")) or None
    receipt = _text(e.get("receipt")) or "UNKNOWN"

    def build(classification: str, decision: str, reason: str, *,
              auto_resume: bool = False) -> dict[str, Any]:
        return _decision(
            task_id=task_id, classification=classification, decision=decision,
            reason=reason, execution_id=execution_id, evidence=e,
            auto_resume=auto_resume)

    # 1. A task that already reached a terminal slot state needs no recovery.
    if status in _TERMINAL_SLOT_STATUSES:
        return build(TERMINAL, CONTINUE, f"task is already {status}")

    # 2. A platform/tool refusal is reported, never bypassed or retried.
    denied = _bool(e.get("denied")) or (
        error in _SECURITY_ERRORS
        or (error is not None and any(m in error.upper() for m in _SECURITY_MARKERS))
    )
    if denied:
        return build(SECURITY_FAILURE, BLOCK,
                     "a platform or tool-layer refusal was observed")

    # 3. A moving checkout is never traversed.
    pinned_head = _head(e.get("pinned_head"))
    observed_head = _head(e.get("observed_head"))
    accepted_head = _head(e.get("head"))
    branch_drift = (
        isinstance(e.get("pinned_branch"), str)
        and isinstance(e.get("observed_branch"), str)
        and e.get("observed_branch") != e.get("pinned_branch")
    )
    if ((pinned_head is not None and observed_head is not None
         and pinned_head != observed_head)
            or (accepted_head is not None and observed_head is not None
                and accepted_head != observed_head)
            or branch_drift):
        return build(GIT_DRIFT, HOLD,
                     "the checkout HEAD or branch moved from its pinned revision")

    # 4. An interactive provider gate that needs an explicit operator decision
    #    is reported before claim verification: it is a human-actionable gate,
    #    not a Registry fault.
    if error in _INTERACTIVE_ERRORS:
        return build(INTERACTIVE_GATE, HOLD,
                     "the provider or admission gate needs an operator decision")

    # 5. Registry evidence must be exact, and no foreign writer may hold the
    #    checkout the recovery would re-enter.
    if _bool(e.get("registry_unreadable")):
        return build(REGISTRY_CONFLICT, HOLD, "the shared Registry is unreadable")
    if _bool(e.get("checkout_conflict")):
        return build(REGISTRY_CONFLICT, HOLD,
                     "another active claim holds the same checkout")
    claim_ok, claim_reason = _claim_verified(e)
    if not claim_ok and mode == "READ" and error is not None:
        return build(REGISTRY_CONFLICT, HOLD, claim_reason)

    # 6. A WRITE execution is never replayed, retried, stopped or released.
    #    It is checked before the READ receipt class so a committed-but-
    #    unverified WRITE is never mistaken for a recoverable READ answer.
    if mode == "WRITE":
        if status == "running":
            return build(RUNNING_PROGRESS, CONTINUE, "the WRITE worker is running")
        if (error in _WRITE_UNVERIFIED_ERRORS or error in _RECEIPT_ERRORS
                or status in ("unknown", "blocked")):
            reason = ("the WRITE commit is committed but not verified"
                      if error in _WRITE_UNVERIFIED_ERRORS
                      else "an UNKNOWN WRITE execution is never replayed")
            return build(WRITE_UNVERIFIED, HOLD, reason)

    # 7. A terminal answer whose receipt is missing stays blocked.
    if status == "unknown" and error in _RECEIPT_ERRORS:
        return build(TERMINAL_RECEIPT_MISSING, HOLD,
                     "the terminal receipt is absent or unverified")

    # 8. A pending exact-head CI gate is observed, never merged.
    if e.get("pr_ci") == _PR_CI_PENDING:
        return build(PR_CI_PENDING, OBSERVE, "the exact-head PR CI is pending")

    # 9. A running task with observable progress is simply continued.
    if status == "running":
        return build(RUNNING_PROGRESS, CONTINUE, "the task is running")

    # 10. The only automatic transition: a boundedly recoverable READ transport
    #     failure on the SAME original execution identity.
    return _read_recovery(e, claimed=claim_ok, claim_reason=claim_reason,
                          execution_id=execution_id, build=build)


def _read_recovery(evidence: Mapping[str, Any], *, claimed: bool,
                   claim_reason: str, execution_id: str | None,
                   build: Any) -> dict[str, Any]:
    error = _text(evidence.get("error")) or None
    mode = _text(evidence.get("mode")).upper()
    phase = _text(evidence.get("phase"))

    if mode != "READ" or phase not in ("work", "review"):
        return build(READ_TRANSPORT, HOLD,
                     "only a READ work or review execution is resumable")
    if error not in _TRANSPORT_ERRORS:
        return build(READ_TRANSPORT, HOLD,
                     _text(evidence.get("error")) or "the failure class is unknown")
    if not execution_id:
        return build(READ_TRANSPORT, HOLD, "the original execution identity is absent")
    if not _bool(evidence.get("prompt_present")):
        return build(READ_TRANSPORT, HOLD, "the original prompt is not checkpointed")
    if not _bool(evidence.get("agent_checkpoint")):
        return build(READ_TRANSPORT, HOLD, "the original agent/pane checkpoint is absent")
    if not _bool(evidence.get("runtime_available")):
        return build(READ_TRANSPORT, HOLD, "the runtime is not available to observe")
    if not _bool(evidence.get("worker_present")):
        return build(READ_TRANSPORT, HOLD,
                     "the original worker is no longer reported by the runtime")
    if not claimed:
        return build(REGISTRY_CONFLICT, HOLD, claim_reason)
    if _int(evidence.get("prior_resumes")) >= 1:
        return build(READ_TRANSPORT, HOLD,
                     "the bounded resume budget for this identity is spent")
    ok, reason = _git_verified(evidence)
    if not ok:
        return build(GIT_DRIFT, HOLD, reason)
    if evidence.get("receipt") == "VERIFIED":
        return build(READ_TRANSPORT, HOLD, "a verified receipt already exists")
    return build(READ_TRANSPORT, RESUME_READ,
                 "the original READ execution is recoverable in place",
                 auto_resume=True)
