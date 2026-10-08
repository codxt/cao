"""Durable-progress fingerprinting fails closed on hostile state."""

from __future__ import annotations

import unittest

from cao.progress import (
    ProgressAssessment,
    ProgressUnavailable,
    ProgressVerdict,
    WriterState,
    assess_progress,
    fingerprint_writer_state,
    writer_event_identity,
)


def payload(**overrides):
    state = {
        "scope": ["src/a.py", "src/b.py"],
        "revision": "rev-1",
        "status": "RUNNING",
    }
    state.update(overrides)
    return state


class WriterStateTests(unittest.TestCase):
    def test_valid_payload_normalizes(self) -> None:
        state = WriterState.from_payload(payload(metrics={"steps": 3, "files": 2}))
        self.assertEqual(state.scope, ("src/a.py", "src/b.py"))
        self.assertEqual(state.revision, "rev-1")
        self.assertEqual(state.status, "RUNNING")
        self.assertEqual(state.metrics, (("files", 2.0), ("steps", 3.0)))

    def test_empty_metrics_default(self) -> None:
        self.assertEqual(WriterState.from_payload(payload()).metrics, ())

    def test_non_mapping_payload_is_unavailable(self) -> None:
        for hostile in (None, 42, "state", ["a"], b"bytes"):
            with self.subTest(hostile=hostile):
                with self.assertRaises(ProgressUnavailable):
                    WriterState.from_payload(hostile)

    def test_scope_must_be_a_sequence_of_strings(self) -> None:
        for hostile in (None, "src/a.py", {"a": 1}, [1], [""], [None]):
            with self.subTest(scope=hostile):
                with self.assertRaises(ProgressUnavailable):
                    WriterState.from_payload(payload(scope=hostile))

    def test_missing_or_wrong_revision_and_status_rejected(self) -> None:
        for field_name in ("revision", "status"):
            for hostile in (None, "", 7, ["x"]):
                with self.subTest(field=field_name, value=hostile):
                    body = payload()
                    body[field_name] = hostile
                    with self.assertRaises(ProgressUnavailable):
                        WriterState.from_payload(body)

    def test_non_finite_or_hostile_metrics_rejected(self) -> None:
        for hostile in (
            {"steps": float("nan")},
            {"steps": float("inf")},
            {"steps": float("-inf")},
            {"steps": True},
            {"steps": "3"},
            {"steps": None},
            {"steps": [1]},
        ):
            with self.subTest(metrics=hostile):
                with self.assertRaises(ProgressUnavailable):
                    WriterState.from_payload(payload(metrics=hostile))
        with self.assertRaises(ProgressUnavailable):
            WriterState.from_payload(payload(metrics=["not", "a", "mapping"]))
        with self.assertRaises(ProgressUnavailable):
            WriterState.from_payload(payload(metrics={1: 2}))

    def test_json_reachable_huge_integer_is_unavailable(self) -> None:
        # 10**400 arrives through ordinary JSON and overflows float conversion;
        # it must fail closed rather than escape as OverflowError.
        for hostile in (10 ** 400, -(10 ** 400)):
            with self.subTest(value=hostile):
                with self.assertRaises(ProgressUnavailable):
                    WriterState.from_payload(payload(metrics={"steps": hostile}))


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_stable_and_order_independent(self) -> None:
        first = payload(metrics={"steps": 3, "files": 2})
        second = payload(metrics={"files": 2, "steps": 3})
        self.assertEqual(
            fingerprint_writer_state(first), fingerprint_writer_state(second)
        )
        self.assertTrue(fingerprint_writer_state(first).startswith("sha256:"))

    def test_each_observable_field_changes_the_fingerprint(self) -> None:
        base = fingerprint_writer_state(payload())
        variants = (
            payload(scope=["src/other.py", "src/b.py"]),
            payload(revision="rev-2"),
            payload(status="BLOCKED"),
            payload(metrics={"steps": 1}),
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(base, fingerprint_writer_state(variant))

    def test_accepts_validated_state(self) -> None:
        state = WriterState.from_payload(payload())
        self.assertEqual(
            fingerprint_writer_state(state), fingerprint_writer_state(payload())
        )

    def test_hostile_state_never_yields_a_fingerprint(self) -> None:
        with self.assertRaises(ProgressUnavailable):
            fingerprint_writer_state(payload(metrics={"steps": float("nan")}))


class AssessProgressTests(unittest.TestCase):
    def test_first_observation_is_progress(self) -> None:
        result = assess_progress(None, payload())
        self.assertIsInstance(result, ProgressAssessment)
        self.assertIs(result.verdict, ProgressVerdict.PROGRESS)
        self.assertTrue(result.changed)
        self.assertTrue(result.usable)
        self.assertIsNone(result.previous_fingerprint)

    def test_unchanged_fingerprint_is_not_progress(self) -> None:
        current = fingerprint_writer_state(payload())
        result = assess_progress(current, payload())
        self.assertIs(result.verdict, ProgressVerdict.NO_PROGRESS)
        self.assertFalse(result.changed)
        self.assertEqual(result.fingerprint, current)

    def test_changed_fingerprint_is_progress(self) -> None:
        previous = fingerprint_writer_state(payload(revision="rev-1"))
        result = assess_progress(previous, payload(revision="rev-2"))
        self.assertIs(result.verdict, ProgressVerdict.PROGRESS)
        self.assertEqual(result.previous_fingerprint, previous)

    def test_malformed_payload_is_unknown(self) -> None:
        for hostile in (None, [], "state", payload(metrics={"steps": float("nan")})):
            with self.subTest(hostile=hostile):
                result = assess_progress("sha256:prior", hostile)
                self.assertIs(result.verdict, ProgressVerdict.UNKNOWN)
                self.assertFalse(result.usable)
                self.assertFalse(result.changed)
                self.assertIsNone(result.fingerprint)

    def test_huge_integer_payload_is_unknown(self) -> None:
        # A None prior plus a JSON-reachable 10**400 metric must yield UNKNOWN,
        # not an OverflowError and not a usable fingerprint.
        result = assess_progress(None, payload(metrics={"steps": 10 ** 400}))
        self.assertIs(result.verdict, ProgressVerdict.UNKNOWN)
        self.assertFalse(result.changed)
        self.assertIsNone(result.fingerprint)

    def test_unexpected_previous_type_is_unknown(self) -> None:
        for hostile in (42, [], {}, b"x"):
            with self.subTest(previous=hostile):
                result = assess_progress(hostile, payload())
                self.assertIs(result.verdict, ProgressVerdict.UNKNOWN)
                self.assertFalse(result.changed)


class EventIdentityTests(unittest.TestCase):
    def test_identity_is_stable_and_writer_scoped(self) -> None:
        fp = fingerprint_writer_state(payload())
        first = writer_event_identity("writer-1", fp)
        self.assertEqual(first, writer_event_identity("writer-1", fp))
        self.assertNotEqual(first, writer_event_identity("writer-2", fp))

    def test_identity_rejects_bad_arguments(self) -> None:
        for writer_id, fp in (("", "sha256:x"), (None, "sha256:x"), ("w", "")):
            with self.subTest(writer_id=writer_id, fingerprint=fp):
                with self.assertRaises(ProgressUnavailable):
                    writer_event_identity(writer_id, fp)


if __name__ == "__main__":
    unittest.main()
