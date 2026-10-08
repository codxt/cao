"""Portfolio read-model tests: unknown state is never folded into a known bucket."""

from __future__ import annotations

import unittest

from cao.portfolio import (
    Portfolio,
    PortfolioBoundError,
    PortfolioError,
    WorkItem,
    build_portfolio,
)


class PortfolioTests(unittest.TestCase):
    def test_counts_by_state_and_unknowns(self) -> None:
        portfolio = build_portfolio(
            [
                WorkItem("a", "DONE"),
                WorkItem("b", "DONE"),
                WorkItem("c", "IN_PROGRESS"),
                WorkItem("d"),  # unknown: no state supplied
            ]
        )
        self.assertEqual(portfolio.total, 4)
        self.assertEqual(dict(portfolio.by_state), {"DONE": 2, "IN_PROGRESS": 1})
        self.assertEqual(portfolio.known_count, 3)
        self.assertEqual(portfolio.unknown_count, 1)
        self.assertEqual(portfolio.unknown_items, ("d",))
        # The unknown item is absent from every known bucket.
        self.assertNotIn("d", dict(portfolio.by_state))
        self.assertNotIn(None, dict(portfolio.by_state))

    def test_explicit_none_state_is_unknown(self) -> None:
        portfolio = build_portfolio([WorkItem("a", None)])
        self.assertEqual(portfolio.unknown_count, 1)
        self.assertEqual(portfolio.by_state, ())

    def test_block_relations_are_derived(self) -> None:
        portfolio = build_portfolio(
            [
                WorkItem("a", "WAITING", blocked_by=("b",)),
                WorkItem("b", "RUNNING"),
                WorkItem("c", "WAITING", blocked_by=("a", "z")),
            ]
        )
        self.assertEqual(portfolio.blocked_items, ("a", "c"))
        self.assertEqual(portfolio.blocking_items, ("a", "b"))
        # A referenced blocker that is not an item is reported, not dropped.
        self.assertEqual(portfolio.dangling_blockers, ("z",))

    def test_no_blockers_means_empty_relations(self) -> None:
        portfolio = build_portfolio([WorkItem("a", "DONE")])
        self.assertEqual(portfolio.blocked_items, ())
        self.assertEqual(portfolio.blocking_items, ())
        self.assertEqual(portfolio.dangling_blockers, ())

    def test_duplicate_item_id_fails_closed(self) -> None:
        with self.assertRaises(PortfolioError):
            build_portfolio([WorkItem("a", "DONE"), WorkItem("a", "DONE")])

    def test_self_block_fails_closed(self) -> None:
        with self.assertRaises(PortfolioError):
            build_portfolio([WorkItem("a", "DONE", blocked_by=("a",))])

    def test_bound_exceeded_fails_closed(self) -> None:
        with self.assertRaises(PortfolioBoundError):
            build_portfolio(
                [WorkItem("a"), WorkItem("b"), WorkItem("c")], max_items=2
            )

    def test_malformed_items_fail_closed(self) -> None:
        with self.assertRaises(PortfolioError):
            build_portfolio([{"item_id": "a", "state": "DONE"}])
        with self.assertRaises(PortfolioError):
            build_portfolio([WorkItem("", "DONE")])
        with self.assertRaises(PortfolioError):
            build_portfolio([WorkItem("a", "")])
        with self.assertRaises(PortfolioError):
            build_portfolio([WorkItem("a", "DONE", blocked_by=("",))])
        with self.assertRaises(PortfolioError):
            build_portfolio([WorkItem("a", "DONE", blocked_by=(1,))])

    def test_derivation_is_deterministic_and_read_only(self) -> None:
        items = [
            WorkItem("a", "DONE", blocked_by=("b",)),
            WorkItem("b", None),
        ]
        first = build_portfolio(items)
        second = build_portfolio(items)
        self.assertIsInstance(first, Portfolio)
        self.assertEqual(first.as_dict(), second.as_dict())
        # The caller's items are untouched.
        self.assertEqual(items[0].state, "DONE")


if __name__ == "__main__":
    unittest.main()
