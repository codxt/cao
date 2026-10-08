"""Lifecycle state machine for a single worker execution.

A worker execution passes through a small, explicit set of states. This module
owns only the *shape* of that progression: which state may follow which. It
does not launch processes, read policy, or talk to a runtime; those concerns
live in :mod:`cao.launcher`.

Two rules are load-bearing:

* Every transition is validated against an explicit table. An illegal
  transition raises :class:`IllegalTransition`; state is never silently
  snapped to a nearby state, never repaired and never guessed.
* Truth that cannot be established fails closed. Absent, unreadable,
  contradictory or incomplete evidence resolves to :data:`WorkerState.UNKNOWN`,
  which is a real, terminal state rather than a healthy guess.

There is no clock read at import time. Every transition is stamped through the
clock injected into :class:`Lifecycle`, so state and timestamps are both
explicit.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .evidence import Evidence, Verdict, utc_now

__all__ = [
    "WorkerState",
    "LifecycleError",
    "IllegalTransition",
    "Transition",
    "TERMINAL_STATES",
    "ALLOWED_TRANSITIONS",
    "is_positive_evidence",
    "Lifecycle",
]

Clock = Callable[[], object]


class WorkerState(str, Enum):
    """The states one worker execution genuinely passes through."""

    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"
    UNKNOWN = "unknown"


# Terminal states have no legal successor. UNKNOWN is terminal because the
# true state could not be established: any successor would be a guess, and
# guessing is exactly what this module refuses to do.
TERMINAL_STATES = frozenset(
    {
        WorkerState.COMPLETED,
        WorkerState.STOPPED,
        WorkerState.FAILED,
        WorkerState.UNKNOWN,
    }
)

# The complete transition table. Every state in WorkerState appears exactly
# once; terminal states map to the empty set. There are no self-transitions and
# no cycles, so a Lifecycle can advance only a bounded number of times.
ALLOWED_TRANSITIONS: dict[WorkerState, frozenset[WorkerState]] = {
    WorkerState.PENDING: frozenset(
        {WorkerState.STARTING, WorkerState.FAILED, WorkerState.UNKNOWN}
    ),
    WorkerState.STARTING: frozenset(
        {WorkerState.RUNNING, WorkerState.FAILED, WorkerState.UNKNOWN}
    ),
    WorkerState.RUNNING: frozenset(
        {
            WorkerState.STOPPING,
            WorkerState.COMPLETED,
            WorkerState.FAILED,
            WorkerState.UNKNOWN,
        }
    ),
    WorkerState.STOPPING: frozenset(
        {WorkerState.STOPPED, WorkerState.FAILED, WorkerState.UNKNOWN}
    ),
    WorkerState.COMPLETED: frozenset(),
    WorkerState.STOPPED: frozenset(),
    WorkerState.FAILED: frozenset(),
    WorkerState.UNKNOWN: frozenset(),
}


class LifecycleError(Exception):
    """Base class for lifecycle failures."""

    code = "LIFECYCLE_ERROR"


class IllegalTransition(LifecycleError):
    """Raised when a transition is not permitted by the transition table."""

    code = "ILLEGAL_TRANSITION"

    def __init__(self, from_state: object, to_state: object) -> None:
        self.from_state = from_state
        self.to_state = to_state
        origin = getattr(from_state, "value", from_state)
        target = getattr(to_state, "value", to_state)
        super().__init__(f"illegal transition: {origin} -> {target}")


@dataclass(frozen=True)
class Transition:
    """One recorded state change, with the time it was applied."""

    from_state: WorkerState | None
    to_state: WorkerState
    at: str
    reason: str | None = None


def is_positive_evidence(evidence: object) -> bool:
    """True only for complete, positively supported evidence.

    Anything else fails closed: an absent value, a non-``Evidence`` object, a
    verdict other than ``SUPPORTED`` (including ``UNKNOWN``, ``UNAVAILABLE``,
    ``REFUSED`` and ``INVALID``), or a ``SUPPORTED`` verdict missing its
    capture time or fingerprint (incomplete/contradictory) is not positive.
    """

    return (
        isinstance(evidence, Evidence)
        and evidence.verdict is Verdict.SUPPORTED
        and evidence.capture_time is not None
        and evidence.fingerprint is not None
    )


class Lifecycle:
    """A validated state machine for one worker execution.

    ``initial`` must be a member of :class:`WorkerState`. Each successful
    transition is stamped by the injected ``clock`` and appended to history.
    """

    def __init__(
        self,
        initial: WorkerState = WorkerState.PENDING,
        *,
        clock: Clock | None = None,
        reason: str | None = None,
    ) -> None:
        if not isinstance(initial, WorkerState):
            raise LifecycleError(
                f"initial state must be a WorkerState, got "
                f"{type(initial).__name__}"
            )
        self._clock = clock
        self._state = initial
        self._history: tuple[Transition, ...] = (
            Transition(
                from_state=None,
                to_state=initial,
                at=utc_now(clock),
                reason=reason,
            ),
        )

    @property
    def state(self) -> WorkerState:
        return self._state

    @property
    def history(self) -> tuple[Transition, ...]:
        return self._history

    @property
    def is_terminal(self) -> bool:
        return self._state in TERMINAL_STATES

    def advance(
        self,
        to_state: WorkerState,
        *,
        reason: str | None = None,
    ) -> WorkerState:
        """Move to ``to_state`` if the table permits it.

        Raises :class:`IllegalTransition` for an unknown target, a
        self-transition, a transition out of a terminal state, or any target
        not listed as a legal successor of the current state. State is left
        untouched on failure.
        """

        if not isinstance(to_state, WorkerState):
            raise IllegalTransition(self._state, to_state)
        if to_state not in ALLOWED_TRANSITIONS[self._state]:
            raise IllegalTransition(self._state, to_state)
        self._history = self._history + (
            Transition(
                from_state=self._state,
                to_state=to_state,
                at=utc_now(self._clock),
                reason=reason,
            ),
        )
        self._state = to_state
        return self._state

    def resolve(
        self,
        evidence: object,
        intended: WorkerState,
        *,
        reason: str | None = None,
    ) -> WorkerState:
        """Apply an evidence-qualified transition, failing closed.

        With positive evidence the machine advances to ``intended`` (which
        must itself be a legal successor, or :class:`IllegalTransition` is
        raised). With absent, non-supported or incomplete evidence it advances
        to :data:`WorkerState.UNKNOWN` instead, recording ``reason``.
        """

        if not isinstance(intended, WorkerState):
            raise LifecycleError(
                f"intended state must be a WorkerState, got "
                f"{type(intended).__name__}"
            )
        if not is_positive_evidence(evidence):
            observed = getattr(evidence, "verdict", None)
            qualifier = getattr(observed, "value", None) or type(evidence).__name__
            detail = reason or "evidence did not positively support a transition"
            return self.advance(
                WorkerState.UNKNOWN,
                reason=f"{detail} (evidence: {qualifier})",
            )
        return self.advance(intended, reason=reason)
