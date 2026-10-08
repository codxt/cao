"""Policy authorization against the exact requested action."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cao.evidence import EvidenceSource, Verdict
from cao.policy import ActionKind, ActionRequest, Grant, PolicyEngine


class ExactMatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PolicyEngine(
            [Grant(ActionKind.START, "fake", "ws-1")]
        )

    def test_matching_action_is_allowed(self) -> None:
        decision = self.engine.authorize(
            ActionRequest(ActionKind.START, "fake", "ws-1")
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.evidence.verdict, Verdict.SUPPORTED)
        self.assertIsNotNone(decision.matched)

    def test_different_kind_is_refused(self) -> None:
        decision = self.engine.authorize(
            ActionRequest(ActionKind.CLOSE, "fake", "ws-1")
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.evidence.verdict, Verdict.REFUSED)
        self.assertIsNone(decision.matched)

    def test_different_workspace_is_refused(self) -> None:
        decision = self.engine.authorize(
            ActionRequest(ActionKind.START, "fake", "ws-2")
        )
        self.assertFalse(decision.allowed)

    def test_different_plugin_is_refused(self) -> None:
        decision = self.engine.authorize(
            ActionRequest(ActionKind.START, "other", "ws-1")
        )
        self.assertFalse(decision.allowed)

    def test_default_deny_with_no_grants(self) -> None:
        decision = PolicyEngine().authorize(
            ActionRequest(ActionKind.START, "fake", "ws-1")
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.evidence.verdict, Verdict.REFUSED)


class NarrowingTests(unittest.TestCase):
    def test_execution_narrowing_must_match_exactly(self) -> None:
        engine = PolicyEngine(
            [Grant(ActionKind.PROMPT, "fake", "ws-1", execution_id="exec-1")]
        )
        allowed = engine.authorize(
            ActionRequest(ActionKind.PROMPT, "fake", "ws-1", execution_id="exec-1")
        )
        refused = engine.authorize(
            ActionRequest(ActionKind.PROMPT, "fake", "ws-1", execution_id="exec-2")
        )
        missing = engine.authorize(
            ActionRequest(ActionKind.PROMPT, "fake", "ws-1")
        )
        self.assertTrue(allowed.allowed)
        self.assertFalse(refused.allowed)
        self.assertFalse(missing.allowed)

    def test_session_narrowing_must_match_exactly(self) -> None:
        engine = PolicyEngine(
            [
                Grant(
                    ActionKind.CLOSE,
                    "fake",
                    "ws-1",
                    session_id="sess-1",
                )
            ]
        )
        allowed = engine.authorize(
            ActionRequest(ActionKind.CLOSE, "fake", "ws-1", session_id="sess-1")
        )
        refused = engine.authorize(
            ActionRequest(ActionKind.CLOSE, "fake", "ws-1", session_id="sess-2")
        )
        self.assertTrue(allowed.allowed)
        self.assertFalse(refused.allowed)


class DecisionEvidenceTests(unittest.TestCase):
    def test_evidence_records_policy_source_and_fingerprint(self) -> None:
        decision = PolicyEngine(
            [Grant(ActionKind.START, "fake", "ws-1")]
        ).authorize(ActionRequest(ActionKind.START, "fake", "ws-1"))
        evidence = decision.evidence
        self.assertEqual(evidence.source, EvidenceSource.POLICY)
        self.assertIsNotNone(evidence.capture_time)
        self.assertIsNotNone(evidence.fingerprint)
        self.assertIsNotNone(evidence.reason)

    def test_decision_fingerprint_tracks_the_exact_request(self) -> None:
        engine = PolicyEngine([Grant(ActionKind.START, "fake", "ws-1")])
        first = engine.authorize(ActionRequest(ActionKind.START, "fake", "ws-1"))
        second = engine.authorize(ActionRequest(ActionKind.START, "fake", "ws-2"))
        self.assertNotEqual(
            first.evidence.fingerprint, second.evidence.fingerprint
        )


class PolicyLoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _path(self, name: str = "policy.json") -> Path:
        return self.dir / name

    def test_absent_file_is_deny_all_but_readable(self) -> None:
        engine = PolicyEngine.load(self._path())
        self.assertTrue(engine.readable)
        self.assertEqual(engine.grants, ())
        decision = engine.authorize(ActionRequest(ActionKind.START, "fake", "ws-1"))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.evidence.verdict, Verdict.REFUSED)

    def test_valid_file_authorizes(self) -> None:
        path = self._path()
        path.write_text(
            json.dumps(
                {"grants": [{"kind": "execution.start", "plugin_id": "fake", "workspace_id": "ws-1"}]}
            ),
            encoding="utf-8",
        )
        engine = PolicyEngine.load(path)
        self.assertTrue(engine.readable)
        decision = engine.authorize(ActionRequest(ActionKind.START, "fake", "ws-1"))
        self.assertTrue(decision.allowed)

    def test_corrupt_file_fails_closed(self) -> None:
        path = self._path()
        path.write_text("{not json", encoding="utf-8")
        engine = PolicyEngine.load(path)
        self.assertFalse(engine.readable)
        decision = engine.authorize(ActionRequest(ActionKind.START, "fake", "ws-1"))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.evidence.verdict, Verdict.UNKNOWN)

    def test_non_object_file_fails_closed(self) -> None:
        path = self._path()
        path.write_text("[1, 2, 3]", encoding="utf-8")
        engine = PolicyEngine.load(path)
        self.assertFalse(engine.readable)
        self.assertFalse(
            engine.authorize(ActionRequest(ActionKind.START, "fake", "ws-1")).allowed
        )

    def test_missing_grants_key_fails_closed(self) -> None:
        path = self._path()
        path.write_text(json.dumps({}), encoding="utf-8")
        engine = PolicyEngine.load(path)
        self.assertFalse(engine.readable)

    def test_unknown_kind_fails_closed(self) -> None:
        path = self._path()
        path.write_text(
            json.dumps(
                {"grants": [{"kind": "explode", "plugin_id": "fake", "workspace_id": "ws-1"}]}
            ),
            encoding="utf-8",
        )
        engine = PolicyEngine.load(path)
        self.assertFalse(engine.readable)
        self.assertFalse(
            engine.authorize(ActionRequest(ActionKind.START, "fake", "ws-1")).allowed
        )

    def test_from_mapping_does_not_raise(self) -> None:
        engine = PolicyEngine.from_mapping({"grants": "nope"})
        self.assertFalse(engine.readable)

    def test_unknown_grant_field_fails_closed(self) -> None:
        # A misspelled narrowing key must not silently broaden authority.
        engine = PolicyEngine.from_mapping(
            {
                "grants": [
                    {
                        "kind": "session.prompt",
                        "plugin_id": "fake",
                        "workspace_id": "ws-1",
                        "sessionid": "only-session",
                    }
                ]
            }
        )
        self.assertFalse(engine.readable)
        decision = engine.authorize(
            ActionRequest(
                ActionKind.PROMPT, "fake", "ws-1", session_id="another-session"
            )
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.evidence.verdict, Verdict.UNKNOWN)

    def test_unknown_grant_field_fails_closed_when_loaded(self) -> None:
        path = self._path()
        path.write_text(
            json.dumps(
                {
                    "grants": [
                        {
                            "kind": "execution.start",
                            "plugin_id": "fake",
                            "workspace_id": "ws-1",
                            "executionid": "exec-1",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        engine = PolicyEngine.load(path)
        self.assertFalse(engine.readable)
        self.assertFalse(
            engine.authorize(ActionRequest(ActionKind.START, "fake", "ws-1")).allowed
        )

    def test_valid_narrowing_fields_still_authorize(self) -> None:
        engine = PolicyEngine.from_mapping(
            {
                "grants": [
                    {
                        "kind": "session.prompt",
                        "plugin_id": "fake",
                        "workspace_id": "ws-1",
                        "session_id": "sess-1",
                    }
                ]
            }
        )
        self.assertTrue(engine.readable)
        allowed = engine.authorize(
            ActionRequest(ActionKind.PROMPT, "fake", "ws-1", session_id="sess-1")
        )
        self.assertTrue(allowed.allowed)

    def test_dangling_symlink_policy_is_unreadable(self) -> None:
        link = self._path("policy-link.json")
        link.symlink_to(self.dir / "missing-target")
        engine = PolicyEngine.load(link)
        self.assertFalse(engine.readable)
        decision = engine.authorize(
            ActionRequest(ActionKind.START, "fake", "ws-1")
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.evidence.verdict, Verdict.UNKNOWN)

    def test_invalid_utf8_policy_fails_closed_without_raising(self) -> None:
        path = self._path()
        path.write_bytes(b"\xff")
        engine = PolicyEngine.load(path)
        self.assertFalse(engine.readable)
        decision = engine.authorize(
            ActionRequest(ActionKind.START, "fake", "ws-1")
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.evidence.verdict, Verdict.UNKNOWN)

    def test_nonfinite_policy_fails_closed(self) -> None:
        path = self._path()
        path.write_text('{"grants": [], "counter": NaN}', encoding="utf-8")
        engine = PolicyEngine.load(path)
        self.assertFalse(engine.readable)


if __name__ == "__main__":
    unittest.main()
