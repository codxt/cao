"""Manager-wake relay with durable deduplication.

A relay watches writers and forwards a *manager wake* when a writer makes
durable progress (see :mod:`cao.progress`). It never wakes a manager for an
unchanged fingerprint, and it never wakes twice for the same event identity.

Two bounds keep a relay from spinning:

* an explicit check interval and a bounded check budget, and
* a bounded retry budget for a state reader that fails.

When a bound is reached, or any input cannot be read, the relay returns an
explicit ``UNKNOWN`` or ``UNAVAILABLE`` result rather than looping or
substituting a healthy default.

Deduplication state is explicit and inspectable. It lives in a
:class:`RelayLedger` that can be persisted to a JSON state file and reloaded
after a crash or restart; a corrupt or malformed state file fails closed
instead of being read as an empty ledger.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .evidence import utc_now
from .progress import (
    ProgressUnavailable,
    fingerprint_writer_state,
    writer_event_identity,
)
from .state import StateError, read_state, write_state

__all__ = [
    "RELAY_STATE_SCHEMA",
    "RelayError",
    "RelayStateError",
    "RelayCapacityError",
    "RelayVerdict",
    "RelayBudget",
    "ManagerWake",
    "RelayResult",
    "RelayLedger",
    "load_ledger",
    "ProgressRelay",
]

RELAY_STATE_SCHEMA = "cao.relay.v0"


class RelayError(Exception):
    """Base class for relay failures."""

    code = "RELAY_ERROR"


class RelayStateError(RelayError):
    """The persisted deduplication state is present but not usable."""

    code = "RELAY_STATE"


class RelayCapacityError(RelayError):
    """The bounded deduplication ledger cannot accept another identity."""

    code = "RELAY_CAPACITY"


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


class RelayVerdict(str, Enum):
    """What a single relay check decided."""

    DELIVERED = "DELIVERED"
    NO_PROGRESS = "NO_PROGRESS"
    DUPLICATE = "DUPLICATE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    INVALID = "INVALID"


@dataclass(frozen=True)
class RelayBudget:
    """Explicit bounds on a relay run.

    ``interval_seconds`` is the pause between checks, ``max_checks`` bounds the
    total number of checks, and ``max_errors`` bounds consecutive reader
    failures before the relay gives up.
    """

    interval_seconds: float = 5.0
    max_checks: int = 60
    max_errors: int = 3

    def __post_init__(self) -> None:
        if isinstance(self.interval_seconds, bool) or not isinstance(
            self.interval_seconds, (int, float)
        ):
            raise RelayError("interval_seconds must be a number")
        interval = float(self.interval_seconds)
        if not math.isfinite(interval):
            raise RelayError("interval_seconds must be finite")
        if interval < 0:
            raise RelayError("interval_seconds must be non-negative")
        if isinstance(self.max_checks, bool) or not isinstance(self.max_checks, int):
            raise RelayError("max_checks must be an integer")
        if self.max_checks < 1:
            raise RelayError("max_checks must be at least 1")
        if isinstance(self.max_errors, bool) or not isinstance(self.max_errors, int):
            raise RelayError("max_errors must be an integer")
        if self.max_errors < 1:
            raise RelayError("max_errors must be at least 1")


@dataclass(frozen=True)
class ManagerWake:
    """One forwarded event telling a manager that durable progress happened."""

    identity: str
    writer_id: str
    fingerprint: str
    previous_fingerprint: str | None
    captured_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "writer_id": self.writer_id,
            "fingerprint": self.fingerprint,
            "previous_fingerprint": self.previous_fingerprint,
            "captured_at": self.captured_at,
        }


@dataclass(frozen=True)
class RelayResult:
    """The inspectable outcome of one relay check or run."""

    verdict: RelayVerdict
    identity: str | None = None
    fingerprint: str | None = None
    previous_fingerprint: str | None = None
    reason: str | None = None
    retryable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "identity": self.identity,
            "fingerprint": self.fingerprint,
            "previous_fingerprint": self.previous_fingerprint,
            "reason": self.reason,
            "retryable": self.retryable,
        }


class RelayLedger:
    """Explicit, inspectable deduplication state.

    ``delivered`` is an ordered, bounded set of delivered event identities;
    ``writers`` records the last seen fingerprint per writer so an unchanged
    state is recognized after a restart.
    """

    def __init__(
        self,
        *,
        delivered: Any = None,
        writers: Any = None,
        max_identities: int = 4096,
    ) -> None:
        if isinstance(max_identities, bool) or not isinstance(max_identities, int):
            raise RelayStateError("max_identities must be an integer")
        if max_identities < 1:
            raise RelayStateError("max_identities must be at least 1")
        self._max_identities = max_identities
        # Preserve order but drop repeats from untrusted input.
        unique_delivered: list[str] = list(dict.fromkeys(delivered or ()))
        # The finite-capacity rule is shared with the insertion path: an
        # oversized supplied or reloaded state is rejected, never trusted on
        # the way in, so it can never hold more identities than the maximum.
        if len(unique_delivered) > max_identities:
            raise RelayStateError(
                f"relay ledger holds {len(unique_delivered)} identities, "
                f"exceeding max_identities={max_identities}"
            )
        self._delivered: list[str] = unique_delivered
        self._seen: set[str] = set(self._delivered)
        self._writers: dict[str, str] = dict(writers or {})

    @property
    def max_identities(self) -> int:
        return self._max_identities

    @property
    def delivered(self) -> tuple[str, ...]:
        return tuple(self._delivered)

    @property
    def writers(self) -> dict[str, str]:
        return dict(self._writers)

    def seen(self, identity: str) -> bool:
        return identity in self._seen

    def last_fingerprint(self, writer_id: str) -> str | None:
        return self._writers.get(writer_id)

    def record(self, identity: str, writer_id: str, fingerprint_value: str) -> None:
        if identity not in self._seen:
            # Deduplication is absolute: once the bounded ledger is full, a new
            # identity is refused rather than evicting one that may recur. The
            # caller must fail closed instead of silently forgetting history.
            if len(self._delivered) >= self._max_identities:
                raise RelayCapacityError(
                    "deduplication ledger is at capacity "
                    f"({self._max_identities} identities); refusing a new identity"
                )
            self._delivered.append(identity)
            self._seen.add(identity)
        self._writers[writer_id] = fingerprint_value

    def as_state(self) -> dict[str, Any]:
        return {
            "schema": RELAY_STATE_SCHEMA,
            "delivered": list(self._delivered),
            "writers": dict(self._writers),
        }


def load_ledger(payload: Any, *, max_identities: int = 4096) -> RelayLedger:
    """Rebuild a ledger from a persisted state object, or fail closed.

    A payload that is not the exact expected shape raises
    :class:`RelayStateError`; it is never read as an empty ledger.
    """

    if not isinstance(payload, Mapping):
        raise RelayStateError(
            f"relay state must be a mapping, got {type(payload).__name__}"
        )
    if payload.get("schema") != RELAY_STATE_SCHEMA:
        raise RelayStateError(
            f"unsupported relay state schema: {payload.get('schema')!r}"
        )
    delivered = payload.get("delivered")
    if not isinstance(delivered, list) or any(
        not _non_empty_str(item) for item in delivered
    ):
        raise RelayStateError("relay state 'delivered' must be a list of non-empty strings")
    writers = payload.get("writers")
    if not isinstance(writers, Mapping):
        raise RelayStateError("relay state 'writers' must be a mapping")
    normalized: dict[str, str] = {}
    for writer_id, fingerprint_value in writers.items():
        if not _non_empty_str(writer_id) or not _non_empty_str(fingerprint_value):
            raise RelayStateError(
                "relay state 'writers' must map non-empty strings to non-empty strings"
            )
        normalized[writer_id] = fingerprint_value
    return RelayLedger(
        delivered=delivered, writers=normalized, max_identities=max_identities
    )


WakeSink = Callable[[ManagerWake], None]


class ProgressRelay:
    """Forward durable-progress events to a manager, deduplicated and bounded.

    ``sink`` receives a :class:`ManagerWake`; when it is ``None`` a progress
    event yields an explicit ``UNAVAILABLE`` instead of a silent no-op. With
    ``state_path`` set, deduplication state is persisted and reloaded so a
    restart does not re-wake for unchanged work.
    """

    def __init__(
        self,
        sink: WakeSink | None = None,
        state_path: str | None = None,
        *,
        budget: RelayBudget | None = None,
        ledger: RelayLedger | None = None,
        max_identities: int = 4096,
        clock: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if sink is not None and not callable(sink):
            raise RelayError(f"sink must be callable, got {type(sink).__name__}")
        self._sink = sink
        self._state_path = state_path
        self._budget = budget if budget is not None else RelayBudget()
        self._max_identities = max_identities
        self._clock = clock
        self._sleep = sleep if sleep is not None else time.sleep
        self._ledger = ledger

    @property
    def ledger(self) -> RelayLedger | None:
        return self._ledger

    # -- reading and persisting dedup state --------------------------------

    def _load(self) -> RelayLedger:
        if self._state_path is None:
            return RelayLedger(max_identities=self._max_identities)
        payload = read_state(self._state_path)
        if payload is None:
            return RelayLedger(max_identities=self._max_identities)
        return load_ledger(payload, max_identities=self._max_identities)

    def _ensure_ledger(self) -> RelayLedger:
        if self._ledger is None:
            self._ledger = self._load()
        return self._ledger

    def _persist(self, ledger: RelayLedger) -> None:
        if self._state_path is not None:
            write_state(self._state_path, ledger.as_state())

    # -- the check ---------------------------------------------------------

    def check(self, reader: Callable[[], Any], *, writer_id: str) -> RelayResult:
        """Read the writer's state, decide progress, and wake at most once.

        The state is read twice and the relay refuses to act if the two reads
        fingerprint differently, so a state that flips mid-read cannot be
        reported as false progress.
        """

        if not callable(reader):
            return RelayResult(
                RelayVerdict.INVALID,
                reason=f"reader must be callable, got {type(reader).__name__}",
            )
        if not _non_empty_str(writer_id):
            return RelayResult(
                RelayVerdict.INVALID, reason="writer_id must be a non-empty string"
            )
        try:
            ledger = self._ensure_ledger()
        except (StateError, RelayStateError) as exc:
            return RelayResult(
                RelayVerdict.UNKNOWN, reason=f"dedup state unreadable: {exc}"
            )

        try:
            first = reader()
        except Exception as exc:  # noqa: BLE001 - an unreadable reader is not progress
            return RelayResult(
                RelayVerdict.UNAVAILABLE,
                retryable=True,
                reason=f"writer state read failed: {type(exc).__name__}: {exc}",
            )
        try:
            first_fp = fingerprint_writer_state(first)
        except ProgressUnavailable as exc:
            return RelayResult(
                RelayVerdict.UNKNOWN, reason=f"writer state unusable: {exc}"
            )

        try:
            second = reader()
        except Exception as exc:  # noqa: BLE001 - fail closed, do not act on half a read
            return RelayResult(
                RelayVerdict.UNAVAILABLE,
                retryable=True,
                reason=f"writer state re-read failed: {type(exc).__name__}: {exc}",
            )
        try:
            second_fp = fingerprint_writer_state(second)
        except ProgressUnavailable as exc:
            return RelayResult(
                RelayVerdict.UNKNOWN,
                reason=f"writer state unusable on re-read: {exc}",
            )

        if first_fp != second_fp:
            return RelayResult(
                RelayVerdict.UNKNOWN,
                reason="fingerprint changed mid-read; refusing an unstable observation",
            )

        previous = ledger.last_fingerprint(writer_id)
        if previous is not None and previous == first_fp:
            return RelayResult(
                RelayVerdict.NO_PROGRESS,
                fingerprint=first_fp,
                previous_fingerprint=previous,
                reason="fingerprint unchanged",
            )

        identity = writer_event_identity(writer_id, first_fp)
        if ledger.seen(identity):
            # The fingerprint recurred after an intervening change. Record it so
            # the next unchanged check is NO_PROGRESS, but do not wake again.
            try:
                ledger.record(identity, writer_id, first_fp)
            except RelayCapacityError as exc:
                return RelayResult(
                    RelayVerdict.UNKNOWN,
                    identity=identity,
                    fingerprint=first_fp,
                    previous_fingerprint=previous,
                    reason=f"dedup ledger full: {exc}",
                )
            try:
                self._persist(ledger)
            except StateError as exc:
                return RelayResult(
                    RelayVerdict.UNKNOWN,
                    identity=identity,
                    fingerprint=first_fp,
                    previous_fingerprint=previous,
                    reason=f"cannot persist dedup state: {exc}",
                )
            return RelayResult(
                RelayVerdict.DUPLICATE,
                identity=identity,
                fingerprint=first_fp,
                previous_fingerprint=previous,
                reason="event identity already delivered",
            )

        if self._sink is None:
            return RelayResult(
                RelayVerdict.UNAVAILABLE,
                identity=identity,
                fingerprint=first_fp,
                previous_fingerprint=previous,
                reason="no manager wake sink is configured",
            )

        # Record the delivery before waking so a crash between the two cannot
        # produce a duplicate wake. Deduplication is absolute, so a full ledger
        # refuses the new identity before any wake is attempted.
        try:
            ledger.record(identity, writer_id, first_fp)
        except RelayCapacityError as exc:
            return RelayResult(
                RelayVerdict.UNKNOWN,
                identity=identity,
                fingerprint=first_fp,
                previous_fingerprint=previous,
                reason=f"dedup ledger full: {exc}",
            )
        try:
            self._persist(ledger)
        except StateError as exc:
            return RelayResult(
                RelayVerdict.UNKNOWN,
                identity=identity,
                fingerprint=first_fp,
                previous_fingerprint=previous,
                reason=f"cannot persist dedup state: {exc}",
            )

        wake = ManagerWake(
            identity=identity,
            writer_id=writer_id,
            fingerprint=first_fp,
            previous_fingerprint=previous,
            captured_at=utc_now(self._clock),
        )
        try:
            self._sink(wake)
        except Exception as exc:  # noqa: BLE001 - delivery may already have happened
            # An exception alone cannot authorize another wake: the sink may
            # have recorded the event before raising. Keep the recorded identity
            # so the same event can never wake a manager twice.
            return RelayResult(
                RelayVerdict.UNAVAILABLE,
                identity=identity,
                fingerprint=first_fp,
                previous_fingerprint=previous,
                reason=f"manager wake failed: {type(exc).__name__}: {exc}",
            )
        return RelayResult(
            RelayVerdict.DELIVERED,
            identity=identity,
            fingerprint=first_fp,
            previous_fingerprint=previous,
            reason="durable progress delivered",
        )

    # -- the bounded loop --------------------------------------------------

    def run(self, reader: Callable[[], Any], *, writer_id: str) -> RelayResult:
        """Check within the explicit bounds; never loop unbounded.

        Returns on the first delivered, duplicate, unknown or non-retryable
        result. An unchanged fingerprint (``NO_PROGRESS``) consumes the check
        budget; a retryable reader failure consumes the error budget. Either
        bound being reached returns an explicit ``UNKNOWN``/``UNAVAILABLE``.
        """

        if not callable(reader):
            return RelayResult(
                RelayVerdict.INVALID,
                reason=f"reader must be callable, got {type(reader).__name__}",
            )
        budget = self._budget
        checks = 0
        errors = 0
        last: RelayResult = RelayResult(RelayVerdict.UNKNOWN)
        while checks < budget.max_checks:
            checks += 1
            last = self.check(reader, writer_id=writer_id)
            if last.verdict in (
                RelayVerdict.DELIVERED,
                RelayVerdict.DUPLICATE,
                RelayVerdict.UNKNOWN,
                RelayVerdict.INVALID,
            ):
                return last
            if last.verdict is RelayVerdict.UNAVAILABLE:
                if not last.retryable:
                    return last
                errors += 1
                if errors >= budget.max_errors:
                    return last
            if checks >= budget.max_checks:
                break
            self._sleep(budget.interval_seconds)
        return RelayResult(
            RelayVerdict.UNKNOWN,
            fingerprint=last.fingerprint,
            reason="bounded check budget exhausted without durable progress",
        )
