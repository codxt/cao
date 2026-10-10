"""Outward delivery with explicit uncertainty and fail-closed persistence.

Outward delivery is not the internal manager-wake relay. :class:`cao.relay.
ProgressRelay` records an event identity *before* the wake, so a crash between
the two cannot produce a duplicate manager callback. Outward delivery has the
opposite hazard: a crash can lose the *answer* without losing the *send*. A
transport may have accepted the artifact and then died before the caller learned
the receipt id.

This module therefore has three load-bearing rules:

* ``CONFIRMED`` is reached only from a positive sink acknowledgement that
  carries a receipt id. A transport that returns plausible data with
  ``positive=True`` but no receipt id is ``UNCERTAIN`` and is never confirmed.
* An ``UNCERTAIN`` (or crashed ``SENDING``) key is reconciled against the
  destination *by key* before it can be sent again. Reconciliation that cannot
  produce a result leaves the key ``UNCERTAIN``; it is never silently resent.
* The ledger is bounded and persisted through :mod:`cao.state`. A corrupt or
  oversized state raises rather than being read as an empty ledger.

The only destination in v0 is :class:`LocalFileSink`, which writes a local file
under a caller-supplied directory. There is no HTTP, socket, SMTP, webhook,
credential or environment path anywhere in this module.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from .evidence import fingerprint, utc_now
from .state import write_state

__all__ = [
    "DELIVERY_STATE_SCHEMA",
    "DeliveryError",
    "DeliveryStateError",
    "DeliveryCapacityError",
    "DeliveryMismatchError",
    "DeliveryState",
    "delivery_key",
    "SinkAck",
    "LocalFileSink",
    "DeliveryReceipt",
    "DeliveryLedger",
    "load_ledger",
    "deliver",
    "reconcile_delivery",
]

DELIVERY_STATE_SCHEMA = "cao.automation.delivery.v0"

Clock = Callable[[], Any]


class DeliveryError(Exception):
    """Base class for delivery failures."""

    code = "DELIVERY_ERROR"


class DeliveryStateError(DeliveryError):
    """Persisted delivery state is present but not usable, or a key is unknown."""

    code = "DELIVERY_STATE"


class DeliveryCapacityError(DeliveryError):
    """The bounded delivery ledger cannot accept another receipt."""

    code = "DELIVERY_CAPACITY"


class DeliveryMismatchError(DeliveryError):
    """A key was reused for a different destination identity."""

    code = "DELIVERY_MISMATCH"


class DeliveryState(str, Enum):
    """Where a delivery key currently stands.

    ``PREPARED`` and ``SENDING`` precede an answer; ``CONFIRMED`` and
    ``UNCERTAIN`` are the two possible answers (``UNCERTAIN`` is not a success);
    ``HELD`` is terminal until a fresh prepare.
    """

    PREPARED = "PREPARED"
    SENDING = "SENDING"
    CONFIRMED = "CONFIRMED"
    UNCERTAIN = "UNCERTAIN"
    HELD = "HELD"


def _non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise DeliveryError(
            f"{field} must be a non-empty string, got {type(value).__name__}"
        )
    return value


def delivery_key(source_id: str, destination_id: str, cycle_id: str) -> str:
    """Return the stable fingerprint that identifies one outward delivery.

    The key is scoped to the source, the destination and the cycle, so the same
    logical delivery always maps to the same artifact path and the same ledger
    entry across restarts.
    """

    return fingerprint(
        {
            "source_id": _non_empty_str(source_id, "source_id"),
            "destination_id": _non_empty_str(destination_id, "destination_id"),
            "cycle_id": _non_empty_str(cycle_id, "cycle_id"),
        }
    )


@dataclass(frozen=True)
class SinkAck:
    """A destination's answer to a send or a reconcile.

    ``positive`` alone is not enough: a receipt id (``ack_id``) is required
    before a delivery may be called confirmed.
    """

    ack_id: str | None
    positive: bool
    detail: str | None = None


def _confirmed_ack(ack: Any) -> bool:
    return (
        isinstance(ack, SinkAck)
        and ack.positive is True
        and isinstance(ack.ack_id, str)
        and bool(ack.ack_id)
    )


class LocalFileSink:
    """The only v0 destination: a local Markdown file under a directory.

    ``deliver`` writes ``<directory>/<key>.md`` atomically (temp file, fsync,
    ``os.replace``) and returns a positive acknowledgement whose receipt id is
    the key. ``reconcile`` reads that file back, so a crash that lost the answer
    can still be resolved without resending.
    """

    def __init__(self, directory: str | os.PathLike[str], *, clock: Clock | None = None) -> None:
        self._directory = Path(directory)
        self._clock = clock

    @property
    def directory(self) -> Path:
        return self._directory

    def _path(self, key: str) -> Path:
        key = _non_empty_str(key, "key")
        # The key names a file INSIDE the caller's directory. Without this a key
        # carrying a separator or an absolute path would write outside it while
        # the docstring still promised a file "under" the directory.
        if key in (".", "..") or "/" in key or "\\" in key or "\0" in key:
            raise DeliveryError(
                "delivery key must be a single safe filename component"
            )
        return self._directory / f"{key}.md"

    def deliver(self, key: str, title: str, body: str) -> SinkAck:
        if not isinstance(title, str) or not isinstance(body, str):
            raise DeliveryError("title and body must be strings")
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # An empty title lets a caller supply a body that already carries its
        # own heading, instead of producing two identical H1 lines.
        text = f"# {title}\n\n{body}\n" if title else f"{body}\n"
        _write_atomic(target, text)
        return SinkAck(ack_id=key, positive=True, detail="local file written")

    def reconcile(self, key: str) -> SinkAck | None:
        target = self._path(key)
        if not target.is_file():
            return None
        return SinkAck(ack_id=key, positive=True, detail="local file present")


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` durably: temp file, fsync, atomic replace."""

    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            raise
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


@dataclass(frozen=True)
class DeliveryReceipt:
    """The durable record of one outward delivery attempt."""

    key: str
    destination_id: str
    state: DeliveryState
    attempts: int
    receipt_id: str | None
    reason: str | None
    prepared_at: str
    sent_at: str | None
    confirmed_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "destination_id": self.destination_id,
            "state": self.state.value,
            "attempts": self.attempts,
            "receipt_id": self.receipt_id,
            "reason": self.reason,
            "prepared_at": self.prepared_at,
            "sent_at": self.sent_at,
            "confirmed_at": self.confirmed_at,
        }


_PENDING_STATES = (
    DeliveryState.PREPARED,
    DeliveryState.SENDING,
    DeliveryState.UNCERTAIN,
)


class DeliveryLedger:
    """A bounded, inspectable, persisted set of delivery receipts.

    The ledger is keyed by delivery key. Every mutation is explicit; a
    ``CONFIRMED`` or ``HELD`` receipt is never downgraded by an incidental
    operation. Persist with :meth:`save` (``cao.state``) and rebuild with
    :func:`load_ledger`.
    """

    def __init__(
        self,
        *,
        receipts: Any = None,
        max_receipts: int = 512,
        clock: Clock | None = None,
    ) -> None:
        if isinstance(max_receipts, bool) or not isinstance(max_receipts, int):
            raise DeliveryStateError("max_receipts must be an integer")
        if max_receipts < 1:
            raise DeliveryStateError("max_receipts must be at least 1")
        self._max_receipts = max_receipts
        self._clock = clock
        self._receipts: dict[str, DeliveryReceipt] = {}
        for receipt in receipts or ():
            if not isinstance(receipt, DeliveryReceipt):
                raise DeliveryStateError(
                    f"receipts must be DeliveryReceipt, got {type(receipt).__name__}"
                )
            if receipt.key in self._receipts:
                raise DeliveryStateError(f"duplicate delivery key: {receipt.key}")
            self._receipts[receipt.key] = receipt
        if len(self._receipts) > max_receipts:
            raise DeliveryCapacityError(
                f"delivery ledger holds {len(self._receipts)} receipts, "
                f"exceeding max_receipts={max_receipts}"
            )

    @property
    def max_receipts(self) -> int:
        return self._max_receipts

    @property
    def clock(self) -> Clock | None:
        return self._clock

    def __len__(self) -> int:
        return len(self._receipts)

    def get(self, key: str) -> DeliveryReceipt | None:
        return self._receipts.get(key)

    def receipts(self) -> tuple[DeliveryReceipt, ...]:
        return tuple(self._receipts.values())

    def pending(self) -> tuple[DeliveryReceipt, ...]:
        """Receipts that are not yet resolved: PREPARED, SENDING, UNCERTAIN."""

        return tuple(
            receipt
            for receipt in self._receipts.values()
            if receipt.state in _PENDING_STATES
        )

    def _stamp(self, clock: Clock | None) -> str:
        return utc_now(clock if clock is not None else self._clock)

    def _require(self, key: str) -> DeliveryReceipt:
        receipt = self._receipts.get(key)
        if receipt is None:
            raise DeliveryStateError(f"no delivery receipt for key {key!r}")
        return receipt

    def prepare(
        self, key: str, destination_id: str, *, clock: Clock | None = None
    ) -> DeliveryReceipt:
        """Create a PREPARED receipt, or return the existing one.

        A ``HELD`` receipt is the one state a fresh prepare clears: holding is
        terminal until a deliberate new prepare. A ``CONFIRMED`` receipt is
        returned unchanged so a repeated delivery cannot create a second
        artifact. Reusing a key for a different destination is refused.
        """

        _non_empty_str(key, "key")
        _non_empty_str(destination_id, "destination_id")
        existing = self._receipts.get(key)
        if existing is not None:
            if existing.destination_id != destination_id:
                raise DeliveryMismatchError(
                    f"key {key!r} belongs to destination "
                    f"{existing.destination_id!r}, not {destination_id!r}"
                )
            if existing.state is DeliveryState.HELD:
                fresh = DeliveryReceipt(
                    key=key,
                    destination_id=destination_id,
                    state=DeliveryState.PREPARED,
                    attempts=0,
                    receipt_id=None,
                    reason=None,
                    prepared_at=self._stamp(clock),
                    sent_at=None,
                    confirmed_at=None,
                )
                self._receipts[key] = fresh
                return fresh
            return existing
        if len(self._receipts) >= self._max_receipts:
            # Bounded and fail closed: never evict a receipt that may recur.
            raise DeliveryCapacityError(
                f"delivery ledger is at capacity ({self._max_receipts} receipts); "
                "refusing a new delivery key"
            )
        receipt = DeliveryReceipt(
            key=key,
            destination_id=destination_id,
            state=DeliveryState.PREPARED,
            attempts=0,
            receipt_id=None,
            reason=None,
            prepared_at=self._stamp(clock),
            sent_at=None,
            confirmed_at=None,
        )
        self._receipts[key] = receipt
        return receipt

    def mark_sending(
        self, key: str, *, clock: Clock | None = None
    ) -> DeliveryReceipt:
        receipt = self._require(key)
        if receipt.state in (DeliveryState.CONFIRMED, DeliveryState.HELD):
            return receipt
        updated = replace(
            receipt,
            state=DeliveryState.SENDING,
            attempts=receipt.attempts + 1,
            sent_at=self._stamp(clock),
        )
        self._receipts[key] = updated
        return updated

    def confirm(
        self, key: str, ack_id: str, *, clock: Clock | None = None
    ) -> DeliveryReceipt:
        if not isinstance(ack_id, str) or not ack_id:
            raise DeliveryError("confirm requires a non-empty receipt id")
        receipt = self._require(key)
        if receipt.state in (DeliveryState.CONFIRMED, DeliveryState.HELD):
            return receipt
        updated = replace(
            receipt,
            state=DeliveryState.CONFIRMED,
            receipt_id=ack_id,
            reason=None,
            confirmed_at=self._stamp(clock),
        )
        self._receipts[key] = updated
        return updated

    def mark_uncertain(
        self, key: str, reason: str, *, clock: Clock | None = None
    ) -> DeliveryReceipt:
        receipt = self._require(key)
        if receipt.state in (DeliveryState.CONFIRMED, DeliveryState.HELD):
            return receipt
        updated = replace(receipt, state=DeliveryState.UNCERTAIN, reason=reason)
        self._receipts[key] = updated
        return updated

    def hold(self, key: str, reason: str, *, clock: Clock | None = None) -> DeliveryReceipt:
        receipt = self._require(key)
        if receipt.state is DeliveryState.CONFIRMED:
            # A confirmed delivery is the only positive fact in the ledger.
            # Holding must not be able to erase it and re-open the key for a
            # resend, so a hold on CONFIRMED is refused rather than applied.
            raise DeliveryError(
                "refusing to hold a CONFIRMED delivery: it is the only positive "
                "receipt in the ledger and holding it would re-open the key"
            )
        updated = replace(receipt, state=DeliveryState.HELD, reason=reason)
        self._receipts[key] = updated
        return updated

    def requeue(self, key: str, *, clock: Clock | None = None) -> DeliveryReceipt:
        """Return an ambiguous key to PREPARED after a reconcile proved it absent.

        Only ``SENDING`` and ``UNCERTAIN`` receipts move; a receipt that was
        never sent (``PREPARED``), or is already resolved, is returned as-is.
        """

        receipt = self._require(key)
        if receipt.state in (
            DeliveryState.PREPARED,
            DeliveryState.CONFIRMED,
            DeliveryState.HELD,
        ):
            return receipt
        updated = replace(
            receipt,
            state=DeliveryState.PREPARED,
            receipt_id=None,
            reason="reconciled absent at destination",
        )
        self._receipts[key] = updated
        return updated

    def as_state(self) -> dict[str, Any]:
        return {
            "schema": DELIVERY_STATE_SCHEMA,
            "receipts": [receipt.as_dict() for receipt in self._receipts.values()],
        }

    def save(self, path: str | os.PathLike[str]) -> dict[str, Any]:
        return write_state(path, self.as_state())


def _receipt_from_state(payload: Any) -> DeliveryReceipt:
    if not isinstance(payload, Mapping):
        raise DeliveryStateError(
            f"delivery receipt must be a mapping, got {type(payload).__name__}"
        )
    key = payload.get("key")
    if not isinstance(key, str) or not key:
        raise DeliveryStateError("delivery receipt 'key' must be a non-empty string")
    destination_id = payload.get("destination_id")
    if not isinstance(destination_id, str) or not destination_id:
        raise DeliveryStateError(
            "delivery receipt 'destination_id' must be a non-empty string"
        )
    raw_state = payload.get("state")
    try:
        state = DeliveryState(raw_state)
    except ValueError as exc:
        raise DeliveryStateError(
            f"delivery receipt 'state' is not a known state: {raw_state!r}"
        ) from exc
    attempts = payload.get("attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise DeliveryStateError(
            "delivery receipt 'attempts' must be a non-negative integer"
        )
    receipt_id = payload.get("receipt_id")
    if receipt_id is not None and (not isinstance(receipt_id, str) or not receipt_id):
        raise DeliveryStateError(
            "delivery receipt 'receipt_id' must be None or a non-empty string"
        )
    reason = payload.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise DeliveryStateError("delivery receipt 'reason' must be None or a string")
    prepared_at = payload.get("prepared_at")
    if not isinstance(prepared_at, str) or not prepared_at:
        raise DeliveryStateError(
            "delivery receipt 'prepared_at' must be a non-empty string"
        )
    sent_at = payload.get("sent_at")
    if sent_at is not None and (not isinstance(sent_at, str) or not sent_at):
        raise DeliveryStateError(
            "delivery receipt 'sent_at' must be None or a non-empty string"
        )
    confirmed_at = payload.get("confirmed_at")
    if confirmed_at is not None and (not isinstance(confirmed_at, str) or not confirmed_at):
        raise DeliveryStateError(
            "delivery receipt 'confirmed_at' must be None or a non-empty string"
        )
    return DeliveryReceipt(
        key=key,
        destination_id=destination_id,
        state=state,
        attempts=attempts,
        receipt_id=receipt_id,
        reason=reason,
        prepared_at=prepared_at,
        sent_at=sent_at,
        confirmed_at=confirmed_at,
    )


def load_ledger(payload: Any, *, max_receipts: int = 512) -> DeliveryLedger:
    """Rebuild a ledger from persisted state, or fail closed.

    A payload that is not the exact expected shape raises
    :class:`DeliveryStateError`; an oversized payload raises
    :class:`DeliveryCapacityError`. A malformed payload is never read as an
    empty ledger.
    """

    if not isinstance(payload, Mapping):
        raise DeliveryStateError(
            f"delivery state must be a mapping, got {type(payload).__name__}"
        )
    if payload.get("schema") != DELIVERY_STATE_SCHEMA:
        raise DeliveryStateError(
            f"unsupported delivery state schema: {payload.get('schema')!r}"
        )
    raw_receipts = payload.get("receipts")
    if not isinstance(raw_receipts, list):
        raise DeliveryStateError("delivery state 'receipts' must be a list")
    if isinstance(max_receipts, bool) or not isinstance(max_receipts, int):
        raise DeliveryStateError("max_receipts must be an integer")
    if max_receipts < 1:
        raise DeliveryStateError("max_receipts must be at least 1")
    if len(raw_receipts) > max_receipts:
        raise DeliveryCapacityError(
            f"delivery state holds {len(raw_receipts)} receipts, "
            f"exceeding max_receipts={max_receipts}"
        )
    receipts = [_receipt_from_state(item) for item in raw_receipts]
    return DeliveryLedger(receipts=receipts, max_receipts=max_receipts)


def _reconcile(
    ledger: DeliveryLedger,
    sink: Any,
    key: str,
    clock: Clock | None,
) -> tuple[bool | None, DeliveryReceipt]:
    """Ask the destination whether ``key`` is present.

    Returns ``(True, receipt)`` when the destination confirms the artifact,
    ``(False, receipt)`` when it definitively reports absence, and
    ``(None, receipt)`` when no usable answer could be obtained (in which case
    the receipt is left or moved to ``UNCERTAIN``).
    """

    reconcile = getattr(sink, "reconcile", None)
    if not callable(reconcile):
        return None, ledger.mark_uncertain(
            key, "sink provides no reconcile operation", clock=clock
        )
    try:
        ack = reconcile(key)
    except Exception as exc:  # noqa: BLE001 - no answer means no resend
        # A type-based diagnostic only: a reconcile error string can echo a
        # credential or a server response, and this reason is persisted. The
        # exception TYPE is a fixed, harmless qualifier.
        return None, ledger.mark_uncertain(
            key, f"reconcile failed ({type(exc).__name__})", clock=clock
        )
    if ack is None:
        return False, ledger._require(key)
    if _confirmed_ack(ack):
        return True, ledger.confirm(key, ack.ack_id, clock=clock)
    if not isinstance(ack, SinkAck):
        reason = f"reconcile returned {type(ack).__name__}, not SinkAck"
    elif not ack.positive:
        reason = "reconcile ack was not positive"
    else:
        reason = "reconcile ack carried no receipt id"
    return None, ledger.mark_uncertain(key, reason, clock=clock)


def deliver(
    ledger: DeliveryLedger,
    sink: Any,
    *,
    key: str,
    destination_id: str,
    title: str,
    body: str,
    clock: Clock | None = None,
) -> DeliveryReceipt:
    """Deliver one artifact to the destination, safely across crashes.

    A brand-new or ``PREPARED`` key is sent. A ``CONFIRMED`` or ``HELD`` key is
    returned unchanged. An ambiguous ``SENDING`` or ``UNCERTAIN`` key is
    reconciled by key first: a positive reconcile confirms it without a resend,
    a definitive absence allows exactly one resend, and an unusable reconcile
    leaves it ``UNCERTAIN``. The delivery is confirmed only from a positive
    acknowledgement that carries a receipt id.
    """

    if not isinstance(ledger, DeliveryLedger):
        raise DeliveryError(
            f"ledger must be a DeliveryLedger, got {type(ledger).__name__}"
        )
    _non_empty_str(key, "key")
    _non_empty_str(destination_id, "destination_id")
    if not isinstance(title, str) or not isinstance(body, str):
        raise DeliveryError("title and body must be strings")
    if not callable(getattr(sink, "deliver", None)):
        raise DeliveryError("sink must provide a callable deliver operation")

    receipt = ledger.get(key)
    if receipt is not None and receipt.destination_id != destination_id:
        raise DeliveryMismatchError(
            f"key {key!r} belongs to destination {receipt.destination_id!r}, "
            f"not {destination_id!r}"
        )
    if receipt is None:
        receipt = ledger.prepare(key, destination_id, clock=clock)

    if receipt.state in (DeliveryState.CONFIRMED, DeliveryState.HELD):
        return receipt

    if receipt.state in (DeliveryState.SENDING, DeliveryState.UNCERTAIN):
        found, receipt = _reconcile(ledger, sink, key, clock)
        if found is not False:
            # Confirmed, or no usable answer: never resend.
            return receipt
        # Definitive absence: the artifact is not at the destination, so a
        # single resend is safe and cannot duplicate it (same keyed path).

    receipt = ledger.mark_sending(key, clock=clock)
    try:
        ack = sink.deliver(key, title, body)
    except Exception as exc:  # noqa: BLE001 - a send that raised is uncertain
        # A type-based diagnostic only, for the same reason as reconcile: the
        # reason is persisted, and a transport error string may echo a token or
        # a raw response.
        return ledger.mark_uncertain(
            key, f"send failed ({type(exc).__name__})", clock=clock
        )
    if _confirmed_ack(ack):
        return ledger.confirm(key, ack.ack_id, clock=clock)
    if not isinstance(ack, SinkAck):
        reason = f"transport returned {type(ack).__name__}, not SinkAck"
    elif not ack.positive:
        reason = "transport ack was not positive"
    else:
        # Positive but no receipt id: plausible, unverifiable, not confirmed.
        reason = "transport ack carried no receipt id"
    return ledger.mark_uncertain(key, reason, clock=clock)


def reconcile_delivery(ledger: DeliveryLedger, sink: Any, *, key: str) -> DeliveryReceipt:
    """Resolve an ambiguous key by asking the destination, without sending.

    A ``CONFIRMED`` or ``HELD`` receipt is returned unchanged. A positive
    reconcile confirms the key (no resend). A definitive absence returns an
    ambiguous key to ``PREPARED`` so a later :func:`deliver` may send it once. A
    reconcile that cannot answer leaves the key ``UNCERTAIN``.
    """

    _non_empty_str(key, "key")
    receipt = ledger.get(key)
    if receipt is None:
        raise DeliveryStateError(f"no delivery receipt for key {key!r}")
    if receipt.state in (
        DeliveryState.CONFIRMED,
        DeliveryState.HELD,
        DeliveryState.PREPARED,
    ):
        return receipt
    found, receipt = _reconcile(ledger, sink, key, None)
    if found is not False:
        return receipt
    return ledger.requeue(key)
