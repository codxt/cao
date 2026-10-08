"""Reconcile INTENDED state against OBSERVED state.

A *subject* is a generic identity (a work item, a claim, a lane, a target).
The *intended* state is what a registry, a claim, or a caller declared should
be true. The *observed* state is what an injected probe actually reports.

The reconciler reports a qualified comparison per subject: ``AGREEMENT``,
``DISAGREEMENT`` or ``UNKNOWN``, each carrying its own
:class:`~cao.evidence.Evidence`. Two rules are load-bearing:

* A subject that was never observed is ``UNKNOWN``. Absence of observation is
  not observation of absence: it is never reported as "absent", "missing" or
  "gone", and never inferred from silence.
* Intended and observed disagreeing is a first-class result. It is reported,
  never reconciled away and never resolved by preferring one side.

Every probe goes through the injected callable. The reconciler never shells
out and never reads the live system implicitly; there is no auto-repair, no
retry ladder, no queue and no escalation. It reports disagreement; it does
not resolve it.

This module is runtime neutral and depends only on the evidence primitives.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .evidence import (
    Clock,
    Evidence,
    EvidenceSource,
    Verdict,
    build_evidence,
    fingerprint,
)

__all__ = [
    "DEFAULT_MAX_SUBJECTS",
    "CoordinatorError",
    "CoordinatorBoundError",
    "ProbeUnavailable",
    "ReconcileVerdict",
    "IntendedState",
    "ObservedState",
    "SubjectReconciliation",
    "Reconciliation",
    "reconcile",
]

# A hard cap on the number of intended subjects one reconciliation may carry.
# The reconciler iterates a caller-supplied finite collection; the cap bounds
# that work deterministically instead of trusting the input to be small.
DEFAULT_MAX_SUBJECTS = 10_000


class CoordinatorError(Exception):
    """Raised for a caller mistake that must fail closed."""

    code = "COORDINATOR_ERROR"


class CoordinatorBoundError(CoordinatorError):
    """The intended state exceeds the stated subject bound."""

    code = "COORDINATOR_BOUND"


class ProbeUnavailable(CoordinatorError):
    """Raised by a probe that explicitly could not observe a subject.

    Carrying an explicit reason keeps "I could not look" distinct from
    "nothing is there", which the reconciler reports as ``UNKNOWN``.
    """

    code = "PROBE_UNAVAILABLE"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ReconcileVerdict(str, Enum):
    """The outcome of comparing intended and observed state for one subject."""

    AGREEMENT = "AGREEMENT"
    DISAGREEMENT = "DISAGREEMENT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class IntendedState:
    """What a declaration says a subject's state should be."""

    subject: str
    state: str

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str) or not self.subject:
            raise CoordinatorError("intended subject must be a non-empty string")
        if not isinstance(self.state, str) or not self.state:
            raise CoordinatorError("intended state must be a non-empty string")


@dataclass(frozen=True)
class ObservedState:
    """What a probe reports a subject's state actually is."""

    subject: str
    state: str


@dataclass(frozen=True)
class SubjectReconciliation:
    """The qualified comparison for a single subject."""

    subject: str
    verdict: ReconcileVerdict
    intended_state: str | None
    observed_state: str | None
    evidence: Evidence

    @property
    def agrees(self) -> bool:
        return self.verdict is ReconcileVerdict.AGREEMENT

    @property
    def disagrees(self) -> bool:
        return self.verdict is ReconcileVerdict.DISAGREEMENT

    @property
    def unknown(self) -> bool:
        return self.verdict is ReconcileVerdict.UNKNOWN

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "verdict": self.verdict.value,
            "intended_state": self.intended_state,
            "observed_state": self.observed_state,
            "evidence": self.evidence.as_dict(),
        }


@dataclass(frozen=True)
class Reconciliation:
    """A whole reconciliation, one qualified result per intended subject."""

    subjects: tuple[SubjectReconciliation, ...]

    def verdict_counts(self) -> dict[str, int]:
        counts = {verdict.value: 0 for verdict in ReconcileVerdict}
        for subject in self.subjects:
            counts[subject.verdict.value] += 1
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "subjects": [subject.as_dict() for subject in self.subjects],
            "counts": self.verdict_counts(),
        }


def _evidence(
    source: EvidenceSource,
    verdict: Verdict,
    *,
    reason: str,
    clock: Clock | None,
    value: Any = None,
) -> Evidence:
    return build_evidence(
        source,
        verdict,
        reason=reason,
        fingerprint_value=fingerprint(value) if value is not None else None,
        clock=clock,
    )


def reconcile(
    intended: Iterable[IntendedState],
    probe: Callable[[str], ObservedState | None],
    *,
    clock: Clock | None = None,
    source: EvidenceSource = EvidenceSource.INTERNAL,
    max_subjects: int = DEFAULT_MAX_SUBJECTS,
) -> Reconciliation:
    """Reconcile ``intended`` against observations made by ``probe``.

    ``probe(subject)`` returns an :class:`ObservedState` when the subject was
    observed, ``None`` when it was not, and may raise :class:`ProbeUnavailable`
    to say so explicitly. Any other exception is caught and degrades to
    ``UNKNOWN`` rather than propagating or producing a positive result.

    ``max_subjects`` is a hard cap: exceeding it raises
    :class:`CoordinatorBoundError` before any probe is called.
    """

    if not callable(probe):
        raise CoordinatorError(
            f"probe must be callable, got {type(probe).__name__}"
        )
    if isinstance(max_subjects, bool) or not isinstance(max_subjects, int):
        raise CoordinatorError("max_subjects must be an integer")
    if max_subjects < 1:
        raise CoordinatorError("max_subjects must be at least 1")

    ordered: list[str] = []
    declared: dict[str, str] = {}
    conflicts: set[str] = set()
    for entry in intended:
        if not isinstance(entry, IntendedState):
            raise CoordinatorError(
                f"intended entries must be IntendedState, got "
                f"{type(entry).__name__}"
            )
        if len(ordered) >= max_subjects and entry.subject not in declared:
            raise CoordinatorBoundError(
                f"intended state exceeds max_subjects={max_subjects}"
            )
        existing = declared.get(entry.subject)
        if existing is None and entry.subject not in declared:
            declared[entry.subject] = entry.state
            ordered.append(entry.subject)
        elif existing != entry.state:
            # The same subject declared two different states cannot be
            # reconciled against an observation without preferring a side.
            conflicts.add(entry.subject)

    results: list[SubjectReconciliation] = []
    for subject in ordered:
        if subject in conflicts:
            results.append(
                SubjectReconciliation(
                    subject=subject,
                    verdict=ReconcileVerdict.UNKNOWN,
                    intended_state=None,
                    observed_state=None,
                    evidence=_evidence(
                        source,
                        Verdict.INVALID,
                        reason="contradictory intended state for subject",
                        clock=clock,
                        value={"subject": subject},
                    ),
                )
            )
            continue
        results.append(
            _reconcile_one(subject, declared[subject], probe, source, clock)
        )
    return Reconciliation(tuple(results))


def _reconcile_one(
    subject: str,
    intended_state: str,
    probe: Callable[[str], ObservedState | None],
    source: EvidenceSource,
    clock: Clock | None,
) -> SubjectReconciliation:
    try:
        observed = probe(subject)
    except ProbeUnavailable as exc:
        return SubjectReconciliation(
            subject=subject,
            verdict=ReconcileVerdict.UNKNOWN,
            intended_state=intended_state,
            observed_state=None,
            evidence=_evidence(
                source,
                Verdict.UNAVAILABLE,
                reason=f"probe could not observe subject: {exc.reason}",
                clock=clock,
                value={"subject": subject},
            ),
        )
    except Exception as exc:  # noqa: BLE001 - an unexpected probe failure is unknown
        return SubjectReconciliation(
            subject=subject,
            verdict=ReconcileVerdict.UNKNOWN,
            intended_state=intended_state,
            observed_state=None,
            evidence=_evidence(
                source,
                Verdict.UNKNOWN,
                reason=f"probe raised {type(exc).__name__}",
                clock=clock,
                value={"subject": subject},
            ),
        )

    if observed is None:
        # Absence of observation is not observation of absence.
        return SubjectReconciliation(
            subject=subject,
            verdict=ReconcileVerdict.UNKNOWN,
            intended_state=intended_state,
            observed_state=None,
            evidence=_evidence(
                source,
                Verdict.UNKNOWN,
                reason="probe did not observe subject",
                clock=clock,
                value={"subject": subject},
            ),
        )
    if type(observed) is not ObservedState or observed.subject != subject \
            or not isinstance(observed.state, str) or not observed.state:
        return SubjectReconciliation(
            subject=subject,
            verdict=ReconcileVerdict.UNKNOWN,
            intended_state=intended_state,
            observed_state=None,
            evidence=_evidence(
                source,
                Verdict.INVALID,
                reason="probe returned a malformed observation",
                clock=clock,
                value={"subject": subject},
            ),
        )

    value = {
        "subject": subject,
        "intended": intended_state,
        "observed": observed.state,
    }
    if observed.state == intended_state:
        return SubjectReconciliation(
            subject=subject,
            verdict=ReconcileVerdict.AGREEMENT,
            intended_state=intended_state,
            observed_state=observed.state,
            evidence=_evidence(
                source,
                Verdict.SUPPORTED,
                reason="observed state matches intended state",
                clock=clock,
                value=value,
            ),
        )
    return SubjectReconciliation(
        subject=subject,
        verdict=ReconcileVerdict.DISAGREEMENT,
        intended_state=intended_state,
        observed_state=observed.state,
        evidence=_evidence(
            source,
            Verdict.SUPPORTED,
            reason="observed state differs from intended state",
            clock=clock,
            value=value,
        ),
    )
