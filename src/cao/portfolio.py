"""A read-only, derived portfolio view over many lanes or work items.

The portfolio aggregates per-item facts a caller supplies into counts by state
and the structural block relations between items. It is deliberately a pure
function of its input: no mutation, no persistence, no scheduling decision and
no priority scoring heuristic.

One rule is load-bearing: an item whose state is unknown contributes to the
unknown count. It is never silently folded into a known bucket. Unknown is
represented by ``state is None``; a caller that omits a state therefore gets
an honest unknown rather than an assumed default.

Block relations are read from each item's ``blocked_by`` edges and the reverse
"blocks" relation is derived from them, so the two can never disagree. A
blocker that is referenced but not present in the portfolio is reported
explicitly rather than dropped.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_MAX_ITEMS",
    "PortfolioError",
    "PortfolioBoundError",
    "WorkItem",
    "Portfolio",
    "build_portfolio",
]

# A hard cap on the number of items one portfolio may carry. The aggregation
# iterates a caller-supplied finite collection; the cap bounds that work
# deterministically instead of trusting the input to be small.
DEFAULT_MAX_ITEMS = 100_000


class PortfolioError(Exception):
    """Raised for a caller mistake that must fail closed."""

    code = "PORTFOLIO_ERROR"


class PortfolioBoundError(PortfolioError):
    """The item collection exceeds the stated bound."""

    code = "PORTFOLIO_BOUND"


@dataclass(frozen=True)
class WorkItem:
    """One item's facts, as supplied by the caller.

    ``state is None`` means the item's state is unknown. ``blocked_by`` lists
    the item ids that block this one.
    """

    item_id: str
    state: str | None = None
    blocked_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class Portfolio:
    """A derived, read-only aggregate of a finite collection of work items."""

    items: tuple[WorkItem, ...]
    by_state: tuple[tuple[str, int], ...]
    unknown_items: tuple[str, ...]
    blocked_items: tuple[str, ...]
    blocking_items: tuple[str, ...]
    dangling_blockers: tuple[str, ...]

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def known_count(self) -> int:
        return sum(count for _, count in self.by_state)

    @property
    def unknown_count(self) -> int:
        return len(self.unknown_items)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "known_count": self.known_count,
            "unknown_count": self.unknown_count,
            "by_state": {state: count for state, count in self.by_state},
            "unknown_items": list(self.unknown_items),
            "blocked_items": list(self.blocked_items),
            "blocking_items": list(self.blocking_items),
            "dangling_blockers": list(self.dangling_blockers),
        }


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def build_portfolio(
    items: Iterable[WorkItem],
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> Portfolio:
    """Aggregate ``items`` into a :class:`Portfolio`, or fail closed.

    A duplicate item id, a self-block, or any malformed item raises
    :class:`PortfolioError`: an identity that appears twice makes counts
    untrustworthy and is never silently merged.
    """

    if isinstance(max_items, bool) or not isinstance(max_items, int):
        raise PortfolioError("max_items must be an integer")
    if max_items < 1:
        raise PortfolioError("max_items must be at least 1")

    collected: list[WorkItem] = []
    seen: set[str] = set()
    for item in items:
        if type(item) is not WorkItem:
            raise PortfolioError(
                f"items must be WorkItem, got {type(item).__name__}"
            )
        if not _non_empty_str(item.item_id):
            raise PortfolioError("work item id must be a non-empty string")
        if item.state is not None and not _non_empty_str(item.state):
            raise PortfolioError(
                f"item {item.item_id!r} state must be None or a non-empty string"
            )
        if not isinstance(item.blocked_by, (tuple, list)) or not all(
            _non_empty_str(blocker) for blocker in item.blocked_by
        ):
            raise PortfolioError(
                f"item {item.item_id!r} blocked_by must be a sequence of "
                "non-empty strings"
            )
        if item.item_id in item.blocked_by:
            raise PortfolioError(f"item {item.item_id!r} blocks itself")
        if item.item_id in seen:
            raise PortfolioError(f"duplicate work item id: {item.item_id!r}")
        if len(collected) >= max_items:
            raise PortfolioBoundError(
                f"portfolio exceeds max_items={max_items}"
            )
        seen.add(item.item_id)
        collected.append(item)

    ordered_ids = [item.item_id for item in collected]
    present = set(ordered_ids)

    state_counts: dict[str, int] = {}
    unknown_ids: list[str] = []
    blocked_ids: list[str] = []
    blockers_referenced: set[str] = set()
    for item in collected:
        if item.state is None:
            unknown_ids.append(item.item_id)
        else:
            state_counts[item.state] = state_counts.get(item.state, 0) + 1
        if item.blocked_by:
            blocked_ids.append(item.item_id)
            blockers_referenced.update(item.blocked_by)

    blocking_ids = [
        item_id for item_id in ordered_ids if item_id in blockers_referenced
    ]
    dangling = sorted(blockers_referenced - present)

    return Portfolio(
        items=tuple(collected),
        by_state=tuple(sorted(state_counts.items())),
        unknown_items=tuple(unknown_ids),
        blocked_items=tuple(blocked_ids),
        blocking_items=tuple(blocking_ids),
        dangling_blockers=tuple(dangling),
    )
