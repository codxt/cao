"""Relay wakes only on durable progress, deduplicated and bounded."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cao.progress import fingerprint_writer_state
from cao.relay import (
    RELAY_STATE_SCHEMA,
    ManagerWake,
    ProgressRelay,
    RelayBudget,
    RelayCapacityError,
    RelayError,
    RelayLedger,
    RelayStateError,
    RelayVerdict,
    load_ledger,
)
from cao.state import read_state


def payload(revision="rev-1", status="RUNNING", scope=("src/a.py",), metrics=None):
    state = {"scope": list(scope), "revision": revision, "status": status}
    if metrics is not None:
        state["metrics"] = metrics
    return state


class StaticReader:
    """Returns the same state on every read."""

    def __init__(self, value):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return copy.deepcopy(self.value)


class SequenceReader:
    """Returns the next state each call, looping forever (a flapping source)."""

    def __init__(self, *values):
        self.values = values
        self.calls = 0

    def __call__(self):
        value = self.values[self.calls % len(self.values)]
        self.calls += 1
        return copy.deepcopy(value)


class RaisingReader:
    def __init__(self, exc=None):
        self.exc = exc or OSError("state source unavailable")
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise self.exc


class RecordingSink:
    def __init__(self):
        self.events = []

    def __call__(self, wake):
        self.events.append(wake)


class RecordThenRaiseSink:
    """Records the wake and then raises: delivery may already have happened."""

    def __init__(self):
        self.events = []

    def __call__(self, wake):
        self.events.append(wake)
        raise RuntimeError("manager channel failed after recording")


class SleepRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def fixed_clock():
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def seeded(sink, state, *, writer_id="w-1", **kwargs):
    ledger = RelayLedger(
        writers={writer_id: fingerprint_writer_state(state)}
    )
    return ProgressRelay(sink, ledger=ledger, clock=fixed_clock, **kwargs), ledger


class ProgressGateTests(unittest.TestCase):
    def test_unchanged_fingerprint_produces_no_wake(self) -> None:
        state = payload()
        sink = RecordingSink()
        relay, _ = seeded(sink, state)
        result = relay.check(StaticReader(state), writer_id="w-1")
        self.assertIs(result.verdict, RelayVerdict.NO_PROGRESS)
        self.assertEqual(sink.events, [])

    def test_changed_fingerprint_produces_exactly_one_wake(self) -> None:
        sink = RecordingSink()
        relay = ProgressRelay(
            sink,
            ledger=RelayLedger(writers={"w-1": "sha256:prior"}),
            clock=fixed_clock,
        )
        first = relay.check(StaticReader(payload()), writer_id="w-1")
        self.assertIs(first.verdict, RelayVerdict.DELIVERED)
        second = relay.check(StaticReader(payload()), writer_id="w-1")
        self.assertIs(second.verdict, RelayVerdict.NO_PROGRESS)
        self.assertEqual(len(sink.events), 1)
        wake = sink.events[0]
        self.assertIsInstance(wake, ManagerWake)
        self.assertEqual(wake.previous_fingerprint, "sha256:prior")
        self.assertEqual(wake.captured_at, "2026-01-01T00:00:00Z")

    def test_first_observation_establishes_baseline_and_wakes(self) -> None:
        sink = RecordingSink()
        relay = ProgressRelay(sink, ledger=RelayLedger(), clock=fixed_clock)
        result = relay.check(StaticReader(payload()), writer_id="w-1")
        self.assertIs(result.verdict, RelayVerdict.DELIVERED)
        self.assertEqual(len(sink.events), 1)

    def test_duplicate_event_identity_collapses_to_one_wake(self) -> None:
        sink = RecordingSink()
        relay = ProgressRelay(sink, ledger=RelayLedger(), clock=fixed_clock)
        a = payload(revision="rev-a")
        b = payload(revision="rev-b")
        self.assertIs(
            relay.check(StaticReader(a), writer_id="w-1").verdict,
            RelayVerdict.DELIVERED,
        )
        self.assertIs(
            relay.check(StaticReader(b), writer_id="w-1").verdict,
            RelayVerdict.DELIVERED,
        )
        # Recurrence of rev-a is a new fingerprint but an already-delivered
        # identity: it must not wake again.
        self.assertIs(
            relay.check(StaticReader(a), writer_id="w-1").verdict,
            RelayVerdict.DUPLICATE,
        )
        self.assertEqual(len(sink.events), 2)
        self.assertIs(
            relay.check(StaticReader(a), writer_id="w-1").verdict,
            RelayVerdict.NO_PROGRESS,
        )
        self.assertEqual(len(sink.events), 2)


class FailClosedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _relay_with_state(self, raw: str) -> tuple[ProgressRelay, RecordingSink]:
        path = self.dir / "relay.json"
        path.write_text(raw, encoding="utf-8")
        sink = RecordingSink()
        relay = ProgressRelay(
            sink, path, ledger=None, clock=fixed_clock
        )
        return relay, sink

    def test_corrupt_state_file_is_unknown_and_untouched(self) -> None:
        relay, sink = self._relay_with_state("{broken")
        result = relay.check(StaticReader(payload()), writer_id="w-1")
        self.assertIs(result.verdict, RelayVerdict.UNKNOWN)
        self.assertEqual(sink.events, [])
        self.assertEqual((self.dir / "relay.json").read_text(encoding="utf-8"), "{broken")

    def test_truncated_state_file_is_unknown(self) -> None:
        relay, sink = self._relay_with_state('{"schema": "cao.relay.v0", "delivered": [')
        result = relay.check(StaticReader(payload()), writer_id="w-1")
        self.assertIs(result.verdict, RelayVerdict.UNKNOWN)
        self.assertEqual(sink.events, [])

    def test_malformed_state_shape_is_unknown(self) -> None:
        relay, sink = self._relay_with_state(
            json.dumps({"schema": RELAY_STATE_SCHEMA, "delivered": "nope", "writers": {}})
        )
        result = relay.check(StaticReader(payload()), writer_id="w-1")
        self.assertIs(result.verdict, RelayVerdict.UNKNOWN)
        self.assertEqual(sink.events, [])

    def test_unsupported_schema_is_unknown(self) -> None:
        relay, sink = self._relay_with_state(
            json.dumps({"schema": "other.v9", "delivered": [], "writers": {}})
        )
        result = relay.check(StaticReader(payload()), writer_id="w-1")
        self.assertIs(result.verdict, RelayVerdict.UNKNOWN)
        self.assertEqual(sink.events, [])

    def test_hostile_numeric_field_yields_no_wake(self) -> None:
        for hostile in (float("nan"), float("inf"), float("-inf"), True, "3"):
            with self.subTest(value=hostile):
                sink = RecordingSink()
                relay = ProgressRelay(sink, ledger=RelayLedger(), clock=fixed_clock)
                result = relay.check(
                    StaticReader(payload(metrics={"steps": hostile})),
                    writer_id="w-1",
                )
                self.assertIs(result.verdict, RelayVerdict.UNKNOWN)
                self.assertEqual(sink.events, [])

    def test_unexpected_payload_type_is_unknown(self) -> None:
        for hostile in (None, ["not", "a", "mapping"], "state", 7):
            with self.subTest(payload=hostile):
                sink = RecordingSink()
                relay = ProgressRelay(sink, ledger=RelayLedger(), clock=fixed_clock)
                result = relay.check(StaticReader(hostile), writer_id="w-1")
                self.assertIs(result.verdict, RelayVerdict.UNKNOWN)
                self.assertEqual(sink.events, [])

    def test_flapping_state_mid_read_is_unknown(self) -> None:
        sink = RecordingSink()
        ledger = RelayLedger()
        relay = ProgressRelay(sink, ledger=ledger, clock=fixed_clock)
        reader = SequenceReader(payload(revision="rev-a"), payload(revision="rev-b"))
        result = relay.check(reader, writer_id="w-1")
        self.assertIs(result.verdict, RelayVerdict.UNKNOWN)
        self.assertIn("mid-read", result.reason or "")
        self.assertEqual(sink.events, [])
        self.assertEqual(ledger.delivered, ())

    def test_non_callable_reader_is_invalid(self) -> None:
        relay = ProgressRelay(RecordingSink(), ledger=RelayLedger())
        result = relay.check(42, writer_id="w-1")  # type: ignore[arg-type]
        self.assertIs(result.verdict, RelayVerdict.INVALID)

    def test_empty_writer_id_is_invalid(self) -> None:
        relay = ProgressRelay(RecordingSink(), ledger=RelayLedger())
        result = relay.check(StaticReader(payload()), writer_id="")
        self.assertIs(result.verdict, RelayVerdict.INVALID)

    def test_missing_sink_is_unavailable_not_silent(self) -> None:
        relay = ProgressRelay(None, ledger=RelayLedger(), clock=fixed_clock)
        result = relay.check(StaticReader(payload()), writer_id="w-1")
        self.assertIs(result.verdict, RelayVerdict.UNAVAILABLE)
        self.assertIn("sink", result.reason or "")

    def test_reader_failure_is_retryable_unavailable(self) -> None:
        relay = ProgressRelay(RecordingSink(), ledger=RelayLedger())
        result = relay.check(RaisingReader(), writer_id="w-1")
        self.assertIs(result.verdict, RelayVerdict.UNAVAILABLE)
        self.assertTrue(result.retryable)


class RestartRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "relay.json"

    def test_restart_does_not_rewake_unchanged_work(self) -> None:
        state = payload()
        first_sink = RecordingSink()
        first = ProgressRelay(first_sink, self.path, clock=fixed_clock)
        self.assertIs(
            first.check(StaticReader(state), writer_id="w-1").verdict,
            RelayVerdict.DELIVERED,
        )
        self.assertEqual(len(first_sink.events), 1)

        second_sink = RecordingSink()
        second = ProgressRelay(second_sink, self.path, clock=fixed_clock)
        self.assertIs(
            second.check(StaticReader(state), writer_id="w-1").verdict,
            RelayVerdict.NO_PROGRESS,
        )
        self.assertEqual(second_sink.events, [])

        self.assertIs(
            second.check(
                StaticReader(payload(revision="rev-2")), writer_id="w-1"
            ).verdict,
            RelayVerdict.DELIVERED,
        )
        self.assertEqual(len(second_sink.events), 1)

        third_sink = RecordingSink()
        third = ProgressRelay(third_sink, self.path, clock=fixed_clock)
        self.assertIs(
            third.check(StaticReader(payload(revision="rev-2")), writer_id="w-1").verdict,
            RelayVerdict.NO_PROGRESS,
        )
        self.assertEqual(third_sink.events, [])

    def test_persisted_state_is_inspectable(self) -> None:
        relay = ProgressRelay(RecordingSink(), self.path, clock=fixed_clock)
        result = relay.check(StaticReader(payload()), writer_id="w-1")
        persisted = read_state(self.path)
        self.assertEqual(persisted["schema"], RELAY_STATE_SCHEMA)
        self.assertIn(result.identity, persisted["delivered"])
        self.assertEqual(
            persisted["writers"]["w-1"], fingerprint_writer_state(payload())
        )

    def test_failed_wake_cannot_erase_a_used_identity(self) -> None:
        # A sink that records the wake and then raises is ordinary input; the
        # exception alone must not authorize a second wake with the same
        # identity, because delivery may already have happened.
        sink = RecordThenRaiseSink()
        first = ProgressRelay(sink, self.path, clock=fixed_clock)
        result = first.check(StaticReader(payload()), writer_id="w-1")
        self.assertIs(result.verdict, RelayVerdict.UNAVAILABLE)
        self.assertEqual(len(sink.events), 1)

        second = ProgressRelay(RecordingSink(), self.path, clock=fixed_clock)
        repeat = second.check(StaticReader(payload()), writer_id="w-1")
        self.assertIs(repeat.verdict, RelayVerdict.NO_PROGRESS)
        self.assertEqual(len(sink.events), 1)

    def test_oversized_persisted_state_is_unknown_and_untouched(self) -> None:
        oversized = {
            "schema": RELAY_STATE_SCHEMA,
            "delivered": ["id-1", "id-2", "id-3"],
            "writers": {},
        }
        self.path.write_text(json.dumps(oversized), encoding="utf-8")
        before = self.path.read_bytes()
        sink = RecordingSink()
        relay = ProgressRelay(sink, self.path, max_identities=2, clock=fixed_clock)
        result = relay.check(StaticReader(payload()), writer_id="w-1")
        self.assertIs(result.verdict, RelayVerdict.UNKNOWN)
        self.assertEqual(sink.events, [])
        # Fail closed without rewriting or truncating the rejected state.
        self.assertEqual(self.path.read_bytes(), before)


class BoundedRunTests(unittest.TestCase):
    def test_check_budget_is_respected_and_does_not_loop(self) -> None:
        state = payload()
        sink = RecordingSink()
        sleep = SleepRecorder()
        relay, _ = seeded(
            sink,
            state,
            budget=RelayBudget(interval_seconds=0, max_checks=4, max_errors=2),
            sleep=sleep,
        )
        result = relay.run(StaticReader(state), writer_id="w-1")
        self.assertIs(result.verdict, RelayVerdict.UNKNOWN)
        self.assertEqual(len(sleep.calls), 3)
        self.assertEqual(sink.events, [])

    def test_retry_budget_is_respected_on_reader_failure(self) -> None:
        sink = RecordingSink()
        sleep = SleepRecorder()
        relay = ProgressRelay(
            sink,
            ledger=RelayLedger(),
            budget=RelayBudget(interval_seconds=0, max_checks=10, max_errors=2),
            sleep=sleep,
        )
        reader = RaisingReader()
        result = relay.run(reader, writer_id="w-1")
        self.assertIs(result.verdict, RelayVerdict.UNAVAILABLE)
        self.assertEqual(len(sleep.calls), 1)
        self.assertEqual(reader.calls, 2)
        self.assertEqual(sink.events, [])

    def test_run_stops_at_first_delivery(self) -> None:
        sink = RecordingSink()
        sleep = SleepRecorder()
        relay = ProgressRelay(
            sink,
            ledger=RelayLedger(),
            budget=RelayBudget(interval_seconds=0, max_checks=5),
            sleep=sleep,
        )
        result = relay.run(StaticReader(payload()), writer_id="w-1")
        self.assertIs(result.verdict, RelayVerdict.DELIVERED)
        self.assertEqual(sleep.calls, [])
        self.assertEqual(len(sink.events), 1)

    def test_invalid_budget_is_rejected(self) -> None:
        for kwargs in (
            {"max_checks": 0},
            {"max_errors": 0},
            {"interval_seconds": -1},
            {"interval_seconds": float("nan")},
            {"interval_seconds": float("inf")},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(RelayError):
                    RelayBudget(**kwargs)


class LedgerTests(unittest.TestCase):
    def test_record_and_seen(self) -> None:
        ledger = RelayLedger()
        self.assertFalse(ledger.seen("id-1"))
        ledger.record("id-1", "w-1", "sha256:x")
        self.assertTrue(ledger.seen("id-1"))
        self.assertEqual(ledger.last_fingerprint("w-1"), "sha256:x")

    def test_capacity_refuses_new_identities_without_forgetting(self) -> None:
        ledger = RelayLedger(max_identities=2)
        ledger.record("id-1", "w", "f1")
        ledger.record("id-2", "w", "f2")
        with self.assertRaises(RelayCapacityError):
            ledger.record("id-3", "w", "f3")
        # The bounded ledger keeps every used identity; nothing is evicted.
        self.assertEqual(ledger.delivered, ("id-1", "id-2"))
        self.assertFalse(ledger.seen("id-3"))
        # A recurring, already-seen identity is still recognized without growth.
        ledger.record("id-1", "w", "f1b")
        self.assertEqual(ledger.delivered, ("id-1", "id-2"))
        self.assertTrue(ledger.seen("id-1"))

    def test_constructor_rejects_oversized_state(self) -> None:
        with self.assertRaises(RelayStateError):
            RelayLedger(
                delivered=["id-1", "id-2", "id-3"], max_identities=2
            )

    def test_constructor_accepts_capacity_after_deduping(self) -> None:
        ledger = RelayLedger(
            delivered=["id-1", "id-1", "id-2"], max_identities=2
        )
        self.assertEqual(ledger.delivered, ("id-1", "id-2"))

    def test_loader_rejects_oversized_state(self) -> None:
        oversized = {
            "schema": RELAY_STATE_SCHEMA,
            "delivered": ["id-1", "id-2", "id-3"],
            "writers": {},
        }
        with self.assertRaises(RelayStateError):
            load_ledger(oversized, max_identities=2)

    def test_relay_capacity_never_rewakes_a_used_identity(self) -> None:
        # The reviewer's sequence A, B, C, A with capacity 2 must not deliver
        # four wakes: the recurring A can never wake again.
        sink = RecordingSink()
        ledger = RelayLedger(max_identities=2)
        relay = ProgressRelay(sink, ledger=ledger, clock=fixed_clock)
        a = payload(revision="rev-a")
        b = payload(revision="rev-b")
        c = payload(revision="rev-c")
        self.assertIs(
            relay.check(StaticReader(a), writer_id="w-1").verdict,
            RelayVerdict.DELIVERED,
        )
        self.assertIs(
            relay.check(StaticReader(b), writer_id="w-1").verdict,
            RelayVerdict.DELIVERED,
        )
        self.assertIs(
            relay.check(StaticReader(c), writer_id="w-1").verdict,
            RelayVerdict.UNKNOWN,
        )
        self.assertIs(
            relay.check(StaticReader(a), writer_id="w-1").verdict,
            RelayVerdict.DUPLICATE,
        )
        self.assertEqual(len(sink.events), 2)
        identities = [event.identity for event in sink.events]
        self.assertEqual(len(set(identities)), 2)

    def test_json_reachable_huge_integer_yields_unknown_not_a_wake(self) -> None:
        # 10**400 arrives through ordinary JSON and overflows float conversion.
        hostile = payload(metrics={"steps": 10 ** 400})
        sink = RecordingSink()
        relay = ProgressRelay(sink, ledger=RelayLedger(), clock=fixed_clock)
        result = relay.check(StaticReader(hostile), writer_id="w-1")
        self.assertIs(result.verdict, RelayVerdict.UNKNOWN)
        self.assertIsNone(result.fingerprint)
        self.assertEqual(sink.events, [])

    def test_state_round_trips(self) -> None:
        ledger = RelayLedger(delivered=["id-1"], writers={"w-1": "sha256:x"})
        restored = load_ledger(ledger.as_state())
        self.assertEqual(restored.delivered, ("id-1",))
        self.assertEqual(restored.writers, {"w-1": "sha256:x"})

    def test_load_rejects_malformed_payloads(self) -> None:
        bad = (
            None,
            [],
            {},
            {"schema": "other", "delivered": [], "writers": {}},
            {"schema": RELAY_STATE_SCHEMA, "delivered": "x", "writers": {}},
            {"schema": RELAY_STATE_SCHEMA, "delivered": [1], "writers": {}},
            {"schema": RELAY_STATE_SCHEMA, "delivered": [], "writers": []},
            {"schema": RELAY_STATE_SCHEMA, "delivered": [], "writers": {"": "x"}},
            {"schema": RELAY_STATE_SCHEMA, "delivered": [], "writers": {"w": ""}},
        )
        for hostile in bad:
            with self.subTest(payload=hostile):
                with self.assertRaises(RelayStateError):
                    load_ledger(hostile)

    def test_max_identities_must_be_positive_int(self) -> None:
        for hostile in (0, -1, True, "4"):
            with self.subTest(value=hostile):
                with self.assertRaises(RelayStateError):
                    RelayLedger(max_identities=hostile)

    def test_result_is_inspectable(self) -> None:
        sink = RecordingSink()
        relay = ProgressRelay(sink, ledger=RelayLedger(), clock=fixed_clock)
        result = relay.check(StaticReader(payload()), writer_id="w-1")
        rendered = result.as_dict()
        self.assertEqual(rendered["verdict"], "DELIVERED")
        self.assertEqual(rendered["identity"], result.identity)
        self.assertEqual(sink.events[0].as_dict()["fingerprint"], result.fingerprint)


if __name__ == "__main__":
    unittest.main()
