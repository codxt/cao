"""Pure, bounded SLA and exception metrics for the R4 engineering queue.

A metric here is *derived*, never collected: each value is computed on demand
from records CAO already keeps -- the engineering checkpoint and tick result,
the team recovery decision receipt, and the durable product task record. No
second ledger, scheduler, database, watch, daemon or worker is introduced, and
nothing is persisted.

Nothing in this module sends, merges, deploys, releases or mutates anything. It
maps a fixed evidence snapshot to a bounded report so the existing daemon can
surface *exceptions* -- a lost terminal receipt, an exceeded review/CI/evaluation
wait, a spent retry budget, an unproven claim release, a refused source gate --
instead of emitting a routine per-tick message. A condition that cannot be
measured from evidence (a missing capture stamp) is reported ``None`` and never
rounded up to a breach; a breached SLA is never silently dropped.
"""
from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from typing import Any

SCHEMA = "cao.engineering.metrics.v1"
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

#: SLA ceilings, in seconds. Exceeding one raises an exception; it never
#: grants an action. They are deliberately generous -- the point is to stop an
#: unbounded silent wait, not to add a second release gate.
REVIEW_SLA_SECONDS = 6 * 3600
PR_CI_WAIT_SLA_SECONDS = 3 * 3600
EVALUATION_WAIT_SLA_SECONDS = 3 * 3600
OPERATOR_GRANT_SLA_SECONDS = 24 * 3600

# -- exception codes --------------------------------------------------------
TERMINAL_RECEIPT_LOSS = "TERMINAL_RECEIPT_LOSS"
REVIEW_LATENCY_EXCEEDED = "REVIEW_LATENCY_EXCEEDED"
RETRY_BUDGET_SPENT = "RETRY_BUDGET_SPENT"
CLAIM_RELEASE_UNPROVEN = "CLAIM_RELEASE_UNPROVEN"
PR_CI_WAIT_EXCEEDED = "PR_CI_WAIT_EXCEEDED"
EVALUATION_WAIT_EXCEEDED = "EVALUATION_WAIT_EXCEEDED"
OPERATOR_GRANT_WAIT_EXCEEDED = "OPERATOR_GRANT_WAIT_EXCEEDED"
GITHUB_RECONCILIATION_PENDING = "GITHUB_RECONCILIATION_PENDING"
SOURCE_GATE_HELD = "SOURCE_GATE_HELD"
PROVIDER_ROUTE_HELD = "PROVIDER_ROUTE_HELD"
HELD_EVIDENCE = "HELD_EVIDENCE"

CODES = (
    TERMINAL_RECEIPT_LOSS, REVIEW_LATENCY_EXCEEDED, RETRY_BUDGET_SPENT,
    CLAIM_RELEASE_UNPROVEN, PR_CI_WAIT_EXCEEDED, EVALUATION_WAIT_EXCEEDED,
    OPERATOR_GRANT_WAIT_EXCEEDED, GITHUB_RECONCILIATION_PENDING,
    SOURCE_GATE_HELD, PROVIDER_ROUTE_HELD, HELD_EVIDENCE,
)

#: An engineering status that is a *wait* for an external gate, and the exact
#: SLA it is measured against.
_WAIT = {
    "NEEDS_IMPROVEMENT": "evaluation",
    "CI_NOT_GREEN_FOR_HEAD": "ci",
    "INDEPENDENT_REVIEW_REQUIRED": "independent_review",
    "AWAITING_OPERATOR_APPROVAL": "operator_grant",
    "AWAITING_GITHUB_RECONCILIATION": "github_reconciliation",
}
_WAIT_SLA = {
    "evaluation": EVALUATION_WAIT_SLA_SECONDS,
    "ci": PR_CI_WAIT_SLA_SECONDS,
    "independent_review": REVIEW_SLA_SECONDS,
    "operator_grant": OPERATOR_GRANT_SLA_SECONDS,
    "github_reconciliation": 0,
}
_WAIT_EXCEPTION = {
    "evaluation": EVALUATION_WAIT_EXCEEDED,
    "ci": PR_CI_WAIT_EXCEEDED,
    "independent_review": REVIEW_LATENCY_EXCEEDED,
    "operator_grant": OPERATOR_GRANT_WAIT_EXCEEDED,
    "github_reconciliation": GITHUB_RECONCILIATION_PENDING,
}

#: An exact-head source mismatch is a refusal, not a wait: it is reported at
#: once and held, never polled as if it might clear itself.
_SOURCE_GATE = frozenset({
    "PR_SOURCE_MISMATCH", "SCOPE_MISMATCH", "BRANCH_RULES_NOT_VERIFIED",
    "SEPARATE_OPERATOR_IDENTITY_REQUIRED",
})

#: Team recovery classifications that are held for an operator, and the code
#: each reports. Only one bounded code per held decision.
_HELD_CLASSIFICATION = {
    "SECURITY_FAILURE": HELD_EVIDENCE,
    "REGISTRY_CONFLICT": HELD_EVIDENCE,
    "GIT_DRIFT": HELD_EVIDENCE,
    "WRITE_COMMITTED_BUT_UNVERIFIED": HELD_EVIDENCE,
    "INTERACTIVE_GATE": PROVIDER_ROUTE_HELD,
}

_RELEASED_OUTCOMES = ("complete", "abandon")


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return [row for row in value if isinstance(row, Mapping)]
    return []


def _stamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not STAMP.fullmatch(value):
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _age(now: Any, since: Any) -> float | None:
    """Seconds between two bounded stamps, or ``None`` when unprovable.

    A future or unreadable ``since`` is not a proven breach: the metric stays
    ``None`` instead of inventing an interval.
    """
    end, start = _stamp(now), _stamp(since)
    if end is None or start is None:
        return None
    seconds = (end - start).total_seconds()
    return seconds if seconds >= 0 else None


def _exception(code: str, *, source: str, subject: Any = None,
               detail: Any = None, observed: Any = None, head: Any = None) -> dict:
    return {
        "code": code, "source": source,
        "subject": subject if isinstance(subject, (str, int)) else None,
        "detail": detail if isinstance(detail, str) else None,
        "observed": observed if _stamp(observed) else None,
        "head": head if isinstance(head, str) else None,
    }


def engineering(result: Any) -> dict:
    """Derive the wait metric and exceptions for one engineering tick result.

    ``first_seen`` and ``observed_at`` are the two stamps the tick already
    carries; without ``first_seen`` the age is ``None`` and no SLA breach is
    claimed. An ``AWAITING_GITHUB_RECONCILIATION`` state is always an
    exception: the merge request's outcome is uncertain and must be inspected.
    """
    result = result if isinstance(result, Mapping) else {}
    status = result.get("status") if isinstance(result.get("status"), str) else None
    category = _WAIT.get(status)
    observed = result.get("observed_at")
    first_seen = result.get("first_seen")
    age = _age(observed, first_seen)
    head = result.get("head")
    subject = result.get("watch_id")
    metrics = {
        "status": status,
        "wait": category,
        "head": head if isinstance(head, str) else None,
        "evaluation_sha256": (result.get("evaluation_sha256")
                              if isinstance(result.get("evaluation_sha256"), str)
                              else None),
        "first_seen": first_seen if _stamp(first_seen) else None,
        "observed_at": observed if _stamp(observed) else None,
        "age_seconds": age,
        "sla_seconds": _WAIT_SLA.get(category) if category else None,
        "merge_requested": bool(result.get("merge_requested")),
    }
    exceptions: list[dict] = []
    if category is not None:
        sla = _WAIT_SLA[category]
        if category == "github_reconciliation" or (age is not None and age > sla):
            exceptions.append(_exception(
                _WAIT_EXCEPTION[category], source="engineering_checkpoint",
                subject=subject, detail=status, observed=observed, head=head))
    elif status in _SOURCE_GATE:
        exceptions.append(_exception(
            SOURCE_GATE_HELD, source="engineering_checkpoint", subject=subject,
            detail=status, observed=observed, head=head))
    return {"metrics": metrics, "exceptions": exceptions}


def team(decisions: Any) -> dict:
    """Derive retry/terminal metrics from team recovery decision receipts.

    A lost terminal receipt, a withheld provider route, a held Registry/Git/
    security refusal and a spent resume budget each report one bounded
    exception; an applied same-identity READ resume is counted as a retry
    decision. Nothing is started, resumed or released here.
    """
    retry_decisions = 0
    held_decisions = 0
    exceptions: list[dict] = []
    for row in _rows(decisions):
        classification = row.get("classification")
        decision = row.get("decision")
        subject = row.get("task_id")
        reason = row.get("reason")
        if decision == "RESUME_READ":
            retry_decisions += 1
            continue
        if decision in ("HOLD", "BLOCK"):
            held_decisions += 1
        if classification == "TERMINAL_RECEIPT_MISSING":
            exceptions.append(_exception(
                TERMINAL_RECEIPT_LOSS, source="team_recovery", subject=subject,
                detail=reason))
        elif classification == "READ_TRANSPORT" and decision == "HOLD" \
                and isinstance(reason, str) and "budget" in reason:
            exceptions.append(_exception(
                RETRY_BUDGET_SPENT, source="team_recovery", subject=subject,
                detail=reason))
        elif classification in _HELD_CLASSIFICATION:
            exceptions.append(_exception(
                _HELD_CLASSIFICATION[classification], source="team_recovery",
                subject=subject, detail=classification))
    return {
        "metrics": {"retry_decisions": retry_decisions,
                    "held_decisions": held_decisions},
        "exceptions": exceptions,
    }


def claims(tasks: Any) -> dict:
    """Prove the claim-release provenance of every durable task record.

    A ``complete`` task must carry a released claim with a bounded ``complete``
    outcome and the released source head; any other terminal release must carry
    a bounded outcome; a refused release is reported rather than hidden.
    """
    total = 0
    released = 0
    exceptions: list[dict] = []
    for task in _rows(tasks):
        total += 1
        subject = task.get("task_id")
        status = task.get("status")
        claim = task.get("claim")
        claim = claim if isinstance(claim, Mapping) else {}
        error = task.get("error")
        code = error.get("code") if isinstance(error, Mapping) else None
        if code == "TASK_CLAIM_RELEASE_REJECTED":
            exceptions.append(_exception(
                CLAIM_RELEASE_UNPROVEN, source="product_task", subject=subject,
                detail="the Registry refused claim release"))
            continue
        if status == "complete":
            released_head = claim.get("released_head")
            proven = (claim.get("released") is True
                      and claim.get("outcome") == "complete"
                      and isinstance(released_head, str) and bool(released_head))
            if not proven:
                exceptions.append(_exception(
                    CLAIM_RELEASE_UNPROVEN, source="product_task",
                    subject=subject, detail="complete without a proven release"))
        if claim.get("released") is True:
            released += 1
            if claim.get("outcome") not in _RELEASED_OUTCOMES:
                exceptions.append(_exception(
                    CLAIM_RELEASE_UNPROVEN, source="product_task",
                    subject=subject, detail="release without a bounded outcome"))
    return {
        "metrics": {"tasks": total, "released_claims": released},
        "exceptions": exceptions,
    }


def report(*, engineering_results: Any = (), team_decisions: Any = (),
           tasks: Any = (), exceptions: Any = (), now: Any = None) -> dict:
    """Combine every derived metric into one bounded exception report.

    ``engineering_results`` accepts tick-result-like mappings (re-derived
    here); ``exceptions`` accepts already-derived rows (for example from a
    queue report) verbatim. Identical exceptions are collapsed, and the report
    is ``OK`` only when no exception remains.
    """
    eng_metrics: list[dict] = []
    found: list[dict] = list(_rows(exceptions))
    for item in _rows(engineering_results):
        derived = engineering(item)
        eng_metrics.append(derived["metrics"])
        found.extend(derived["exceptions"])
    team_derived = team(team_decisions)
    claims_derived = claims(tasks)
    found.extend(team_derived["exceptions"])
    found.extend(claims_derived["exceptions"])
    unique: list[dict] = []
    seen: set[tuple] = set()
    for row in found:
        key = (row.get("code"), row.get("source"), row.get("subject"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return {
        "schema": SCHEMA,
        "status": "EXCEPTIONS" if unique else "OK",
        "generated_at": now if _stamp(now) else None,
        "metrics": {
            "engineering": eng_metrics,
            "team": team_derived["metrics"],
            "claims": claims_derived["metrics"],
        },
        "exceptions": unique,
    }


def annotate(result: Any) -> dict:
    """Attach the derived ``metrics`` and ``exceptions`` to a tick result.

    The input is copied; the tick result is never mutated in place.
    """
    out = dict(result) if isinstance(result, Mapping) else {}
    derived = engineering(out)
    out["metrics"] = derived["metrics"]
    out["exceptions"] = derived["exceptions"]
    return out


def summary(report_value: Any) -> dict:
    """A bounded, non-identifying view of an exception report for a daemon."""
    value = report_value if isinstance(report_value, Mapping) else {}
    exceptions = _rows(value.get("exceptions"))
    codes = sorted({row.get("code") for row in exceptions
                    if isinstance(row.get("code"), str)})
    return {
        "schema": SCHEMA,
        "status": value.get("status") if value.get("status") in ("OK", "EXCEPTIONS")
        else "UNKNOWN",
        "count": len(exceptions),
        "codes": codes,
    }
