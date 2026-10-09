"""Per-source reads and replay-safe bookmarks for CAO automations.

An automation source (a work registry, a code-host observer, a runtime status
projection, ...) reports through exactly one value: a :class:`SourceRead`. A
read is either a complete (``OK``) answer or an explicitly qualified
non-answer (``PARTIAL``/``UNAVAILABLE``/``UNKNOWN``/``INVALID``). A non-``OK``
read is never treated as empty and never advances the source's bookmark, so a
missing, unreadable or partial source can never be mistaken for "nothing
happened".

Bookmarks make re-runs safe. Each source keeps a monotone ``high_water``
timestamp and a bounded ordered set of already delivered item ids. Selection
drops delivered ids and re-reads a deliberate overlap window so two items that
share a timestamp are never lost between runs; the bookmark is advanced only
past items that were actually selected, so a bounded selection can never drop
an item it did not deliver.

This module is runtime-neutral: it names no concrete runtime, performs no I/O
of its own, and depends only on the standard library plus :mod:`cao.evidence`
and :mod:`cao.state`. Callers inject the source reader and, in tests, the
clock.
"""

from __future__ import annotations

import datetime as _datetime
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from .evidence import Evidence, EvidenceSource, Verdict, build_evidence, utc_now

__all__ = [
    "SOURCE_STATE_SCHEMA",
    "SourcesError",
    "SourcesStateError",
    "SourcesCapacityError",
    "SourceOutcome",
    "SourceItem",
    "SourceRead",
    "SourceReader",
    "Bookmark",
    "Bookmarks",
    "SourceCycle",
    "load_bookmarks",
    "select_new",
    "collect",
]

SOURCE_STATE_SCHEMA = "cao.automation.sources.v0"

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# The only outcomes that may carry items. Every other outcome is an explicit
# non-answer and must stay empty.
_ITEM_OUTCOMES = ("OK", "PARTIAL")


class SourcesError(Exception):
    """Base class for source and bookmark failures."""

    code = "SOURCES_ERROR"


class SourcesStateError(SourcesError):
    """Persisted bookmark state is present but not usable."""

    code = "SOURCES_STATE"


class SourcesCapacityError(SourcesError):
    """A bounded bookmark collection cannot accept another item id."""

    code = "SOURCES_CAPACITY"


def _is_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SourcesError(f"{field} must be a positive integer")
    return value


def _timestamp(value: Any, field: str) -> str:
    """Return ``value`` if it is an RFC 3339 UTC second-precision time."""

    if not _is_non_empty_str(value):
        raise SourcesError(f"{field} must be a non-empty timestamp string")
    try:
        _datetime.datetime.strptime(value, _TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise SourcesError(
            f"{field} must be an RFC 3339 UTC second-precision timestamp, "
            f"got {value!r}"
        ) from exc
    return value


def _parse_timestamp(value: str) -> _datetime.datetime:
    return _datetime.datetime.strptime(value, _TIMESTAMP_FORMAT)


class SourceOutcome(str, Enum):
    """How completely one source answered a read."""

    OK = "OK"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    INVALID = "INVALID"


@dataclass(frozen=True)
class SourceItem:
    """One item a source observed.

    ``item_id`` must be stable within its source so repeats collapse;
    ``observed_at`` is an RFC 3339 UTC second-precision timestamp, which makes
    ordering deterministic and equal timestamps comparable.
    """

    item_id: str
    observed_at: str
    title: str
    link: str | None = None
    attributes: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not _is_non_empty_str(self.item_id):
            raise SourcesError("item_id must be a non-empty string")
        _timestamp(self.observed_at, "observed_at")
        if not isinstance(self.title, str):
            raise SourcesError("title must be a string")
        if self.link is not None and not isinstance(self.link, str):
            raise SourcesError("link must be a string or None")
        if not isinstance(self.attributes, tuple):
            raise SourcesError("attributes must be a tuple of (str, str) pairs")
        for pair in self.attributes:
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not all(isinstance(part, str) for part in pair)
            ):
                raise SourcesError(
                    "attributes must be a tuple of (str, str) pairs"
                )


@dataclass(frozen=True)
class SourceRead:
    """The only way a source reports the result of one read.

    A positive (``SUPPORTED``) evidence verdict is reserved for a complete
    ``OK`` read; a non-``OK`` read is never reported as healthy. Only ``OK`` and
    ``PARTIAL`` may carry items, and a ``PARTIAL`` read still never counts as a
    complete answer.
    """

    source_id: str
    outcome: SourceOutcome
    items: tuple[SourceItem, ...]
    evidence: Evidence
    captured_at: str

    def __post_init__(self) -> None:
        if not _is_non_empty_str(self.source_id):
            raise SourcesError("source_id must be a non-empty string")
        if not isinstance(self.outcome, SourceOutcome):
            raise SourcesError("outcome must be a SourceOutcome")
        if not isinstance(self.evidence, Evidence):
            raise SourcesError("evidence must be an Evidence")
        if self.evidence.supported != (self.outcome is SourceOutcome.OK):
            raise SourcesError(
                "evidence SUPPORTED verdict must exactly match outcome OK"
            )
        if not isinstance(self.items, tuple):
            raise SourcesError("items must be a tuple")
        if self.outcome.value not in _ITEM_OUTCOMES and self.items:
            raise SourcesError(
                f"outcome {self.outcome.value} may not carry items"
            )
        _timestamp(self.captured_at, "captured_at")

    @property
    def complete(self) -> bool:
        """Whether this read is a complete (``OK``) answer."""

        return self.outcome is SourceOutcome.OK


SourceReader = Callable[[str, "str | None"], SourceRead]  # (source_id, since) -> read


@dataclass(frozen=True)
class Bookmark:
    """The last-successful-read position of one source.

    ``high_water`` is the maximum ``observed_at`` of the last ``OK`` read
    (empty until the first one); ``delivered`` is a bounded, ordered tuple of
    item ids already surfaced.
    """

    source_id: str
    high_water: str
    delivered: tuple[str, ...]
    updated_at: str

    def __post_init__(self) -> None:
        if not _is_non_empty_str(self.source_id):
            raise SourcesError("bookmark source_id must be a non-empty string")
        if self.high_water:
            _timestamp(self.high_water, "high_water")
        if not isinstance(self.delivered, tuple) or any(
            not _is_non_empty_str(item_id) for item_id in self.delivered
        ):
            raise SourcesError("delivered must be a tuple of non-empty strings")
        _timestamp(self.updated_at, "updated_at")


class Bookmarks:
    """Bounded, monotone per-source bookmarks.

    ``advance`` accepts only an ``OK`` read and advances the bookmark's
    ``high_water`` monotonically and its ``delivered`` set to include the
    read's item ids. Every other outcome leaves the bookmark unchanged: a
    missing or partial read never moves the cursor.
    """

    def __init__(
        self,
        entries: Iterable[Bookmark] | None = None,
        *,
        max_delivered_per_source: int = 512,
    ) -> None:
        self._max_delivered = _positive_int(
            max_delivered_per_source, "max_delivered_per_source"
        )
        self._entries: dict[str, Bookmark] = {}
        for entry in entries or ():
            if not isinstance(entry, Bookmark):
                raise SourcesStateError(
                    "bookmark entries must be Bookmark values, got "
                    f"{type(entry).__name__}"
                )
            if len(entry.delivered) > self._max_delivered:
                raise SourcesCapacityError(
                    f"bookmark for {entry.source_id!r} holds "
                    f"{len(entry.delivered)} delivered ids, exceeding "
                    f"max_delivered_per_source={self._max_delivered}"
                )
            self._entries[entry.source_id] = entry

    @property
    def max_delivered_per_source(self) -> int:
        return self._max_delivered

    def get(self, source_id: str) -> Bookmark | None:
        return self._entries.get(source_id)

    def seen(self, source_id: str, item_id: str) -> bool:
        entry = self._entries.get(source_id)
        if entry is None:
            return False
        return item_id in entry.delivered

    def advance(
        self,
        read: SourceRead,
        *,
        high_water_cap: str | None = None,
    ) -> bool:
        """Advance the bookmark for an ``OK`` read; a no-op otherwise.

        Returns ``True`` only when the bookmark mutates. ``high_water`` never
        lowers, duplicate item ids collapse, and a read that would push the
        bounded ``delivered`` set past its maximum raises
        :class:`SourcesCapacityError` without mutating anything.

        ``high_water_cap`` records the delivered ids while holding the cursor
        below a stated timestamp. That separation matters whenever delivery is
        partial: recording an id without moving the cursor stops the item being
        delivered twice, while refusing to move the cursor keeps an older,
        still-pending item selectable. Without the cap the caller must choose
        between those two, and both choices are wrong for somebody.
        """

        if not isinstance(read, SourceRead):
            raise SourcesError("advance expects a SourceRead")
        if read.outcome is not SourceOutcome.OK:
            return False
        if high_water_cap is not None:
            _timestamp(high_water_cap, "high_water_cap")
        existing = self._entries.get(read.source_id)
        delivered: list[str] = list(existing.delivered) if existing else []
        seen = set(delivered)
        for item in read.items:
            if item.item_id not in seen:
                delivered.append(item.item_id)
                seen.add(item.item_id)
        if len(delivered) > self._max_delivered:
            raise SourcesCapacityError(
                f"source {read.source_id!r} bookmark would hold "
                f"{len(delivered)} delivered ids, exceeding "
                f"max_delivered_per_source={self._max_delivered}"
            )
        high_water = existing.high_water if existing else ""
        for item in read.items:
            if item.observed_at > high_water:
                high_water = item.observed_at
        if high_water_cap is not None and high_water_cap < high_water:
            high_water = high_water_cap
        self._entries[read.source_id] = Bookmark(
            source_id=read.source_id,
            high_water=high_water,
            delivered=tuple(delivered),
            updated_at=read.captured_at,
        )
        return True

    def as_state(self) -> dict[str, Any]:
        """Return a deterministic, JSON-shaped snapshot for persistence."""

        return {
            "schema": SOURCE_STATE_SCHEMA,
            "bookmarks": {
                source_id: {
                    "source_id": entry.source_id,
                    "high_water": entry.high_water,
                    "delivered": list(entry.delivered),
                    "updated_at": entry.updated_at,
                }
                for source_id, entry in sorted(self._entries.items())
            },
        }


def load_bookmarks(payload: Any, *, max_delivered_per_source: int = 512) -> Bookmarks:
    """Rebuild bookmarks from persisted state, or fail closed.

    A payload that is not the exact expected shape raises
    :class:`SourcesStateError`; an over-capacity ``delivered`` list raises
    :class:`SourcesCapacityError`. Corrupt state is never read as empty.
    """

    _positive_int(max_delivered_per_source, "max_delivered_per_source")
    if not isinstance(payload, Mapping):
        raise SourcesStateError(
            f"bookmark state must be a mapping, got {type(payload).__name__}"
        )
    if payload.get("schema") != SOURCE_STATE_SCHEMA:
        raise SourcesStateError(
            f"unsupported bookmark state schema: {payload.get('schema')!r}"
        )
    raw = payload.get("bookmarks")
    if not isinstance(raw, Mapping):
        raise SourcesStateError("bookmark state 'bookmarks' must be a mapping")
    entries: list[Bookmark] = []
    for source_id, entry in raw.items():
        if not _is_non_empty_str(source_id):
            raise SourcesStateError("bookmark keys must be non-empty strings")
        if not isinstance(entry, Mapping):
            raise SourcesStateError(
                f"bookmark for {source_id!r} must be a mapping"
            )
        if entry.get("source_id") != source_id:
            raise SourcesStateError(
                f"bookmark key {source_id!r} does not match its source_id field"
            )
        delivered = entry.get("delivered")
        if not isinstance(delivered, list) or any(
            not _is_non_empty_str(item_id) for item_id in delivered
        ):
            raise SourcesStateError(
                f"bookmark {source_id!r} 'delivered' must be a list of "
                "non-empty strings"
            )
        if len(delivered) > max_delivered_per_source:
            raise SourcesCapacityError(
                f"bookmark {source_id!r} holds {len(delivered)} delivered ids, "
                f"exceeding max_delivered_per_source={max_delivered_per_source}"
            )
        try:
            entries.append(
                Bookmark(
                    source_id=source_id,
                    high_water=entry.get("high_water"),
                    delivered=tuple(delivered),
                    updated_at=entry.get("updated_at"),
                )
            )
        except SourcesError as exc:
            raise SourcesStateError(
                f"bookmark {source_id!r} is malformed: {exc}"
            ) from exc
    return Bookmarks(
        entries=entries, max_delivered_per_source=max_delivered_per_source
    )


def _overlap_since(
    bookmark: Bookmark | None, overlap_seconds: int
) -> str | None:
    """Return the inclusive ``since`` cutoff for a reader, or ``None``.

    The cutoff is ``high_water`` shifted back by the overlap window, so a
    reader that treats ``since`` as exclusive still returns items whose
    timestamp equals the previous high water.
    """

    if bookmark is None or not bookmark.high_water:
        return None
    moment = _parse_timestamp(bookmark.high_water) - _datetime.timedelta(
        seconds=overlap_seconds
    )
    return moment.strftime(_TIMESTAMP_FORMAT)


def select_new(
    read: SourceRead,
    bookmark: Bookmark | None,
    *,
    overlap_seconds: int = 60,
    max_items: int = 1000,
) -> tuple[SourceItem, ...]:
    """Select the not-yet-delivered items of a complete read.

    A non-``OK`` read yields nothing: an incomplete read is never a source of
    new items. Otherwise items already in the bookmark's ``delivered`` set are
    dropped, items older than the overlap window are ignored, the rest are
    ordered by ``(observed_at, item_id)``, and the result is bounded by
    ``max_items``.
    """

    if not isinstance(read, SourceRead):
        raise SourcesError("select_new expects a SourceRead")
    if not read.complete:
        return ()
    _positive_int(max_items, "max_items")
    if (
        isinstance(overlap_seconds, bool)
        or not isinstance(overlap_seconds, int)
        or overlap_seconds < 0
    ):
        raise SourcesError("overlap_seconds must be a non-negative integer")

    delivered = set(bookmark.delivered) if bookmark is not None else set()
    cutoff = _overlap_since(bookmark, overlap_seconds)
    candidates = [
        item
        for item in read.items
        if item.item_id not in delivered
        and (cutoff is None or item.observed_at >= cutoff)
    ]
    candidates.sort(key=lambda item: (item.observed_at, item.item_id))
    return tuple(candidates[:max_items])


@dataclass(frozen=True)
class SourceCycle:
    """The result of reading several sources once.

    ``reads`` is one entry per requested source in order; ``new_items`` pairs
    each new item with its source. A source that failed is preserved as a
    non-complete read rather than dropped, so ``unqualified`` names it.
    """

    reads: tuple[SourceRead, ...]
    new_items: tuple[tuple[str, SourceItem], ...]

    @property
    def covered(self) -> bool:
        """Whether every read in the cycle was complete (``OK``)."""

        return all(read.complete for read in self.reads)

    @property
    def unqualified(self) -> tuple[str, ...]:
        """The source ids that did not produce a complete read, in order."""

        return tuple(
            read.source_id for read in self.reads if not read.complete
        )


_VERDICT_BY_FAILURE = {
    SourceOutcome.UNAVAILABLE: Verdict.UNAVAILABLE,
    SourceOutcome.UNKNOWN: Verdict.UNKNOWN,
    SourceOutcome.INVALID: Verdict.INVALID,
}


def _failed_read(
    source_id: str,
    outcome: SourceOutcome,
    reason: str,
    clock: Callable[[], Any] | None,
) -> SourceRead:
    """Build a fail-closed read for a source that could not report."""

    verdict = _VERDICT_BY_FAILURE[outcome]
    captured_at = utc_now(clock)
    evidence = build_evidence(
        EvidenceSource.INTERNAL,
        verdict,
        reason=reason,
        capture_time=captured_at,
    )
    return SourceRead(
        source_id=source_id,
        outcome=outcome,
        items=(),
        evidence=evidence,
        captured_at=captured_at,
    )


def collect(
    reader: SourceReader,
    source_ids: Iterable[str],
    bookmarks: Bookmarks,
    *,
    overlap_seconds: int = 60,
    max_items_per_source: int = 1000,
    clock: Callable[[], Any] | None = None,
) -> SourceCycle:
    """Read every source once, independently and fail closed.

    A reader that raises, returns something other than a :class:`SourceRead`,
    or reports a read for the wrong source is recorded as a non-complete read
    with no items; it never becomes an empty success and never advances that
    source's bookmark. Other sources are unaffected, so one failure leaves the
    rest intact and ``covered`` is false with ``unqualified`` naming exactly
    the failures.

    Only the selected new items are committed to the bookmark, so a selection
    bounded by ``max_items_per_source`` can never advance ``high_water`` past
    an item it did not deliver.
    """

    if not callable(reader):
        raise SourcesError("reader must be callable")
    if not isinstance(bookmarks, Bookmarks):
        raise SourcesError("bookmarks must be a Bookmarks")
    _positive_int(max_items_per_source, "max_items_per_source")
    if (
        isinstance(overlap_seconds, bool)
        or not isinstance(overlap_seconds, int)
        or overlap_seconds < 0
    ):
        raise SourcesError("overlap_seconds must be a non-negative integer")

    ordered_ids: list[str] = []
    for source_id in source_ids:
        if not _is_non_empty_str(source_id):
            raise SourcesError("source_ids must be non-empty strings")
        if source_id not in ordered_ids:
            ordered_ids.append(source_id)

    reads: list[SourceRead] = []
    new_items: list[tuple[str, SourceItem]] = []
    for source_id in ordered_ids:
        bookmark = bookmarks.get(source_id)
        since = _overlap_since(bookmark, overlap_seconds)
        try:
            read = reader(source_id, since)
        except Exception as exc:  # noqa: BLE001 - an unreadable source is not empty
            read = _failed_read(
                source_id,
                SourceOutcome.UNAVAILABLE,
                f"source read failed: {type(exc).__name__}: {exc}",
                clock,
            )
        if not isinstance(read, SourceRead):
            read = _failed_read(
                source_id,
                SourceOutcome.INVALID,
                f"reader did not return a SourceRead, got "
                f"{type(read).__name__}",
                clock,
            )
        elif read.source_id != source_id:
            read = _failed_read(
                source_id,
                SourceOutcome.INVALID,
                f"reader returned a read for {read.source_id!r}",
                clock,
            )
        reads.append(read)
        if read.complete:
            selected = select_new(
                read,
                bookmark,
                overlap_seconds=overlap_seconds,
                max_items=max_items_per_source,
            )
            new_items.extend((source_id, item) for item in selected)
            bookmarks.advance(replace(read, items=selected))
        else:
            bookmarks.advance(read)
    return SourceCycle(reads=tuple(reads), new_items=tuple(new_items))
