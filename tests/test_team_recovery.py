"""Pure R3 recovery-classifier contract tests.

The classifier is deterministic over a fixed evidence snapshot: it performs no
I/O and can start nothing. These tests pin every classification, the single
automatic READ resume and each gate that must hold, plus idempotent decision
fingerprints. No live runtime, registry or network is involved.
"""
from __future__ import annotations

import unittest

from cao import team_recovery

A = "a" * 40
B = "b" * 40
IDENT = "ct-squad-inspect-work-0-0123456789"


def snapshot(**over):
    """A fully proven, boundedly resumable READ evidence baseline."""

    evidence = {
        "task_id": "inspect",
        "mode": "READ",
        "phase": "work",
        "status": "unknown",
        "error": "TEAM_RESULT_UNREADABLE",
        "execution_id": IDENT,
        "prompt_present": True,
        "head": None,
        "pinned_head": A,
        "pinned_branch": "main",
        "observed_head": A,
        "observed_branch": "main",
        "durable_claim": {"claim_id": 7, "released": False, "mode": "READ"},
        "registry_claim": {
            "claim_id": 7, "released": False, "mode": "READ",
            "owner": "cao-product", "run_id": "cao-team-squad",
            "work_key": IDENT,
        },
        "registry_unreadable": False,
        "checkout_conflict": False,
        "worker_present": True,
        "runtime_available": True,
        "agent_checkpoint": True,
        "receipt": "MISSING",
        "claim_id": 7,
        "prior_resumes": 0,
        "expected_owner": "cao-product",
        "expected_run_id": "cao-team-squad",
        "expected_work_key": IDENT,
        "now": "2026-01-01T00:00:00Z",
    }
    evidence.update(over)
    return evidence


class BaselineTests(unittest.TestCase):
    def test_schema_and_vocabulary_are_explicit(self):
        self.assertEqual(team_recovery.SCHEMA, "cao.team.recovery.v1")
        self.assertEqual(len(team_recovery.CLASSIFICATIONS),
                         len(set(team_recovery.CLASSIFICATIONS)))
        self.assertEqual(len(team_recovery.DECISIONS),
                         len(set(team_recovery.DECISIONS)))

    def test_proven_resume_is_the_only_automatic_transition(self):
        decision = team_recovery.classify(snapshot())
        self.assertEqual(decision["classification"], team_recovery.READ_TRANSPORT)
        self.assertEqual(decision["decision"], team_recovery.RESUME_READ)
        self.assertTrue(decision["auto_resume"])
        self.assertEqual(decision["execution_id"], IDENT)
        self.assertEqual(decision["next_action"], "resume_same_read_execution")
        provenance = decision["provenance"]
        # The bounded provenance the R3 receipt must carry.
        for key in ("task_id", "source_head", "claim_id", "nonce_state", "timestamp"):
            self.assertIn(key, provenance)
        self.assertEqual(provenance["task_id"], "inspect")
        self.assertEqual(provenance["source_head"], A)
        self.assertEqual(provenance["claim_id"], 7)
        self.assertEqual(provenance["nonce_state"], "MISSING")

    def test_fingerprint_is_stable_across_timestamps(self):
        first = team_recovery.classify(snapshot(now="2026-01-01T00:00:00Z"))
        later = team_recovery.classify(snapshot(now="2026-06-01T00:00:00Z"))
        self.assertEqual(team_recovery.fingerprint(first),
                         team_recovery.fingerprint(later))


class ClassificationTests(unittest.TestCase):
    def test_terminal_and_running_are_continued(self):
        terminal = team_recovery.classify(snapshot(status="complete"))
        self.assertEqual((terminal["classification"], terminal["decision"]),
                         (team_recovery.TERMINAL, team_recovery.CONTINUE))
        running = team_recovery.classify(snapshot(status="running", error=None))
        self.assertEqual((running["classification"], running["decision"]),
                         (team_recovery.RUNNING_PROGRESS, team_recovery.CONTINUE))
        self.assertFalse(running["auto_resume"])

    def test_lost_terminal_receipt_holds(self):
        for code in ("TEAM_RESULT_UNVERIFIED", "TEAM_DURABLE_RESULT_UNVERIFIED"):
            with self.subTest(code=code):
                decision = team_recovery.classify(snapshot(error=code))
                self.assertEqual(decision["classification"],
                                 team_recovery.TERMINAL_RECEIPT_MISSING)
                self.assertEqual(decision["decision"], team_recovery.HOLD)
                self.assertFalse(decision["auto_resume"])

    def test_a_verified_receipt_is_never_resumed(self):
        decision = team_recovery.classify(snapshot(receipt="VERIFIED"))
        self.assertEqual(decision["decision"], team_recovery.HOLD)
        self.assertFalse(decision["auto_resume"])

    def test_interactive_gate_holds(self):
        for code in ("TEAM_AGENT_BLOCKED", "PRODUCT_TASK_UNAVAILABLE",
                     "PROVIDER_DENIED", "MODEL_UNAVAILABLE",
                     "TEAM_MODEL_NOT_ALLOWED"):
            with self.subTest(code=code):
                decision = team_recovery.classify(snapshot(error=code))
                self.assertEqual(decision["classification"],
                                 team_recovery.INTERACTIVE_GATE)
                self.assertEqual(decision["decision"], team_recovery.HOLD)

    def test_security_and_tool_denial_blocks(self):
        for code in ("TEAM_TOOL_DENIED", "TASK_PERMISSION_DENIED",
                     "TASK_PLATFORM_TOOL_DENIAL", "TOOL_REFUSED",
                     "SANDBOX_ESCAPE_DENIED"):
            with self.subTest(code=code):
                decision = team_recovery.classify(snapshot(error=code))
                self.assertEqual(decision["classification"],
                                 team_recovery.SECURITY_FAILURE)
                self.assertEqual(decision["decision"], team_recovery.BLOCK)
                self.assertFalse(decision["auto_resume"])

    def test_git_drift_holds(self):
        head = team_recovery.classify(snapshot(observed_head=B))
        self.assertEqual((head["classification"], head["decision"]),
                         (team_recovery.GIT_DRIFT, team_recovery.HOLD))
        branch = team_recovery.classify(snapshot(observed_branch="release"))
        self.assertEqual(branch["classification"], team_recovery.GIT_DRIFT)
        self.assertFalse(branch["auto_resume"])

    def test_registry_conflict_holds(self):
        released = team_recovery.classify(snapshot(
            registry_claim={"claim_id": 7, "released": True, "mode": "READ",
                            "owner": "cao-product", "run_id": "cao-team-squad",
                            "work_key": IDENT}))
        self.assertEqual(released["classification"], team_recovery.REGISTRY_CONFLICT)
        mismatched = team_recovery.classify(snapshot(
            registry_claim={"claim_id": 9, "released": False, "mode": "READ",
                            "owner": "cao-product", "run_id": "cao-team-squad",
                            "work_key": IDENT}))
        self.assertEqual(mismatched["classification"], team_recovery.REGISTRY_CONFLICT)
        foreign = team_recovery.classify(snapshot(
            registry_claim={"claim_id": 7, "released": False, "mode": "READ",
                            "owner": "another", "run_id": "cao-team-squad",
                            "work_key": IDENT}))
        self.assertEqual(foreign["classification"], team_recovery.REGISTRY_CONFLICT)
        conflict = team_recovery.classify(snapshot(checkout_conflict=True))
        self.assertEqual(conflict["classification"], team_recovery.REGISTRY_CONFLICT)
        unreadable = team_recovery.classify(snapshot(registry_unreadable=True))
        self.assertEqual(unreadable["classification"], team_recovery.REGISTRY_CONFLICT)

    def test_write_is_never_automatically_resumed(self):
        unknown = team_recovery.classify(snapshot(
            mode="WRITE", error="TASK_WRITE_RESULT_UNVERIFIED"))
        self.assertEqual(unknown["classification"], team_recovery.WRITE_UNVERIFIED)
        self.assertEqual(unknown["decision"], team_recovery.HOLD)
        self.assertFalse(unknown["auto_resume"])
        committed = team_recovery.classify(snapshot(
            mode="WRITE", error="TASK_RELEASE_UNVERIFIED", head=A))
        self.assertEqual(committed["classification"], team_recovery.WRITE_UNVERIFIED)
        self.assertFalse(committed["auto_resume"])

    def test_pending_pr_ci_is_observed(self):
        decision = team_recovery.classify(snapshot(status="running", error=None,
                                                   pr_ci="PENDING"))
        self.assertEqual(decision["classification"], team_recovery.PR_CI_PENDING)
        self.assertEqual(decision["decision"], team_recovery.OBSERVE)
        self.assertFalse(decision["auto_resume"])


class ResumeGateTests(unittest.TestCase):
    def test_resume_budget_is_single_attempt(self):
        decision = team_recovery.classify(snapshot(prior_resumes=1))
        self.assertEqual(decision["classification"], team_recovery.READ_TRANSPORT)
        self.assertEqual(decision["decision"], team_recovery.HOLD)
        self.assertFalse(decision["auto_resume"])

    def test_missing_original_identity_or_prompt_holds(self):
        self.assertEqual(team_recovery.classify(snapshot(execution_id=None))["decision"],
                         team_recovery.HOLD)
        self.assertEqual(team_recovery.classify(snapshot(prompt_present=False))["decision"],
                         team_recovery.HOLD)

    def test_missing_runtime_or_worker_holds(self):
        self.assertEqual(team_recovery.classify(snapshot(runtime_available=False))["decision"],
                         team_recovery.HOLD)
        self.assertEqual(team_recovery.classify(snapshot(worker_present=False))["decision"],
                         team_recovery.HOLD)

    def test_missing_agent_checkpoint_holds(self):
        decision = team_recovery.classify(snapshot(agent_checkpoint=False))
        self.assertEqual(decision["decision"], team_recovery.HOLD)

    def test_unverifiable_pin_holds(self):
        self.assertEqual(team_recovery.classify(
            snapshot(pinned_head=None))["classification"], team_recovery.GIT_DRIFT)
        self.assertEqual(team_recovery.classify(
            snapshot(pinned_branch=None))["classification"], team_recovery.GIT_DRIFT)
        self.assertEqual(team_recovery.classify(
            snapshot(observed_head=None))["classification"], team_recovery.GIT_DRIFT)

    def test_unknown_failure_class_holds(self):
        decision = team_recovery.classify(snapshot(error="TEAM_PROMPT_INVALID"))
        self.assertEqual(decision["classification"], team_recovery.INTERACTIVE_GATE)
        decision = team_recovery.classify(snapshot(error="SOMETHING_ELSE"))
        self.assertEqual(decision["decision"], team_recovery.HOLD)
        self.assertFalse(decision["auto_resume"])

    def test_review_phase_is_also_resumable(self):
        decision = team_recovery.classify(snapshot(phase="review", head=A,
                                                   mode="READ"))
        self.assertEqual(decision["decision"], team_recovery.RESUME_READ)

    def test_non_read_mode_never_automatic(self):
        decision = team_recovery.classify(snapshot(mode="write",
                                                   error="TEAM_RESULT_UNREADABLE"))
        self.assertEqual(decision["decision"], team_recovery.HOLD)
        self.assertFalse(decision["auto_resume"])


if __name__ == "__main__":
    unittest.main()
