"""Evidence semantics: verdicts, fail-closed gating, fingerprints, coverage."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from cao.evidence import (
    Evidence,
    EvidenceError,
    EvidenceSource,
    EvidenceUnavailable,
    Verdict,
    build_evidence,
    canonical_json,
    fingerprint,
    fingerprint_bytes,
    utc_now,
    validate_coverage,
)


class FingerprintTests(unittest.TestCase):
    def test_canonical_json_is_order_independent(self) -> None:
        self.assertEqual(canonical_json({"b": 1, "a": 2}), '{"a":2,"b":1}')

    def test_fingerprint_is_deterministic(self) -> None:
        self.assertEqual(fingerprint({"a": 1}), fingerprint({"a": 1}))

    def test_fingerprint_changes_with_content(self) -> None:
        self.assertNotEqual(fingerprint({"a": 1}), fingerprint({"a": 2}))

    def test_fingerprint_prefixes_algorithm(self) -> None:
        self.assertTrue(fingerprint({"a": 1}).startswith("sha256:"))
        self.assertEqual(
            fingerprint_bytes(b"abc"),
            "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )


class CoverageTests(unittest.TestCase):
    def test_valid_coverage_passes_through(self) -> None:
        self.assertEqual(validate_coverage(0.5), 0.5)
        self.assertIsNone(validate_coverage(None))

    def test_out_of_range_coverage_rejected(self) -> None:
        for bad in (-0.01, 1.01, 2):
            with self.assertRaises(EvidenceError):
                validate_coverage(bad)

    def test_non_numeric_coverage_rejected(self) -> None:
        with self.assertRaises(EvidenceError):
            validate_coverage("half")  # type: ignore[arg-type]
        with self.assertRaises(EvidenceError):
            validate_coverage(True)  # type: ignore[arg-type]

    def test_evidence_rejects_invalid_coverage(self) -> None:
        with self.assertRaises(EvidenceError):
            Evidence(EvidenceSource.PLUGIN, Verdict.SUPPORTED, coverage=1.5)


class UtcNowTests(unittest.TestCase):
    def test_uses_injected_clock(self) -> None:
        fixed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.assertEqual(utc_now(lambda: fixed), "2026-01-02T03:04:05Z")

    def test_naive_clock_treated_as_utc(self) -> None:
        self.assertEqual(
            utc_now(lambda: datetime(2026, 1, 2, 3, 4, 5)), "2026-01-02T03:04:05Z"
        )


class FailClosedTests(unittest.TestCase):
    def test_require_supported_accepts_supported(self) -> None:
        evidence = build_evidence(EvidenceSource.INTERNAL, Verdict.SUPPORTED)
        self.assertIs(evidence.require_supported(), evidence)

    def test_require_supported_rejects_every_other_verdict(self) -> None:
        for verdict in (
            Verdict.UNAVAILABLE,
            Verdict.UNKNOWN,
            Verdict.REFUSED,
            Verdict.INVALID,
        ):
            evidence = build_evidence(
                EvidenceSource.INTERNAL, verdict, reason="nope"
            )
            with self.assertRaises(EvidenceUnavailable) as ctx:
                evidence.require_supported()
            self.assertIn("nope", str(ctx.exception))

    def test_supported_property(self) -> None:
        self.assertTrue(
            build_evidence(EvidenceSource.INTERNAL, Verdict.SUPPORTED).supported
        )
        self.assertFalse(
            build_evidence(EvidenceSource.INTERNAL, Verdict.UNKNOWN).supported
        )


class EvidenceShapeTests(unittest.TestCase):
    def test_all_required_fields_present(self) -> None:
        evidence = build_evidence(
            EvidenceSource.PLUGIN,
            Verdict.SUPPORTED,
            reason="ok",
            fingerprint_value="sha256:abc",
            coverage=0.5,
            capture_time="2026-01-01T00:00:00Z",
        )
        self.assertEqual(
            set(evidence.as_dict()),
            {
                "source",
                "verdict",
                "capture_time",
                "fingerprint",
                "coverage",
                "reason",
            },
        )

    def test_build_evidence_stamps_capture_time_from_clock(self) -> None:
        evidence = build_evidence(
            EvidenceSource.INTERNAL,
            Verdict.UNKNOWN,
            clock=lambda: datetime(2026, 5, 6, 7, 8, 9, tzinfo=timezone.utc),
        )
        self.assertEqual(evidence.capture_time, "2026-05-06T07:08:09Z")

    def test_capture_time_is_never_implicit_positive(self) -> None:
        # Missing source data yields None, not a fabricated value.
        evidence = build_evidence(EvidenceSource.INTERNAL, Verdict.UNKNOWN)
        self.assertIsNone(evidence.fingerprint)
        self.assertIsNone(evidence.coverage)

    def test_to_json_round_trips(self) -> None:
        import json

        evidence = build_evidence(
            EvidenceSource.POLICY,
            Verdict.REFUSED,
            reason="denied",
            fingerprint_value="sha256:deadbeef",
        )
        self.assertEqual(json.loads(evidence.to_json()), evidence.as_dict())


if __name__ == "__main__":
    unittest.main()
