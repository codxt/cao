"""Draft-only automation briefs for CAO.

This is a DRAFT-ONLY pilot whose destination is a LOCAL FILE. Nothing here is
ever sent: there is no HTTP, socket, SMTP, webhook, third-party mutation or
credential path, and this module imports none of them. The brief is composed
solely from injected adapters (a source ``reader`` and optional read-only
projections), never from a live network or a live host read.

Five properties are load-bearing and each is pinned by a test:

1. Truthful coverage. A source that is missing, unreadable or partial is named
   in the brief's own body as unqualified. A partial read is never shown as a
   complete one, and a source that could not be read is never shown as empty.
   "Read and genuinely empty" (``OK`` with no items) is a different statement
   from "could not be read" (any non-``OK`` outcome), and the rendered body
   distinguishes them.
2. Fresh recheck before output. After the items to report are selected, the
   contributing sources are re-read immediately before rendering. If the fresh
   observation disagrees with the selection, the brief reflects the fresh one.
3. No duplicate unchanged open items. An item the source still reports whose
   decision-relevant state is unchanged since the last brief is not repeated.
   New items, and items whose state actually changed, are what belong.
4. Stable sourced identifiers. Every reported line carries its stable source id
   and, when the source supplied one, its link. Nothing is unsourced.
5. No false health, ever. An automation that ran and could not see everything
   says so.

Selection is delegated to :mod:`cao.sources` (bookmarks + ``collect``) and
outward output to :mod:`cao.delivery` (a keyed ``LocalFileSink`` receipt). The
pending recheck is a second read through the same reader, so an interrupted run
that is repeated produces no missed items and no duplicates: bookmarks are only
advanced after a confirmed delivery, and receipts are keyed by brief content.

No host path is ever derived from manifest or source data. ``cao.spec``
validates ``locator``, ``preferences_ref``, ``state_ref`` and ``policy_ref``
only as non-empty strings; this module never turns any of them into a
filesystem path. The destination directory comes from the caller's sink.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .delivery import (
    DeliveryLedger,
    DeliveryReceipt,
    DeliveryState,
    deliver,
    delivery_key,
)
from .evidence import fingerprint, utc_now
from .sources import (
    Bookmarks,
    SourceItem,
    SourceOutcome,
    SourceRead,
    collect,
    load_bookmarks,
)
from .spec import AutomationSpec, UntrustedData

__all__ = [
    "BRIEF_SCHEMA",
    "BRIEF_HISTORY_SCHEMA",
    "BRIEF_SOURCE_ID",
    "BRIEF_TITLE",
    "MAX_SOURCES",
    "MAX_ITEMS_PER_SOURCE",
    "MAX_LINES",
    "MAX_QUOTED",
    "MAX_SIGNALS",
    "MAX_HISTORY",
    "BriefError",
    "BriefCapacityError",
    "BriefStateError",
    "Signal",
    "BriefLine",
    "SourceCoverage",
    "ObservedItem",
    "DraftBrief",
    "BriefHistory",
    "load_history",
    "BriefRun",
    "compose_brief",
    "render_markdown",
    "run_brief",
]

BRIEF_SCHEMA = "cao.automation.brief.v0"
BRIEF_HISTORY_SCHEMA = "cao.automation.brief.history.v0"

# The synthetic source identity used for the single keyed brief artifact. It is
# not a real source: the brief aggregates several sources into one document.
BRIEF_SOURCE_ID = "brief"
BRIEF_TITLE = "CAO automation brief"

# Explicit finite bounds. Every collection and loop in this module is bounded
# by one of these; unbounded iteration is a defect, and a selection that would
# exceed a bound raises rather than silently truncating an item.
MAX_SOURCES = 256
MAX_ITEMS_PER_SOURCE = 1000
MAX_LINES = 5000
MAX_QUOTED = 256
MAX_SIGNALS = 64
MAX_HISTORY = 4096
MAX_FIELD_CHARS = 500


class BriefError(Exception):
    """Base class for brief composition and delivery failures."""

    code = "BRIEF_ERROR"


class BriefCapacityError(BriefError):
    """A bounded brief collection cannot accept another entry."""

    code = "BRIEF_CAPACITY"


class BriefStateError(BriefError):
    """Persisted brief-observation state is present but not usable."""

    code = "BRIEF_STATE"


def _is_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _require_non_empty_str(value: Any, field: str) -> str:
    if not _is_non_empty_str(value):
        raise BriefError(f"{field} must be a non-empty string")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BriefError(f"{field} must be a positive integer")
    return value


def _one_line(text: Any) -> str:
    """Collapse untrusted free text to one bounded, control-free line.

    Untrusted text is quoted as data, never executed or obeyed. Whitespace is
    collapsed so a value cannot forge extra brief lines, and the result is
    capped with an explicit marker rather than silently dropped.
    """

    if not isinstance(text, str):
        text = str(text)
    collapsed = " ".join(text.split())
    collapsed = "".join(ch for ch in collapsed if ch.isprintable())
    if len(collapsed) > MAX_FIELD_CHARS:
        return collapsed[:MAX_FIELD_CHARS] + " [truncated]"
    return collapsed


def _state_of(item: SourceItem) -> str | None:
    """Return an item's declared ``state`` attribute, if any (data only)."""

    for key, value in item.attributes:
        if key.lower() == "state" and _is_non_empty_str(value):
            return value
    return None


def _item_fingerprint(item: SourceItem) -> str:
    """Fingerprint the decision-relevant state of an item.

    ``observed_at`` is deliberately excluded: it is the time the source
    observed the item, so a source that re-stamps every poll would otherwise
    look changed on every run. A source signals a real change through its
    title, link or attributes.
    """

    return fingerprint(
        {
            "item_id": item.item_id,
            "title": item.title,
            "link": item.link,
            "attributes": [list(pair) for pair in item.attributes],
        }
    )


@dataclass(frozen=True)
class Signal:
    """A read-only registry or runtime projection summarized truthfully.

    A signal is optional, injected context. A non-``OK`` outcome is reported as
    such and is never rounded up to healthy.
    """

    name: str
    outcome: SourceOutcome
    detail: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.name, "signal.name")
        if not isinstance(self.outcome, SourceOutcome):
            raise BriefError("signal.outcome must be a SourceOutcome")
        if self.detail is not None and not isinstance(self.detail, str):
            raise BriefError("signal.detail must be a string or None")


@dataclass(frozen=True)
class BriefLine:
    """One reported item: always sourced, optionally linked."""

    source_id: str
    item_id: str
    title: str
    link: str | None
    observed_at: str
    change: str
    state: str | None
    fingerprint: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.source_id, "line.source_id")
        _require_non_empty_str(self.item_id, "line.item_id")
        if self.change not in ("new", "changed"):
            raise BriefError("line.change must be 'new' or 'changed'")
        _require_non_empty_str(self.fingerprint, "line.fingerprint")
        if self.link is not None and not isinstance(self.link, str):
            raise BriefError("line.link must be a string or None")


@dataclass(frozen=True)
class SourceCoverage:
    """Truthful per-source coverage of one brief."""

    source_id: str
    outcome: SourceOutcome
    item_count: int
    detail: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.source_id, "coverage.source_id")
        if not isinstance(self.outcome, SourceOutcome):
            raise BriefError("coverage.outcome must be a SourceOutcome")
        if isinstance(self.item_count, bool) or not isinstance(self.item_count, int):
            raise BriefError("coverage.item_count must be an integer")
        if self.item_count < 0:
            raise BriefError("coverage.item_count must be non-negative")

    @property
    def complete(self) -> bool:
        return self.outcome is SourceOutcome.OK


@dataclass(frozen=True)
class ObservedItem:
    """The last reported decision-relevant state of one item."""

    source_id: str
    item_id: str
    fingerprint: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.source_id, "observed.source_id")
        _require_non_empty_str(self.item_id, "observed.item_id")
        _require_non_empty_str(self.fingerprint, "observed.fingerprint")


class BriefHistory:
    """Bounded prior-observation snapshot used to suppress unchanged items.

    It records only content fingerprints of items already reported; it stores
    no source text, no credential and no operational value. It is not a second
    delivery ledger: nothing is delivered from it, and persistence is the
    caller's choice through :meth:`as_state` / :func:`load_history`.
    """

    def __init__(
        self,
        entries: Iterable[ObservedItem] | None = None,
        *,
        max_entries: int = MAX_HISTORY,
    ) -> None:
        self._max_entries = _positive_int(max_entries, "max_entries")
        self._entries: dict[tuple[str, str], ObservedItem] = {}
        for entry in entries or ():
            if not isinstance(entry, ObservedItem):
                raise BriefStateError(
                    f"history entries must be ObservedItem, got "
                    f"{type(entry).__name__}"
                )
            key = (entry.source_id, entry.item_id)
            if key not in self._entries and len(self._entries) >= self._max_entries:
                raise BriefCapacityError(
                    f"brief history exceeds max_entries={self._max_entries}"
                )
            self._entries[key] = entry

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, source_id: str, item_id: str) -> ObservedItem | None:
        return self._entries.get((source_id, item_id))

    def record(self, source_id: str, item_id: str, item_fingerprint: str) -> None:
        """Record the latest reported state of one item, bounded and fail closed."""

        _require_non_empty_str(source_id, "source_id")
        _require_non_empty_str(item_id, "item_id")
        _require_non_empty_str(item_fingerprint, "fingerprint")
        key = (source_id, item_id)
        if key not in self._entries and len(self._entries) >= self._max_entries:
            raise BriefCapacityError(
                f"brief history exceeds max_entries={self._max_entries}; "
                "refusing a new observation"
            )
        self._entries[key] = ObservedItem(
            source_id=source_id, item_id=item_id, fingerprint=item_fingerprint
        )

    def as_state(self) -> dict[str, Any]:
        return {
            "schema": BRIEF_HISTORY_SCHEMA,
            "observed": [
                {
                    "source_id": entry.source_id,
                    "item_id": entry.item_id,
                    "fingerprint": entry.fingerprint,
                }
                for _, entry in sorted(self._entries.items())
            ],
        }


def load_history(
    payload: Any, *, max_entries: int = MAX_HISTORY
) -> BriefHistory:
    """Rebuild a history from persisted state, or fail closed.

    A payload that is not the exact expected shape raises; corrupt state is
    never read as an empty history.
    """

    _positive_int(max_entries, "max_entries")
    if not isinstance(payload, Mapping):
        raise BriefStateError(
            f"brief history state must be a mapping, got "
            f"{type(payload).__name__}"
        )
    if payload.get("schema") != BRIEF_HISTORY_SCHEMA:
        raise BriefStateError(
            f"unsupported brief history schema: {payload.get('schema')!r}"
        )
    raw = payload.get("observed")
    if not isinstance(raw, list):
        raise BriefStateError("brief history 'observed' must be a list")
    if len(raw) > max_entries:
        raise BriefCapacityError(
            f"brief history holds {len(raw)} entries, exceeding "
            f"max_entries={max_entries}"
        )
    entries: list[ObservedItem] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise BriefStateError("brief history entries must be mappings")
        source_id = item.get("source_id")
        item_id = item.get("item_id")
        stored = item.get("fingerprint")
        if not _is_non_empty_str(source_id) or not _is_non_empty_str(item_id):
            raise BriefStateError(
                "brief history entries need non-empty source_id and item_id"
            )
        if not _is_non_empty_str(stored):
            raise BriefStateError(
                "brief history entries need a non-empty fingerprint"
            )
        entries.append(
            ObservedItem(source_id=source_id, item_id=item_id, fingerprint=stored)
        )
    return BriefHistory(entries, max_entries=max_entries)


@dataclass(frozen=True)
class DraftBrief:
    """A composed, deterministic draft brief ready to render."""

    schema: str
    generated_at: str
    coverage: tuple[SourceCoverage, ...]
    lines: tuple[BriefLine, ...]
    quoted: tuple[UntrustedData, ...]
    signals: tuple[Signal, ...]
    content_fingerprint: str

    @property
    def unqualified(self) -> tuple[str, ...]:
        """The source ids that were not read completely, in request order."""

        return tuple(
            entry.source_id for entry in self.coverage if not entry.complete
        )

    @property
    def covered(self) -> bool:
        return not self.unqualified


@dataclass(frozen=True)
class BriefRun:
    """The result of one brief run: composed content and its delivery receipt."""

    brief: DraftBrief
    markdown: str
    key: str
    destination_id: str
    receipt: DeliveryReceipt
    delivered: bool
    bookmarks: Bookmarks
    history: BriefHistory | None


def _empty_bookmarks() -> Bookmarks:
    return Bookmarks()


def _fresh_read(
    reader: Any,
    source_id: str,
    *,
    clock: Any,
    max_items_per_source: int,
) -> SourceRead:
    """Re-read one source through ``cao.sources.collect`` (fail closed).

    Reusing ``collect`` here means a recheck that raises, returns a non-read or
    returns a read for the wrong source becomes an explicit non-``OK`` read
    rather than a stale success.
    """

    cycle = collect(
        reader,
        [source_id],
        _empty_bookmarks(),
        max_items_per_source=max_items_per_source,
        clock=clock,
    )
    return cycle.reads[0]


def _ordered_sources(source_ids: Iterable[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    for source_id in source_ids:
        _require_non_empty_str(source_id, "source_ids")
        if source_id not in ordered:
            ordered.append(source_id)
    if len(ordered) > MAX_SOURCES:
        raise BriefCapacityError(
            f"brief requested {len(ordered)} sources, exceeding "
            f"MAX_SOURCES={MAX_SOURCES}"
        )
    return tuple(ordered)


def _coerce_signals(projection: Any, name: str) -> tuple[Signal, ...]:
    """Accept an optional projection as a Signal or a bounded Signal sequence."""

    if projection is None:
        return ()
    if isinstance(projection, Signal):
        return (projection,)
    if isinstance(projection, Sequence) and not isinstance(
        projection, (str, bytes, bytearray)
    ):
        if len(projection) > MAX_SIGNALS:
            raise BriefCapacityError(
                f"{name} projection exceeds MAX_SIGNALS={MAX_SIGNALS}"
            )
        signals = tuple(projection)
        for signal in signals:
            if not isinstance(signal, Signal):
                raise BriefError(
                    f"{name} projection entries must be Signal values"
                )
        return signals
    raise BriefError(
        f"{name} projection must be a Signal, a sequence of Signals, or None"
    )


def _coverage_of(read: SourceRead) -> SourceCoverage:
    if read.outcome is SourceOutcome.OK:
        return SourceCoverage(
            source_id=read.source_id,
            outcome=read.outcome,
            item_count=len(read.items),
            detail=None,
        )
    return SourceCoverage(
        source_id=read.source_id,
        outcome=read.outcome,
        item_count=len(read.items),
        detail=read.evidence.reason or read.outcome.value,
    )


def _without_generated_at(rendered: str) -> str:
    """The rendered artifact with its single ``Generated:`` line removed.

    Exactly one line can start with ``Generated: ``: every free-text field is
    collapsed to one line before rendering, so untrusted text cannot forge a
    second one and make this ambiguous.
    """

    return "".join(
        line + "\n"
        for line in rendered.splitlines()
        if not line.startswith("Generated: ")
    )


def _compose(
    *,
    source_ids: Iterable[str],
    reader: Any,
    bookmarks: Bookmarks | None = None,
    registry_projection: Any = None,
    runtime_projection: Any = None,
    history: BriefHistory | None = None,
    source_texts: Mapping[str, UntrustedData] | None = None,
    clock: Any = None,
    overlap_seconds: int = 60,
    max_items_per_source: int = MAX_ITEMS_PER_SOURCE,
    max_lines: int = MAX_LINES,
    max_quoted: int = MAX_QUOTED,
) -> tuple[DraftBrief, tuple[SourceRead, ...]]:
    """Compose a draft brief, its final reads, and the items it selected.

    The selected set is returned because the bookmark cursor must never move
    past an item that was selected but not delivered; see
    :func:`_commit_bookmarks`.
    """

    if not callable(reader):
        raise BriefError("reader must be callable")
    if bookmarks is None:
        working = _empty_bookmarks()
    elif isinstance(bookmarks, Bookmarks):
        # A copy: selection must use the caller's committed pre-state, and
        # ``collect``'s in-memory advancement must not leak on failure. The
        # caller's own capacity carries over, so a legitimately configured
        # bookmark set is not rejected by its own defensive copy.
        working = load_bookmarks(
            bookmarks.as_state(),
            max_delivered_per_source=bookmarks.max_delivered_per_source,
        )
    else:
        raise BriefError(
            f"bookmarks must be a Bookmarks, got {type(bookmarks).__name__}"
        )
    if history is not None and not isinstance(history, BriefHistory):
        raise BriefError(
            f"history must be a BriefHistory, got {type(history).__name__}"
        )
    if source_texts is None:
        source_texts = {}
    elif not isinstance(source_texts, Mapping):
        raise BriefError("source_texts must be a mapping")
    _positive_int(max_lines, "max_lines")
    _positive_int(max_quoted, "max_quoted")
    _positive_int(max_items_per_source, "max_items_per_source")

    ordered = _ordered_sources(source_ids)

    cycle = collect(
        reader,
        ordered,
        working,
        overlap_seconds=overlap_seconds,
        max_items_per_source=max_items_per_source,
        clock=clock,
    )
    new_ids = {(source_id, item.item_id) for source_id, item in cycle.new_items}

    candidates: list[tuple[str, SourceItem, str]] = []
    for read in cycle.reads:
        if not read.complete:
            # A non-complete read is never a source of a complete picture.
            continue
        for item in read.items:
            if (read.source_id, item.item_id) in new_ids:
                candidates.append((read.source_id, item, "new"))
            elif history is not None:
                prior = history.get(read.source_id, item.item_id)
                if prior is not None and prior.fingerprint != _item_fingerprint(
                    item
                ):
                    candidates.append((read.source_id, item, "changed"))
    if len(candidates) > max_lines:
        raise BriefCapacityError(
            f"brief selection holds {len(candidates)} items, exceeding "
            f"max_lines={max_lines}; refusing to truncate"
        )

    order_index = {source_id: index for index, source_id in enumerate(ordered)}
    candidates.sort(
        key=lambda entry: (
            order_index[entry[0]],
            entry[1].observed_at,
            entry[1].item_id,
        )
    )

    # Fresh recheck: re-read every source that contributed an item immediately
    # before rendering, and reflect the fresh observation.
    final_reads: dict[str, SourceRead] = {
        read.source_id: read for read in cycle.reads
    }
    recheck_failed: set[str] = set()
    contributing: list[str] = []
    for source_id, _item, _change in candidates:
        if source_id not in contributing:
            contributing.append(source_id)
    for source_id in contributing:
        fresh = _fresh_read(
            reader,
            source_id,
            clock=clock,
            max_items_per_source=max_items_per_source,
        )
        final_reads[source_id] = fresh
        if not fresh.complete:
            recheck_failed.add(source_id)

    lines: list[BriefLine] = []
    for source_id, _item, change in candidates:
        if source_id in recheck_failed:
            # The fresh observation is a non-answer: the stale item may not be
            # presented as current.
            continue
        fresh = final_reads[source_id]
        fresh_item = next(
            (entry for entry in fresh.items if entry.item_id == _item.item_id),
            None,
        )
        if fresh_item is None:
            # The item is no longer observed; it is not reported as current.
            continue
        fresh_fingerprint = _item_fingerprint(fresh_item)
        if change == "changed" and history is not None:
            # The recheck corrected the CONTENT; it must also correct the
            # DECISION. An item the recheck reverted to its last delivered
            # state is unchanged again, so repeating it would duplicate it.
            prior = history.get(source_id, fresh_item.item_id)
            if prior is not None and prior.fingerprint == fresh_fingerprint:
                continue
        lines.append(
            BriefLine(
                source_id=source_id,
                item_id=fresh_item.item_id,
                title=fresh_item.title,
                link=fresh_item.link,
                observed_at=fresh_item.observed_at,
                change=change,
                state=_state_of(fresh_item),
                fingerprint=fresh_fingerprint,
            )
        )

    coverage = tuple(_coverage_of(final_reads[source_id]) for source_id in ordered)

    quoted: list[UntrustedData] = []
    for source_id in ordered:
        if len(quoted) >= max_quoted:
            break
        data = source_texts.get(source_id)
        if data is None:
            continue
        if not isinstance(data, UntrustedData):
            raise BriefError(
                f"source text for {source_id!r} must be UntrustedData"
            )
        if data.text:
            quoted.append(data)

    signals = _coerce_signals(registry_projection, "registry") + _coerce_signals(
        runtime_projection, "runtime"
    )

    # The delivery key is derived from the RENDERED artifact, not from a
    # hand-maintained list of fields. A list is exactly how the previous two
    # corrections missed a field that reached the output; deriving the key from
    # the output makes that class of bug impossible by construction. Only the
    # timestamp is excluded, because a repeat run that observes exactly the
    # same things is the same logical delivery and must not write a second
    # artifact.
    brief = DraftBrief(
        schema=BRIEF_SCHEMA,
        generated_at=utc_now(clock),
        coverage=coverage,
        lines=tuple(lines),
        quoted=tuple(quoted),
        signals=signals,
        content_fingerprint="",
    )
    brief = replace(
        brief,
        content_fingerprint=fingerprint(_without_generated_at(render_markdown(brief))),
    )
    return (
        brief,
        tuple(final_reads[source_id] for source_id in ordered),
        # (source_id, item_id, observed_at-as-selected) for every candidate.
        # The timestamp is the one the SELECTION saw, not the one the recheck
        # saw: an item the recheck dropped entirely is still a gap, and losing
        # it here is exactly how it would become unreachable.
        frozenset(
            (source_id, item.item_id, item.observed_at)
            for source_id, item, _change in candidates
        ),
    )


def compose_brief(
    *,
    source_ids: Iterable[str],
    reader: Any,
    bookmarks: Bookmarks | None = None,
    registry_projection: Any = None,
    runtime_projection: Any = None,
    history: BriefHistory | None = None,
    source_texts: Mapping[str, UntrustedData] | None = None,
    clock: Any = None,
    overlap_seconds: int = 60,
    max_items_per_source: int = MAX_ITEMS_PER_SOURCE,
    max_lines: int = MAX_LINES,
    max_quoted: int = MAX_QUOTED,
) -> DraftBrief:
    """Compose a draft brief from injected adapters.

    Selection follows :mod:`cao.sources`: new items since the bookmark, plus
    items whose recorded state changed. Unchanged items the source still
    reports are not repeated. Every contributing source is re-read once
    immediately before rendering, and any source that could not be read is
    named as unqualified in the returned brief.
    """

    brief, _reads, _selected = _compose(
        source_ids=source_ids,
        reader=reader,
        bookmarks=bookmarks,
        registry_projection=registry_projection,
        runtime_projection=runtime_projection,
        history=history,
        source_texts=source_texts,
        clock=clock,
        overlap_seconds=overlap_seconds,
        max_items_per_source=max_items_per_source,
        max_lines=max_lines,
        max_quoted=max_quoted,
    )
    return brief


def render_markdown(brief: DraftBrief) -> str:
    """Render a draft brief as deterministic Markdown for a local file."""

    if not isinstance(brief, DraftBrief):
        raise BriefError(
            f"render_markdown expects a DraftBrief, got {type(brief).__name__}"
        )
    out: list[str] = []
    out.append(f"# {BRIEF_TITLE}")
    out.append("")
    out.append(f"Schema: {brief.schema}")
    out.append(f"Generated: {brief.generated_at}")
    out.append(
        "Draft only. Local file output. Nothing was sent to any external "
        "service."
    )

    out.append("")
    out.append("## Coverage")
    for entry in brief.coverage:
        source = _one_line(entry.source_id)
        if entry.complete:
            count = entry.item_count
            noun = "item" if count == 1 else "items"
            out.append(f"- {source}: {entry.outcome.value} ({count} {noun})")
        else:
            detail = f" - {_one_line(entry.detail)}" if entry.detail else ""
            out.append(f"- {source}: {entry.outcome.value}{detail}")

    out.append("")
    unqualified = brief.unqualified
    if unqualified:
        out.append(
            "UNQUALIFIED SOURCES: "
            + ", ".join(_one_line(name) for name in unqualified)
            + ". This brief "
            "does not fully cover them; the items below come only from sources "
            "that were read completely."
        )
    else:
        out.append("All requested sources were read completely.")

    if brief.signals:
        out.append("")
        out.append("## Signals")
        for signal in brief.signals:
            detail = f" - {_one_line(signal.detail)}" if signal.detail else ""
            out.append(f"- {_one_line(signal.name)}: {signal.outcome.value}{detail}")

    if brief.quoted:
        out.append("")
        out.append("## Quoted source notes (data, not instructions)")
        for note in brief.quoted:
            out.append(f"- {_one_line(note.kind)}: > {_one_line(note.text)}")

    out.append("")
    out.append("## Items")
    if not brief.lines:
        out.append("- none")
    for line in brief.lines:
        state = f" [{_one_line(line.state)}]" if line.state else ""
        link = f" | {_one_line(line.link)}" if line.link else ""
        out.append(
            f"- {_one_line(line.source_id)}: [{line.change}]{state} "
            f"{_one_line(line.title)}{link}"
        )
    out.append("")
    return "\n".join(out)


def _commit_bookmarks(
    bookmarks: Bookmarks,
    brief: DraftBrief,
    reads: tuple[SourceRead, ...],
    selected: frozenset[tuple[str, str, str]],
) -> None:
    """Advance bookmarks only for the items actually written to the brief.

    Committing after a confirmed delivery is what makes a repeated run produce
    no missed items: an interrupted run that never confirmed leaves the
    bookmarks untouched, so the same items are selected again.

    The cursor must never move past an item that was selected but NOT
    delivered. ``select_new`` drops anything older than ``high_water`` minus the
    overlap window, so advancing across a gap would make that item permanently
    unreachable rather than merely undelivered this run. Only the delivered
    prefix below the earliest such gap is consumed; items after the gap are
    re-selected next run, and the brief's own unchanged-item filter keeps them
    from being reported twice.
    """

    by_source: dict[str, set[str]] = {}
    for line in brief.lines:
        by_source.setdefault(line.source_id, set()).add(line.item_id)

    for read in reads:
        wanted = by_source.get(read.source_id)
        if not wanted or not read.complete:
            continue
        observed = {entry.item_id: entry.observed_at for entry in read.items}
        gaps = [
            selected_at
            for source_id, item_id, selected_at in selected
            if source_id == read.source_id and item_id not in wanted
        ]
        # Nothing is consumed past the earliest selected-but-undelivered item.
        ceiling = min(gaps) if gaps else None
        items = tuple(
            entry
            for entry in read.items
            if entry.item_id in wanted
            and (ceiling is None or entry.observed_at < ceiling)
        )
        if items:
            bookmarks.advance(replace(read, items=items))


def run_brief(
    spec: AutomationSpec,
    *,
    reader: Any,
    sink: Any,
    ledger: DeliveryLedger,
    bookmarks: Bookmarks,
    clock: Any = None,
    registry_projection: Any = None,
    runtime_projection: Any = None,
    history: BriefHistory | None = None,
    overlap_seconds: int = 60,
    max_items_per_source: int = MAX_ITEMS_PER_SOURCE,
    max_lines: int = MAX_LINES,
) -> BriefRun:
    """Compose, render and deliver one draft brief to the local sink.

    Output goes only through :func:`cao.delivery.deliver` to the caller's sink.
    The delivery key is derived from the brief content, so a repeated or
    interrupted run reconciles to the same artifact instead of duplicating it.
    Bookmarks are advanced only when the receipt is ``CONFIRMED``; an
    ``UNCERTAIN`` or ``HELD`` delivery is never reported as delivered.
    """

    if not isinstance(spec, AutomationSpec):
        raise BriefError(
            f"run_brief expects an AutomationSpec, got {type(spec).__name__}"
        )
    if not isinstance(ledger, DeliveryLedger):
        raise BriefError(
            f"ledger must be a DeliveryLedger, got {type(ledger).__name__}"
        )
    if not isinstance(bookmarks, Bookmarks):
        raise BriefError(
            f"bookmarks must be a Bookmarks, got {type(bookmarks).__name__}"
        )

    source_ids = tuple(source.source_id for source in spec.sources)
    source_texts = {source.source_id: source.text for source in spec.sources}

    brief, reads, selected = _compose(
        source_ids=source_ids,
        reader=reader,
        bookmarks=bookmarks,
        registry_projection=registry_projection,
        runtime_projection=runtime_projection,
        history=history,
        source_texts=source_texts,
        clock=clock,
        overlap_seconds=overlap_seconds,
        max_items_per_source=max_items_per_source,
        max_lines=max_lines,
    )
    markdown = render_markdown(brief)
    destination_id = spec.destination.destination_id
    key = delivery_key(
        BRIEF_SOURCE_ID, destination_id, brief.content_fingerprint
    )
    receipt = deliver(
        ledger,
        sink,
        key=key,
        destination_id=destination_id,
        # The rendered body already carries its own H1; an empty title keeps the
        # artifact from opening with the same heading twice.
        title="",
        body=markdown,
        clock=clock,
    )
    delivered = receipt.state is DeliveryState.CONFIRMED
    if delivered:
        _commit_bookmarks(bookmarks, brief, reads, selected)
        if history is not None:
            for line in brief.lines:
                history.record(line.source_id, line.item_id, line.fingerprint)

    return BriefRun(
        brief=brief,
        markdown=markdown,
        key=key,
        destination_id=destination_id,
        receipt=receipt,
        delivered=delivered,
        bookmarks=bookmarks,
        history=history,
    )
