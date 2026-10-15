"""Bounded, deterministic Intake pipeline owned by the existing daemon (B8).

B8 is the first **safe end-to-end capability**: once a genuinely authenticated,
previously operator-approved standing authority envelope applies, the existing
daemon automatically processes bounded eligible admissions through deterministic
B3 triage, B6 READ-only eligibility and the B7 durable queue, and the existing
CAO team/review/Registry path then reconciles them. This module owns only the
**front half** of that pipeline -- bounded discovery, deterministic triage and
enqueue -- and it adds no scheduler, no service, no second ledger and no
network egress. The B7 queue (in :mod:`cao.intake_queue`) still owns
reconciliation; the daemon's single tick drives both.

Load-bearing rules:

* **The trusted scope, never untrusted text, chooses the checkout.** The
  repository and ref a candidate is queued against come from the validated
  standing-authority envelope, never from a submission body or an environment
  value. With no valid envelope the pipeline discovers nothing and enqueues
  nothing.
* **Deterministic and bounded.** Discovery is ordered by admission time then id.
  The grant scope is honored *before* the per-tick bound, so unrelated work can
  never silence authorized work behind a truncated prefix, and a rotating cursor
  (the daemon's durable tick count) makes discovery fair: every authorized
  submission is eventually processed even when earlier candidates keep being
  held. Triaging and enqueue are the existing deterministic APIs, so a replay is
  idempotent.
* **A grant verdict is resolved before anything is discovered.** A missing,
  refused, revoked, expired or future-dated standing-authority record discovers
  nothing and enqueues nothing; only an explicitly verified AND Supported
  envelope drives the pass.
* **Advisory triage, gated eligibility.** B3 may produce an advisory candidate;
  the actual classification is B6's, which rejects explicit, ambiguous and mixed
  reserved effects. The pipeline never treats a B3 disposition or a B4 draft as
  permission, and it never dispatches work itself.

Standard library only; no third-party dependency, no network and no credential.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import intake, intake_queue, intake_triage

__all__ = [
    "PIPELINE_SCHEMA",
    "DISCOVERY_LIMIT",
    "PIPELINE_STATUSES",
    "discover",
    "tick",
]

#: The read-only report of one bounded pipeline pass.
PIPELINE_SCHEMA = "cao.intake.pipeline.tick.v1"
#: The pipeline processes at most this many candidates per tick.
DISCOVERY_LIMIT = 32
#: Every terminal pipeline status.
PIPELINE_STATUSES = ("IDLE", "RUNNING", "HOLD", "DRY_RUN")


def discover(
    env: Mapping[str, str] | None = None, *, limit: int = DISCOVERY_LIMIT,
    allowed: set[str] | frozenset[str] | None = None,
) -> list[str]:
    """Bounded, deterministic, read-only list of admitted submission ids.

    Submissions are ordered by their durable admission time and then id, so the
    same store always yields the same bounded prefix; nothing here is random.
    ``allowed``, when given, is applied *before* ``limit`` (an in-scope
    submission behind unrelated work is never silenced by the per-tick bound)
    and ``limit=0`` yields an empty list.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("discovery limit must be a non-negative integer")
    views = intake.list_submissions(env)
    ordered = sorted(
        views,
        key=lambda view: (view.get("admitted_at") or "", view.get("submission_id") or ""),
    )
    ids: list[str] = []
    for view in ordered:
        submission_id = view.get("submission_id")
        if isinstance(submission_id, str):
            ids.append(submission_id)
    if allowed is not None:
        ids = [sid for sid in ids if sid in allowed]
    if limit == 0:
        return []
    return ids[:limit]


def _ordered_all(env: Mapping[str, str] | None) -> list[str]:
    """Every admitted id, ordered by admission time then id (unbounded read)."""

    ordered = sorted(
        intake.list_submissions(env),
        key=lambda view: (view.get("admitted_at") or "", view.get("submission_id") or ""),
    )
    return [
        view["submission_id"] for view in ordered
        if isinstance(view.get("submission_id"), str)
    ]


def _envelope_submissions(envelope: Mapping[str, Any]) -> set[str] | None:
    listed = envelope.get("submissions")
    if listed is None:
        return None
    return set(listed)


def _exception(code: str, subject: str, detail: str | None) -> dict[str, Any]:
    return {
        "code": code,
        "source": "intake_pipeline",
        "subject": subject[:64],
        "detail": detail[:200] if isinstance(detail, str) else None,
        "observed": None,
        "head": None,
    }


def tick(
    *,
    env: Mapping[str, str] | None = None,
    clock: Any = None,
    registry_db: str | None = None,
    authority: Mapping[str, Any] | None = None,
    dry_run: bool = False,
    limit: int = DISCOVERY_LIMIT,
    cursor: int = 0,
) -> dict[str, Any]:
    """Run exactly one bounded discovery/triage/enqueue pass.

    The existing daemon calls this each tick. The pass is a no-op that reports
    the reason unless an explicitly verified standing-authority record is
    supplied; it never creates a team, an agent or a Registry claim.
    ``cursor`` is the daemon's durable tick count: it rotates the processing
    window across eligible candidates, so every authorized submission is
    eventually processed even when earlier candidates keep being held.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("pipeline limit must be a non-negative integer")
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise ValueError("pipeline cursor must be a non-negative integer")
    report: dict[str, Any] = {
        "schema": PIPELINE_SCHEMA,
        "status": "IDLE",
        "code": None,
        "reason": None,
        "authority": "none",
        "repo": None,
        "discovered": 0,
        "processed": 0,
        "enqueued": 0,
        "submissions": [],
        "effects": [],
        "exceptions": [],
        "egress": "none",
    }
    record = authority if isinstance(authority, Mapping) else {}
    envelope = record.get("envelope")
    # Only an explicitly verified record drives the pass. A mapping without a
    # record or envelope, and any refused, revoked, expired, future-dated or
    # otherwise unverifiable record, discovers and enqueues nothing.
    has_envelope = isinstance(envelope, Mapping)
    if not (record.get("verdict") == "SUPPORTED" and has_envelope):
        report.update({
            "status": "HOLD",
            "code": str(record.get("code") or (
                "authority_unverified" if has_envelope
                else "authority_unavailable")),
            "reason": str(record.get("reason") or (
                "the standing authority is not verified, so no admission is "
                "discovered or queued" if has_envelope else
                "no validated standing authority envelope is present, so no "
                "admission is discovered or queued")),
        })
        report["exceptions"].append(_exception(
            report["code"], "pipeline", report["reason"]))
        return report

    scope = envelope.get("scope")
    source = envelope.get("source")
    if not isinstance(scope, Mapping) or not isinstance(source, Mapping):
        report.update({"status": "HOLD", "code": "authority_envelope",
                       "reason": "the standing authority envelope has no scope"})
        return report
    repo = scope.get("repo")
    ref = source.get("ref")
    report["authority"] = "standing"
    report["repo"] = repo

    try:
        ordered = _ordered_all(env)
    except (intake.IntakeError, OSError, ValueError) as exc:
        report.update({"status": "HOLD", "code": "discovery_unavailable",
                       "reason": f"admissions could not be discovered "
                                 f"({type(exc).__name__})"})
        return report

    in_scope = _envelope_submissions(envelope)
    # The grant scope is honored before the per-tick bound, so a bounded
    # prefix never silences authorized work behind unrelated submissions.
    candidates = [
        sid for sid in ordered
        if in_scope is None or sid in in_scope
    ]
    # Fair rotation: the daemon's durable tick count rotates the bounded
    # window, so authorized work eventually enters it even when earlier
    # candidates stay held. Same inputs and cursor always replay the same
    # order, so the pass stays deterministic and idempotent.
    if cursor and len(candidates) > 1:
        offset = cursor % len(candidates)
        candidates = candidates[offset:] + candidates[:offset]
    candidates = candidates[:limit]
    report["discovered"] = len(candidates)
    if not candidates:
        return report

    for submission_id in candidates:
        try:
            triage_result = intake_triage.triage(
                submission_id, env=env, clock=clock)
        except intake_triage.TriageError as exc:
            report["exceptions"].append(_exception(
                "triage_failed", submission_id, exc.code))
            continue
        packet = triage_result.get("packet") or {}
        disposition = (packet.get("disposition") or {}).get("value")
        report["processed"] += 1

        if dry_run:
            report["submissions"].append({
                "submission_id": submission_id,
                "triage": disposition,
                "queue_status": "DRY_RUN",
                "reason": "dry run: triage persisted only its advisory packet",
            })
            continue

        try:
            queued = intake_queue.enqueue(
                submission_id, repo=repo, source_ref=ref, env=env, clock=clock)
        except intake_queue.QueueError as exc:
            report["exceptions"].append(_exception(
                "enqueue_failed", submission_id, exc.code))
            report["submissions"].append({
                "submission_id": submission_id,
                "triage": disposition,
                "queue_status": "HELD",
                "reason": exc.code,
            })
            continue

        status = queued.get("status")
        report["submissions"].append({
            "submission_id": submission_id,
            "triage": disposition,
            "queue_status": status,
            "reason": queued.get("reason"),
        })
        if status in ("QUEUED", "DUPLICATE"):
            report["enqueued"] += 1
        report["effects"].append({
            "action": "intake_pipeline_enqueue",
            "submission_id": submission_id,
            "status": status,
        })
        if status not in ("QUEUED", "DUPLICATE"):
            report["exceptions"].append(_exception(
                str(status or "HELD"), submission_id, queued.get("reason")))

    report["status"] = "DRY_RUN" if dry_run else "RUNNING"
    return report
