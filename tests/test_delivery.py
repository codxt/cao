"""Tests for outward delivery: uncertainty, crash safety, bounds, fail-closed.

Every test injects a clock and a sink and writes only under an ephemeral temp
directory. No wall clock, no network, no real subprocess.
"""

from __future__ import annotations

import ast
import importlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cao.delivery import (
    DELIVERY_STATE_SCHEMA,
    DeliveryCapacityError,
    DeliveryLedger,
    DeliveryMismatchError,
    DeliveryReceipt,
    DeliveryState,
    DeliveryStateError,
    LocalFileSink,
    SinkAck,
    deliver,
    delivery_key,
    load_ledger,
    reconcile_delivery,
)
from cao.state import StateCorruptError, read_state, write_state

SOURCE_ID = "feed"
DESTINATION_ID = "local-notes"
CYCLE_ID = "cycle-0001"


def fixed_clock():
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def a_key():
    return delivery_key(SOURCE_ID, DESTINATION_ID, CYCLE_ID)


class MemorySink:
    """An in-memory destination that models a transport with optional answers."""

    def __init__(
        self,
        *,
        deliver_ack: SinkAck | None = None,
        reconcile_ack: SinkAck | None = None,
        fail_deliver: bool = False,
        fail_reconcile: bool = False,
        record: bool = True,
    ) -> None:
        self.artifacts: dict[str, tuple[str, str]] = {}
        self.deliver_calls = 0
        self.reconcile_calls = 0
        self._deliver_ack = deliver_ack
        self._reconcile_ack = reconcile_ack
        self._fail_deliver = fail_deliver
        self._fail_reconcile = fail_reconcile
        self._record = record

    def deliver(self, key: str, title: str, body: str) -> SinkAck:
        self.deliver_calls += 1
        if self._fail_deliver:
            raise RuntimeError("transport unavailable")
        if self._record:
            self.artifacts[key] = (title, body)
        if self._deliver_ack is not None:
            return self._deliver_ack
        return SinkAck(ack_id=key, positive=True)

    def reconcile(self, key: str) -> SinkAck | None:
        self.reconcile_calls += 1
        if self._fail_reconcile:
            raise RuntimeError("reconcile unavailable")
        if self._reconcile_ack is not None:
            return self._reconcile_ack
        if key in self.artifacts:
            return SinkAck(ack_id=key, positive=True)
        return None


def persist(path: Path, ledger: DeliveryLedger) -> None:
    write_state(path, ledger.as_state())


def md_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.md"))


class DeliveryKeyTests(unittest.TestCase):
    def test_delivery_key_is_stable_and_scoped(self) -> None:
        first = delivery_key(SOURCE_ID, DESTINATION_ID, CYCLE_ID)
        self.assertEqual(first, a_key())
        self.assertNotEqual(first, delivery_key("other", DESTINATION_ID, CYCLE_ID))
        self.assertNotEqual(first, delivery_key(SOURCE_ID, "other", CYCLE_ID))
        self.assertNotEqual(first, delivery_key(SOURCE_ID, DESTINATION_ID, "other"))


class HappyPathTests(unittest.TestCase):
    def test_happy_path_reaches_confirmed_with_receipt_id(self) -> None:
        ledger = DeliveryLedger(clock=fixed_clock)
        sink = MemorySink()
        key = a_key()
        receipt = deliver(
            ledger,
            sink,
            key=key,
            destination_id=DESTINATION_ID,
            title="Title",
            body="Body",
            clock=fixed_clock,
        )
        self.assertIs(receipt.state, DeliveryState.CONFIRMED)
        self.assertEqual(receipt.receipt_id, key)
        self.assertEqual(receipt.attempts, 1)
        self.assertEqual(receipt.confirmed_at, "2026-01-01T00:00:00Z")
        self.assertEqual(ledger.pending(), ())

    def test_local_file_sink_writes_and_reconciles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "out"
            sink = LocalFileSink(directory, clock=fixed_clock)
            key = a_key()
            self.assertIsNone(sink.reconcile(key))
            ack = sink.deliver(key, "Title", "Body")
            self.assertTrue(ack.positive)
            self.assertEqual(ack.ack_id, key)
            files = md_files(directory)
            self.assertEqual(len(files), 1)
            self.assertIn("Body", files[0].read_text(encoding="utf-8"))
            found = sink.reconcile(key)
            self.assertIsNotNone(found)
            self.assertTrue(found.positive)
            self.assertEqual(found.ack_id, key)


class FalsePositiveTests(unittest.TestCase):
    def test_positive_ack_without_receipt_id_is_uncertain(self) -> None:
        ledger = DeliveryLedger(clock=fixed_clock)
        sink = MemorySink(deliver_ack=SinkAck(ack_id=None, positive=True))
        receipt = deliver(
            ledger,
            sink,
            key=a_key(),
            destination_id=DESTINATION_ID,
            title="Title",
            body="Body",
            clock=fixed_clock,
        )
        self.assertIs(receipt.state, DeliveryState.UNCERTAIN)
        self.assertIsNone(receipt.receipt_id)
        self.assertEqual(receipt.attempts, 1)


class UncertaintyTests(unittest.TestCase):
    def test_uncertain_ack_then_successful_reconcile_no_resend(self) -> None:
        ledger = DeliveryLedger(clock=fixed_clock)
        sink = MemorySink(deliver_ack=SinkAck(ack_id=None, positive=True))
        key = a_key()
        first = deliver(
            ledger,
            sink,
            key=key,
            destination_id=DESTINATION_ID,
            title="Title",
            body="Body",
            clock=fixed_clock,
        )
        self.assertIs(first.state, DeliveryState.UNCERTAIN)
        self.assertEqual(sink.deliver_calls, 1)

        confirmed = reconcile_delivery(ledger, sink, key=key)
        self.assertIs(confirmed.state, DeliveryState.CONFIRMED)
        self.assertEqual(confirmed.receipt_id, key)
        # The destination already had it: reconcile must not resend.
        self.assertEqual(sink.deliver_calls, 1)

    def test_deliver_on_uncertain_without_reconcile_result_never_sends(self) -> None:
        ledger = DeliveryLedger(clock=fixed_clock)
        sink = MemorySink(
            deliver_ack=SinkAck(ack_id=None, positive=True),
            fail_reconcile=True,
        )
        key = a_key()
        first = deliver(
            ledger,
            sink,
            key=key,
            destination_id=DESTINATION_ID,
            title="Title",
            body="Body",
            clock=fixed_clock,
        )
        self.assertIs(first.state, DeliveryState.UNCERTAIN)
        self.assertEqual(sink.deliver_calls, 1)

        # Reconcile cannot answer, so the ambiguous key must not be resent and
        # must never appear to be SENDING.
        again = deliver(
            ledger,
            sink,
            key=key,
            destination_id=DESTINATION_ID,
            title="Title",
            body="Body",
            clock=fixed_clock,
        )
        self.assertIs(again.state, DeliveryState.UNCERTAIN)
        self.assertEqual(sink.deliver_calls, 1)

    def test_uncertain_without_artifact_resends_exactly_once(self) -> None:
        ledger = DeliveryLedger(clock=fixed_clock)
        key = a_key()
        ledger.prepare(key, DESTINATION_ID, clock=fixed_clock)
        ledger.mark_sending(key, clock=fixed_clock)
        ledger.mark_uncertain(key, "answer lost before any write", clock=fixed_clock)

        sink = MemorySink()  # empty destination: reconcile definitively absent
        receipt = deliver(
            ledger,
            sink,
            key=key,
            destination_id=DESTINATION_ID,
            title="Title",
            body="Body",
            clock=fixed_clock,
        )
        self.assertIs(receipt.state, DeliveryState.CONFIRMED)
        self.assertEqual(sink.deliver_calls, 1)
        self.assertEqual(receipt.attempts, 2)


class CrashSafetyTests(unittest.TestCase):
    def _sink_and_dir(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name) / "out"
        return LocalFileSink(directory, clock=fixed_clock), directory

    def test_crash_at_prepared_resumes_without_duplicate(self) -> None:
        key = a_key()
        ledger = DeliveryLedger(clock=fixed_clock)
        ledger.prepare(key, DESTINATION_ID, clock=fixed_clock)
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "delivery.json"
            persist(state_path, ledger)
            reloaded = load_ledger(read_state(state_path))
            self.assertIs(reloaded.get(key).state, DeliveryState.PREPARED)

            sink, directory = self._sink_and_dir()
            receipt = deliver(
                reloaded,
                sink,
                key=key,
                destination_id=DESTINATION_ID,
                title="Title",
                body="Body",
                clock=fixed_clock,
            )
            self.assertIs(receipt.state, DeliveryState.CONFIRMED)
            self.assertEqual(receipt.attempts, 1)
            self.assertEqual(len(md_files(directory)), 1)

    def test_crash_at_sending_reconciles_without_duplicate(self) -> None:
        key = a_key()
        sink, directory = self._sink_and_dir()
        ledger = DeliveryLedger(clock=fixed_clock)
        ledger.prepare(key, DESTINATION_ID, clock=fixed_clock)
        ledger.mark_sending(key, clock=fixed_clock)
        sink.deliver(key, "Title", "Body")  # artifact written, answer lost
        self.assertEqual(len(md_files(directory)), 1)

        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "delivery.json"
            persist(state_path, ledger)
            reloaded = load_ledger(read_state(state_path))
            self.assertIs(reloaded.get(key).state, DeliveryState.SENDING)

            receipt = deliver(
                reloaded,
                sink,
                key=key,
                destination_id=DESTINATION_ID,
                title="Title",
                body="Body",
                clock=fixed_clock,
            )
            self.assertIs(receipt.state, DeliveryState.CONFIRMED)
            self.assertEqual(receipt.attempts, 1)  # reconciled, never resent
            self.assertEqual(len(md_files(directory)), 1)

    def test_crash_at_uncertain_reconciles_without_duplicate(self) -> None:
        key = a_key()
        sink, directory = self._sink_and_dir()
        ledger = DeliveryLedger(clock=fixed_clock)
        ledger.prepare(key, DESTINATION_ID, clock=fixed_clock)
        ledger.mark_sending(key, clock=fixed_clock)
        sink.deliver(key, "Title", "Body")  # artifact written, answer lost
        ledger.mark_uncertain(key, "ack lost", clock=fixed_clock)

        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "delivery.json"
            persist(state_path, ledger)
            reloaded = load_ledger(read_state(state_path))
            self.assertIs(reloaded.get(key).state, DeliveryState.UNCERTAIN)

            receipt = deliver(
                reloaded,
                sink,
                key=key,
                destination_id=DESTINATION_ID,
                title="Title",
                body="Body",
                clock=fixed_clock,
            )
            self.assertIs(receipt.state, DeliveryState.CONFIRMED)
            self.assertEqual(receipt.attempts, 1)
            self.assertEqual(len(md_files(directory)), 1)


class RepeatAndRestartTests(unittest.TestCase):
    def test_repeat_delivery_of_same_key_yields_one_artifact_and_one_receipt(self) -> None:
        ledger = DeliveryLedger(clock=fixed_clock)
        sink = MemorySink()
        key = a_key()
        first = deliver(
            ledger,
            sink,
            key=key,
            destination_id=DESTINATION_ID,
            title="Title",
            body="Body",
            clock=fixed_clock,
        )
        second = deliver(
            ledger,
            sink,
            key=key,
            destination_id=DESTINATION_ID,
            title="Title",
            body="Body",
            clock=fixed_clock,
        )
        self.assertIs(first.state, DeliveryState.CONFIRMED)
        self.assertIs(second.state, DeliveryState.CONFIRMED)
        self.assertEqual(second.receipt_id, key)
        self.assertEqual(sink.deliver_calls, 1)
        self.assertEqual(len(ledger.receipts()), 1)

    def test_restart_from_persisted_ledger_behaves_identically(self) -> None:
        ledger = DeliveryLedger(clock=fixed_clock)
        sink = MemorySink()
        key = a_key()
        deliver(
            ledger,
            sink,
            key=key,
            destination_id=DESTINATION_ID,
            title="Title",
            body="Body",
            clock=fixed_clock,
        )
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "delivery.json"
            persist(state_path, ledger)
            reloaded = load_ledger(read_state(state_path))
            self.assertEqual(reloaded.as_state(), ledger.as_state())
            self.assertEqual(
                reloaded.get(key).as_dict(), ledger.get(key).as_dict()
            )
            again = deliver(
                reloaded,
                sink,
                key=key,
                destination_id=DESTINATION_ID,
                title="Title",
                body="Body",
                clock=fixed_clock,
            )
            self.assertIs(again.state, DeliveryState.CONFIRMED)
            self.assertEqual(sink.deliver_calls, 1)


class FailClosedTests(unittest.TestCase):
    def test_corrupt_ledger_file_raises_never_reads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "delivery.json"
            state_path.write_bytes(b"\xff\xfe{not json")
            with self.assertRaises(StateCorruptError):
                read_state(state_path)
            state_path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(StateCorruptError):
                read_state(state_path)

    def test_malformed_persisted_payload_raises(self) -> None:
        with self.assertRaises(DeliveryStateError):
            load_ledger("not a mapping")
        with self.assertRaises(DeliveryStateError):
            load_ledger({"schema": "cao.automation.delivery.other", "receipts": []})
        with self.assertRaises(DeliveryStateError):
            load_ledger({"schema": DELIVERY_STATE_SCHEMA, "receipts": "nope"})
        with self.assertRaises(DeliveryStateError):
            load_ledger(
                {
                    "schema": DELIVERY_STATE_SCHEMA,
                    "receipts": [{"key": "k", "destination_id": "d", "state": "BOGUS"}],
                }
            )

    def test_bounded_capacity_refuses_new_key(self) -> None:
        ledger = DeliveryLedger(max_receipts=1, clock=fixed_clock)
        ledger.prepare("key-1", DESTINATION_ID, clock=fixed_clock)
        with self.assertRaises(DeliveryCapacityError):
            ledger.prepare("key-2", DESTINATION_ID, clock=fixed_clock)
        self.assertEqual(len(ledger.receipts()), 1)

    def test_oversized_persisted_state_is_refused(self) -> None:
        receipts = [
            {
                "key": "key-1",
                "destination_id": DESTINATION_ID,
                "state": DeliveryState.PREPARED.value,
                "attempts": 0,
                "receipt_id": None,
                "reason": None,
                "prepared_at": "2026-01-01T00:00:00Z",
                "sent_at": None,
                "confirmed_at": None,
            },
            {
                "key": "key-2",
                "destination_id": DESTINATION_ID,
                "state": DeliveryState.PREPARED.value,
                "attempts": 0,
                "receipt_id": None,
                "reason": None,
                "prepared_at": "2026-01-01T00:00:00Z",
                "sent_at": None,
                "confirmed_at": None,
            },
        ]
        payload = {"schema": DELIVERY_STATE_SCHEMA, "receipts": receipts}
        with self.assertRaises(DeliveryCapacityError):
            load_ledger(payload, max_receipts=1)


class TerminalStateTests(unittest.TestCase):
    def test_held_is_terminal_until_fresh_prepare(self) -> None:
        ledger = DeliveryLedger(clock=fixed_clock)
        sink = MemorySink()
        key = a_key()
        ledger.prepare(key, DESTINATION_ID, clock=fixed_clock)
        held = ledger.hold(key, "operator paused", clock=fixed_clock)
        self.assertIs(held.state, DeliveryState.HELD)

        receipt = deliver(
            ledger,
            sink,
            key=key,
            destination_id=DESTINATION_ID,
            title="Title",
            body="Body",
            clock=fixed_clock,
        )
        self.assertIs(receipt.state, DeliveryState.HELD)
        self.assertEqual(sink.deliver_calls, 0)
        # No incidental operation moves a held receipt.
        self.assertIs(ledger.mark_sending(key, clock=fixed_clock).state, DeliveryState.HELD)
        self.assertIs(ledger.confirm(key, "ack", clock=fixed_clock).state, DeliveryState.HELD)
        self.assertIs(
            ledger.mark_uncertain(key, "noise", clock=fixed_clock).state,
            DeliveryState.HELD,
        )
        self.assertIs(ledger.hold(key, "still held", clock=fixed_clock).state, DeliveryState.HELD)

        fresh = ledger.prepare(key, DESTINATION_ID, clock=fixed_clock)
        self.assertIs(fresh.state, DeliveryState.PREPARED)
        confirmed = deliver(
            ledger,
            sink,
            key=key,
            destination_id=DESTINATION_ID,
            title="Title",
            body="Body",
            clock=fixed_clock,
        )
        self.assertIs(confirmed.state, DeliveryState.CONFIRMED)

    def test_destination_identity_mismatch_refused(self) -> None:
        ledger = DeliveryLedger(clock=fixed_clock)
        sink = MemorySink()
        key = a_key()
        ledger.prepare(key, DESTINATION_ID, clock=fixed_clock)
        with self.assertRaises(DeliveryMismatchError):
            deliver(
                ledger,
                sink,
                key=key,
                destination_id="somewhere-else",
                title="Title",
                body="Body",
                clock=fixed_clock,
            )
        with self.assertRaises(DeliveryMismatchError):
            ledger.prepare(key, "somewhere-else", clock=fixed_clock)
        self.assertEqual(sink.deliver_calls, 0)

    def test_unknown_key_reconcile_raises(self) -> None:
        ledger = DeliveryLedger(clock=fixed_clock)
        with self.assertRaises(DeliveryStateError):
            reconcile_delivery(ledger, MemorySink(), key="missing")


class BoundaryTests(unittest.TestCase):
    def test_module_imports_no_forbidden_network_or_process_path(self) -> None:
        module = importlib.import_module("cao.delivery")
        source = Path(module.__file__).read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = {
            "http",
            "urllib",
            "socket",
            "smtplib",
            "ssl",
            "ftplib",
            "asyncio",
            "requests",
            "telnetlib",
            "xmlrpc",
            "poplib",
            "imaplib",
            "webbrowser",
            "subprocess",
            "multiprocessing",
        }
        self.assertEqual(imported & forbidden, set())
        # No environment read anywhere in the module.
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)

    def test_module_does_not_reimplement_the_internal_relay(self) -> None:
        module = importlib.import_module("cao.delivery")
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        defined: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
        # It must not import the internal relay or reimplement its type.
        self.assertNotIn("cao.relay", imported)
        self.assertNotIn(".relay", imported)
        self.assertNotIn("ProgressRelay", defined)

    def test_progress_relay_records_identity_before_the_wake(self) -> None:
        from cao.relay import ProgressRelay, RelayVerdict

        def failing_wake(_wake):
            raise RuntimeError("manager channel failed after recording")

        relay = ProgressRelay(sink=failing_wake, clock=fixed_clock)
        state = {"scope": ["src/a.py"], "revision": "rev-1", "status": "RUNNING"}
        result = relay.check(lambda: state, writer_id="w-1")
        self.assertIs(result.verdict, RelayVerdict.UNAVAILABLE)
        self.assertIsNotNone(result.identity)
        # The identity is durable even though the wake failed, so the same
        # event can never wake a manager twice.
        self.assertTrue(relay.ledger.seen(result.identity))


class LedgerUnitTests(unittest.TestCase):
    def test_receipt_round_trips_through_state(self) -> None:
        receipt = DeliveryReceipt(
            key="key-1",
            destination_id=DESTINATION_ID,
            state=DeliveryState.UNCERTAIN,
            attempts=2,
            receipt_id=None,
            reason="ack lost",
            prepared_at="2026-01-01T00:00:00Z",
            sent_at="2026-01-01T00:00:01Z",
            confirmed_at=None,
        )
        ledger = DeliveryLedger(receipts=[receipt], clock=fixed_clock)
        restored = load_ledger(ledger.as_state())
        self.assertEqual(restored.get("key-1"), receipt)

    def test_pending_lists_only_unresolved_receipts(self) -> None:
        ledger = DeliveryLedger(clock=fixed_clock)
        ledger.prepare("p", DESTINATION_ID, clock=fixed_clock)
        ledger.prepare("s", DESTINATION_ID, clock=fixed_clock)
        ledger.mark_sending("s", clock=fixed_clock)
        ledger.prepare("c", DESTINATION_ID, clock=fixed_clock)
        ledger.confirm("c", "ack", clock=fixed_clock)
        ledger.prepare("h", DESTINATION_ID, clock=fixed_clock)
        ledger.hold("h", "paused", clock=fixed_clock)
        pending = {receipt.key for receipt in ledger.pending()}
        self.assertEqual(pending, {"p", "s"})


if __name__ == "__main__":
    unittest.main()
