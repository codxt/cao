"""Sources and bookmarks: fail-closed reads, replay, and monotone cursors.

Every test is deterministic: the clock is injected, the source readers are
injected callables, and state writes happen only under an ephemeral temporary
directory. No wall clock, network or real subprocess is used.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from cao.evidence import (
    EvidenceSource,
    Verdict,
    build_evidence,
    fingerprint,
)
from cao.sources import (
    SOURCE_STATE_SCHEMA,
    Bookmark,
    Bookmarks,
    SourceItem,
    SourceOutcome,
    SourceRead,
    SourcesCapacityError,
    SourcesError,
    SourcesStateError,
    collect,
    load_bookmarks,
    select_new,
)
from cao.state import StateCorruptError, read_state, write_state

T0 = "2026-01-01T09:00:00Z"
T1 = "2026-01-01T10:00:00Z"
T2 = "2026-01-01T10:05:00Z"
T3 = "2026-01-01T10:10:00Z"
CAPTURED = "2026-01-01T12:00:00Z"


def clock_at(moment: datetime = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)):
    return lambda: moment


def item(item_id: str, observed_at: str, title: str | None = None) -> SourceItem:
    return SourceItem(
        item_id=item_id,
        observed_at=observed_at,
        title=title if title is not None else item_id,
    )


def _read(source_id, outcome, evidence, items=(), captured_at=CAPTURED):
    return SourceRead(
        source_id=source_id,
        outcome=outcome,
        items=tuple(items),
        evidence=evidence,
        captured_at=captured_at,
    )


def ok_read(source_id: str, items=(), captured_at: str = CAPTURED) -> SourceRead:
    evidence = build_evidence(
        EvidenceSource.INTERNAL,
        Verdict.SUPPORTED,
        fingerprint_value=fingerprint([entry.item_id for entry in items]),
        coverage=1.0,
        capture_time=captured_at,
    )
    return _read(source_id, SourceOutcome.OK, evidence, items, captured_at)


def partial_read(source_id: str, items=(), captured_at: str = CAPTURED) -> SourceRead:
    evidence = build_evidence(
        EvidenceSource.INTERNAL,
        Verdict.UNKNOWN,
        reason="page 2 unavailable",
        capture_time=captured_at,
    )
    return _read(source_id, SourceOutcome.PARTIAL, evidence, items, captured_at)


def unavailable_read(source_id: str) -> SourceRead:
    evidence = build_evidence(
        EvidenceSource.INTERNAL, Verdict.UNAVAILABLE, reason="unreadable"
    )
    return _read(source_id, SourceOutcome.UNAVAILABLE, evidence)


def unknown_read(source_id: str) -> SourceRead:
    evidence = build_evidence(
        EvidenceSource.INTERNAL, Verdict.UNKNOWN, reason="unestablished"
    )
    return _read(source_id, SourceOutcome.UNKNOWN, evidence)


def invalid_read(source_id: str) -> SourceRead:
    evidence = build_evidence(
        EvidenceSource.INTERNAL, Verdict.INVALID, reason="malformed"
    )
    return _read(source_id, SourceOutcome.INVALID, evidence)


def ids(cycle) -> list[str]:
    return [entry.item_id for _, entry in cycle.new_items]


class ContractTests(unittest.TestCase):
    def test_schema_constant(self) -> None:
        self.assertEqual(SOURCE_STATE_SCHEMA, "cao.automation.sources.v0")

    def test_ok_read_requires_supported_evidence(self) -> None:
        evidence = build_evidence(EvidenceSource.INTERNAL, Verdict.UNKNOWN)
        with self.assertRaises(SourcesError):
            _read("s", SourceOutcome.OK, evidence)

    def test_non_ok_read_cannot_claim_supported_evidence(self) -> None:
        evidence = build_evidence(
            EvidenceSource.INTERNAL, Verdict.SUPPORTED, coverage=1.0
        )
        with self.assertRaises(SourcesError):
            _read("s", SourceOutcome.UNAVAILABLE, evidence)

    def test_unavailable_read_cannot_carry_items(self) -> None:
        evidence = build_evidence(EvidenceSource.INTERNAL, Verdict.UNAVAILABLE)
        with self.assertRaises(SourcesError):
            _read("s", SourceOutcome.UNAVAILABLE, evidence, [item("a", T1)])

    def test_partial_read_may_carry_items_but_is_not_complete(self) -> None:
        read = partial_read("s", [item("a", T1)])
        self.assertFalse(read.complete)
        self.assertEqual(read.outcome, SourceOutcome.PARTIAL)

    def test_malformed_timestamp_is_refused(self) -> None:
        with self.assertRaises(SourcesError):
            item("a", "not-a-timestamp")


class BookmarkAdvanceTests(unittest.TestCase):
    def test_advance_ok_read_moves_high_water_and_records_delivered(self) -> None:
        bookmarks = Bookmarks()
        self.assertTrue(bookmarks.advance(ok_read("s", [item("a", T1)])))
        entry = bookmarks.get("s")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.high_water, T1)
        self.assertEqual(entry.delivered, ("a",))
        self.assertEqual(entry.updated_at, CAPTURED)

    def test_advance_deduplicates_repeated_item_ids(self) -> None:
        bookmarks = Bookmarks()
        bookmarks.advance(ok_read("s", [item("a", T1), item("a", T2)]))
        self.assertEqual(bookmarks.get("s").delivered, ("a",))

    def test_non_ok_reads_leave_the_bookmark_identical(self) -> None:
        for read in (
            partial_read("s"),
            unavailable_read("s"),
            unknown_read("s"),
            invalid_read("s"),
        ):
            with self.subTest(outcome=read.outcome):
                bookmarks = Bookmarks()
                bookmarks.advance(ok_read("s", [item("a", T1)]))
                before = bookmarks.as_state()
                self.assertFalse(bookmarks.advance(read))
                self.assertEqual(bookmarks.as_state(), before)

    def test_advance_never_lowers_high_water(self) -> None:
        bookmarks = Bookmarks()
        bookmarks.advance(ok_read("s", [item("b", T2)]))
        self.assertTrue(bookmarks.advance(ok_read("s", [item("a", T1)])))
        self.assertEqual(bookmarks.get("s").high_water, T2)

    def test_seen_reports_delivered_ids_only(self) -> None:
        bookmarks = Bookmarks()
        bookmarks.advance(ok_read("s", [item("a", T1)]))
        self.assertTrue(bookmarks.seen("s", "a"))
        self.assertFalse(bookmarks.seen("s", "b"))
        self.assertFalse(bookmarks.seen("other", "a"))

    def test_advance_refuses_over_capacity_without_mutating(self) -> None:
        bookmarks = Bookmarks(max_delivered_per_source=2)
        before = bookmarks.as_state()
        with self.assertRaises(SourcesCapacityError):
            bookmarks.advance(
                ok_read("s", [item("a", T1), item("b", T1), item("c", T1)])
            )
        self.assertEqual(bookmarks.as_state(), before)
        self.assertIsNone(bookmarks.get("s"))


class SelectNewTests(unittest.TestCase):
    def test_new_source_selects_all_items_in_order(self) -> None:
        read = ok_read("s", [item("b", T1), item("a", T1), item("c", T2)])
        self.assertEqual(
            [entry.item_id for entry in select_new(read, None)],
            ["a", "b", "c"],
        )

    def test_late_run_selects_items_after_high_water(self) -> None:
        bookmarks = Bookmarks()
        bookmarks.advance(ok_read("s", [item("a", T1)]))
        read = ok_read("s", [item("a", T1), item("b", T2)])
        new = select_new(read, bookmarks.get("s"))
        self.assertEqual([entry.item_id for entry in new], ["b"])

    def test_early_run_before_previous_window_selects_nothing(self) -> None:
        bookmarks = Bookmarks()
        bookmarks.advance(ok_read("s", [item("b", T2)]))
        read = ok_read("s", [item("a", T1)])
        self.assertEqual(select_new(read, bookmarks.get("s")), ())
        # An early run must not pull the cursor backwards either.
        bookmarks.advance(read)
        self.assertEqual(bookmarks.get("s").high_water, T2)

    def test_identical_timestamps_are_not_lost(self) -> None:
        bookmarks = Bookmarks()
        bookmarks.advance(ok_read("s", [item("a", T2)]))
        read = ok_read("s", [item("c", T2), item("a", T2)])
        self.assertEqual(
            [entry.item_id for entry in select_new(read, bookmarks.get("s"))],
            ["c"],
        )

    def test_select_drops_already_delivered(self) -> None:
        read = ok_read("s", [item("a", T1), item("b", T2)])
        bookmark = Bookmark(
            source_id="s",
            high_water=T1,
            delivered=("a",),
            updated_at=CAPTURED,
        )
        self.assertEqual(
            [entry.item_id for entry in select_new(read, bookmark)],
            ["b"],
        )

    def test_select_is_bounded_by_max_items(self) -> None:
        read = ok_read(
            "s",
            [
                item("a", "2026-01-01T10:00:00Z"),
                item("b", "2026-01-01T10:00:01Z"),
                item("c", "2026-01-01T10:00:02Z"),
            ],
        )
        self.assertEqual(
            [entry.item_id for entry in select_new(read, None, max_items=2)],
            ["a", "b"],
        )

    def test_non_ok_read_selects_nothing(self) -> None:
        self.assertEqual(select_new(partial_read("s", [item("a", T1)]), None), ())
        self.assertEqual(select_new(unavailable_read("s"), None), ())


class PersistenceTests(unittest.TestCase):
    def test_restart_from_persisted_bookmark_selects_identically(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bookmarks.json"
            bookmarks = Bookmarks()
            bookmarks.advance(ok_read("s", [item("a", T1)]))
            write_state(path, bookmarks.as_state())

            loaded = load_bookmarks(read_state(path))
            read = ok_read("s", [item("a", T1), item("b", T2)])
            self.assertEqual(
                select_new(read, bookmarks.get("s")),
                select_new(read, loaded.get("s")),
            )
            self.assertEqual(
                [entry.item_id for entry in select_new(read, loaded.get("s"))],
                ["b"],
            )
            self.assertEqual(loaded.as_state(), bookmarks.as_state())

    def test_corrupt_state_file_raises(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bookmarks.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(StateCorruptError):
                read_state(path)

    def test_load_bookmarks_rejects_wrong_schema(self) -> None:
        with self.assertRaises(SourcesStateError):
            load_bookmarks({"schema": "other", "bookmarks": {}})

    def test_load_bookmarks_rejects_missing_bookmarks_mapping(self) -> None:
        with self.assertRaises(SourcesStateError):
            load_bookmarks({"schema": SOURCE_STATE_SCHEMA})

    def test_load_bookmarks_rejects_malformed_high_water(self) -> None:
        payload = {
            "schema": SOURCE_STATE_SCHEMA,
            "bookmarks": {
                "s": {
                    "source_id": "s",
                    "high_water": "not-a-timestamp",
                    "delivered": [],
                    "updated_at": CAPTURED,
                }
            },
        }
        with self.assertRaises(SourcesStateError):
            load_bookmarks(payload)

    def test_load_bookmarks_rejects_over_capacity_delivered(self) -> None:
        payload = {
            "schema": SOURCE_STATE_SCHEMA,
            "bookmarks": {
                "s": {
                    "source_id": "s",
                    "high_water": "",
                    "delivered": ["a", "b", "c"],
                    "updated_at": CAPTURED,
                }
            },
        }
        with self.assertRaises(SourcesCapacityError):
            load_bookmarks(payload, max_delivered_per_source=2)

    def test_load_bookmarks_round_trips(self) -> None:
        bookmarks = Bookmarks()
        bookmarks.advance(ok_read("s", [item("a", T1)]))
        loaded = load_bookmarks(bookmarks.as_state())
        self.assertEqual(loaded.as_state(), bookmarks.as_state())


class CollectTests(unittest.TestCase):
    def test_reader_that_raises_is_unavailable_not_empty(self) -> None:
        def reader(source_id, since):
            raise RuntimeError("transport boom")

        bookmarks = Bookmarks()
        cycle = collect(reader, ["s"], bookmarks, clock=clock_at())
        self.assertEqual(len(cycle.reads), 1)
        self.assertEqual(cycle.reads[0].outcome, SourceOutcome.UNAVAILABLE)
        self.assertFalse(cycle.reads[0].complete)
        self.assertEqual(cycle.reads[0].items, ())
        self.assertEqual(cycle.new_items, ())
        self.assertFalse(cycle.covered)
        self.assertEqual(cycle.unqualified, ("s",))
        self.assertIsNone(bookmarks.get("s"))

    def test_partial_reader_is_not_empty_and_not_advanced(self) -> None:
        def reader(source_id, since):
            return partial_read(source_id, [item("a", T1)])

        bookmarks = Bookmarks()
        cycle = collect(reader, ["s"], bookmarks, clock=clock_at())
        self.assertEqual(cycle.reads[0].outcome, SourceOutcome.PARTIAL)
        self.assertEqual(cycle.new_items, ())
        self.assertFalse(cycle.covered)
        self.assertEqual(cycle.unqualified, ("s",))
        self.assertIsNone(bookmarks.get("s"))

    def test_pagination_failure_is_qualified_partial(self) -> None:
        def reader(source_id, since):
            return partial_read(source_id)

        bookmarks = Bookmarks()
        cycle = collect(reader, ["s"], bookmarks, clock=clock_at())
        self.assertFalse(cycle.covered)
        self.assertEqual(cycle.unqualified, ("s",))
        self.assertIsNone(bookmarks.get("s"))

    def test_one_source_failing_leaves_another_intact(self) -> None:
        def reader(source_id, since):
            if source_id == "bad":
                raise RuntimeError("transport boom")
            return ok_read(source_id, [item(source_id + "-1", T1)])

        bookmarks = Bookmarks()
        cycle = collect(
            reader, ["bad", "good"], bookmarks, clock=clock_at()
        )
        self.assertEqual([read.source_id for read in cycle.reads], ["bad", "good"])
        self.assertFalse(cycle.covered)
        self.assertEqual(cycle.unqualified, ("bad",))
        self.assertEqual(ids(cycle), ["good-1"])
        self.assertIsNone(bookmarks.get("bad"))
        self.assertEqual(bookmarks.get("good").delivered, ("good-1",))

    def test_wrong_source_read_is_invalid(self) -> None:
        def reader(source_id, since):
            return ok_read("other", [item("x", T1)])

        bookmarks = Bookmarks()
        cycle = collect(reader, ["s"], bookmarks, clock=clock_at())
        self.assertEqual(cycle.reads[0].outcome, SourceOutcome.INVALID)
        self.assertEqual(cycle.new_items, ())
        self.assertFalse(cycle.covered)
        self.assertEqual(cycle.unqualified, ("s",))
        self.assertIsNone(bookmarks.get("s"))

    def test_collect_deduplicates_source_ids(self) -> None:
        def reader(source_id, since):
            return ok_read(source_id, [item("a", T1)])

        calls = []

        def tracking_reader(source_id, since):
            calls.append(source_id)
            return reader(source_id, since)

        cycle = collect(
            tracking_reader, ["s", "s"], Bookmarks(), clock=clock_at()
        )
        self.assertEqual(calls, ["s"])
        self.assertEqual(len(cycle.reads), 1)

    def test_bounded_selection_does_not_lose_items(self) -> None:
        everything = [
            item("a", "2026-01-01T10:00:00Z"),
            item("b", "2026-01-01T10:00:01Z"),
            item("c", "2026-01-01T10:00:02Z"),
        ]

        def reader(source_id, since):
            if since is None:
                chosen = everything
            else:
                chosen = [
                    entry for entry in everything if entry.observed_at >= since
                ]
            return ok_read(source_id, chosen)

        bookmarks = Bookmarks()
        first = collect(
            reader,
            ["s"],
            bookmarks,
            overlap_seconds=0,
            max_items_per_source=1,
            clock=clock_at(),
        )
        second = collect(
            reader,
            ["s"],
            bookmarks,
            overlap_seconds=0,
            max_items_per_source=1,
            clock=clock_at(),
        )
        third = collect(
            reader,
            ["s"],
            bookmarks,
            overlap_seconds=0,
            max_items_per_source=1,
            clock=clock_at(),
        )
        self.assertEqual(ids(first), ["a"])
        self.assertEqual(ids(second), ["b"])
        self.assertEqual(ids(third), ["c"])
        self.assertEqual(bookmarks.get("s").delivered, ("a", "b", "c"))

    def test_read_omission_does_not_duplicate_or_crash(self) -> None:
        state = {"call": 0}

        def reader(source_id, since):
            state["call"] += 1
            if state["call"] == 1:
                return ok_read(
                    source_id, [item("a", T1), item("b", T2)]
                )
            # The source now omits the still-present "a"; it must not be
            # re-delivered and the new "c" must still be selected.
            return ok_read(source_id, [item("b", T2), item("c", T3)])

        bookmarks = Bookmarks()
        first = collect(reader, ["s"], bookmarks, clock=clock_at())
        second = collect(reader, ["s"], bookmarks, clock=clock_at())
        self.assertEqual(ids(first), ["a", "b"])
        self.assertEqual(ids(second), ["c"])


if __name__ == "__main__":
    unittest.main()
