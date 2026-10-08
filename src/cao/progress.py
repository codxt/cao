"""Durable progress detection for CAO.

Progress is a change in a *fingerprint* of observable writer state: the
writer's claimed scope, its current commit/revision (or content state), its
status, and any bounded numeric metrics. Activity alone is never progress: a
writer that runs for an hour without changing its fingerprint has not made
durable progress and must not wake a manager.

The fingerprint is derived deterministically from the state, so equal state
always yields the same fingerprint and any real change yields a different one.

Every boundary fails closed. A payload that is not the expected shape, a field
of the wrong type, or a non-finite/hostile numeric value yields ``UNKNOWN``
rather than a usable fingerprint. There is no default value and no
substitution; an unreadable state stays unknown.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .evidence import fingerprint

__all__ = [
    "ProgressError",
    "ProgressUnavailable",
    "WriterState",
    "ProgressVerdict",
    "ProgressAssessment",
    "fingerprint_writer_state",
    "assess_progress",
    "writer_event_identity",
]


class ProgressError(Exception):
    """Base class for progress detection failures."""

    code = "PROGRESS_ERROR"


class ProgressUnavailable(ProgressError):
    """Raised when writer state cannot yield a usable fingerprint."""

    code = "PROGRESS_UNAVAILABLE"


def _non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProgressUnavailable(
            f"{field} must be a non-empty string, got {type(value).__name__}"
        )
    return value


def _normalize_scope(value: Any) -> tuple[str, ...]:
    # A bare string is a sequence of characters; refuse it rather than
    # fingerprinting each character as a separate scope entry.
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ProgressUnavailable(
            "scope must be a sequence of non-empty strings, got "
            f"{type(value).__name__}"
        )
    return tuple(_non_empty_str(item, "scope entry") for item in value)


def _normalize_metrics(value: Any) -> tuple[tuple[str, float], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise ProgressUnavailable(
            "metrics must be a mapping of names to finite numbers, got "
            f"{type(value).__name__}"
        )
    items: list[tuple[str, float]] = []
    for key, raw in value.items():
        name = _non_empty_str(key, "metric name")
        # bool is an int subclass; treat it as hostile rather than as 0/1.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ProgressUnavailable(
                f"metric {name!r} must be a finite number, got "
                f"{type(raw).__name__}"
            )
        try:
            number = float(raw)
        except OverflowError as exc:
            # A JSON-reachable integer larger than a float can hold is hostile:
            # fail closed rather than letting the conversion escape.
            raise ProgressUnavailable(f"metric {name!r} is out of range") from exc
        if not math.isfinite(number):
            raise ProgressUnavailable(f"metric {name!r} is not finite")
        items.append((name, number))
    items.sort(key=lambda item: item[0])
    return tuple(items)


@dataclass(frozen=True)
class WriterState:
    """Validated, immutable observable writer state.

    Constructed only through :meth:`from_payload`, which snapshots the input so
    a caller mutating its mapping mid-read cannot tear the fingerprint.
    """

    scope: tuple[str, ...]
    revision: str
    status: str
    metrics: tuple[tuple[str, float], ...] = ()

    @classmethod
    def from_payload(cls, payload: Any) -> "WriterState":
        """Normalize untrusted state, or raise :class:`ProgressUnavailable`."""

        if not isinstance(payload, Mapping):
            raise ProgressUnavailable(
                f"writer state must be a mapping, got {type(payload).__name__}"
            )
        # Defensive snapshot: never fingerprint a mapping the caller may mutate.
        data = dict(payload)
        return cls(
            scope=_normalize_scope(data.get("scope")),
            revision=_non_empty_str(data.get("revision"), "revision"),
            status=_non_empty_str(data.get("status"), "status"),
            metrics=_normalize_metrics(data.get("metrics")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": list(self.scope),
            "revision": self.revision,
            "status": self.status,
            "metrics": {name: value for name, value in self.metrics},
        }


def fingerprint_writer_state(payload: Any) -> str:
    """Return a stable ``sha256:`` fingerprint of observable writer state.

    Accepts an already-validated :class:`WriterState` or a raw payload. Raises
    :class:`ProgressUnavailable` when the state is malformed or contains a
    hostile value; callers must treat that as an unknown, not as a fingerprint.
    """

    state = payload if isinstance(payload, WriterState) else WriterState.from_payload(payload)
    try:
        return fingerprint(state.as_dict())
    except (TypeError, ValueError) as exc:  # pragma: no cover - validated above
        raise ProgressUnavailable(
            f"writer state is not fingerprintable: {exc}"
        ) from exc


class ProgressVerdict(str, Enum):
    """The outcome of comparing an observed fingerprint to the prior one."""

    PROGRESS = "PROGRESS"
    NO_PROGRESS = "NO_PROGRESS"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ProgressAssessment:
    """A qualified progress decision.

    ``UNKNOWN`` means the comparison could not be made (unusable payload or an
    unexpected previous value); it never counts as progress.
    """

    verdict: ProgressVerdict
    fingerprint: str | None = None
    previous_fingerprint: str | None = None
    reason: str | None = None

    @property
    def changed(self) -> bool:
        return self.verdict is ProgressVerdict.PROGRESS

    @property
    def usable(self) -> bool:
        return self.verdict is not ProgressVerdict.UNKNOWN


def assess_progress(
    previous_fingerprint: Any, payload: Any
) -> ProgressAssessment:
    """Decide whether ``payload`` is durable progress over the prior state.

    A first observation (``previous_fingerprint is None``) establishes a
    baseline and counts as progress. An unchanged fingerprint is explicitly not
    progress. A malformed payload, or a previous value of the wrong type,
    fails closed as ``UNKNOWN``.
    """

    if previous_fingerprint is not None and not isinstance(previous_fingerprint, str):
        return ProgressAssessment(
            ProgressVerdict.UNKNOWN,
            reason=(
                "previous fingerprint must be a string or None, got "
                f"{type(previous_fingerprint).__name__}"
            ),
        )
    try:
        current = fingerprint_writer_state(payload)
    except ProgressUnavailable as exc:
        return ProgressAssessment(
            ProgressVerdict.UNKNOWN,
            previous_fingerprint=previous_fingerprint,
            reason=str(exc),
        )
    if previous_fingerprint is None:
        return ProgressAssessment(
            ProgressVerdict.PROGRESS,
            fingerprint=current,
            reason="initial durable state",
        )
    if current == previous_fingerprint:
        return ProgressAssessment(
            ProgressVerdict.NO_PROGRESS,
            fingerprint=current,
            previous_fingerprint=previous_fingerprint,
            reason="fingerprint unchanged",
        )
    return ProgressAssessment(
        ProgressVerdict.PROGRESS,
        fingerprint=current,
        previous_fingerprint=previous_fingerprint,
        reason="fingerprint changed",
    )


def writer_event_identity(writer_id: Any, fingerprint_value: Any) -> str:
    """Return the stable identity of a writer's durable-state change event.

    The identity is scoped to the writer and the new fingerprint, so the same
    unchanged state can never produce a second event identity.
    """

    writer = _non_empty_str(writer_id, "writer_id")
    fp = _non_empty_str(fingerprint_value, "fingerprint")
    return fingerprint({"writer_id": writer, "fingerprint": fp})
