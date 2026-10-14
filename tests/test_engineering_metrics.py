"""Pure R4 engineering SLA/exception metric tests.

The module is deterministic over a fixed evidence snapshot and performs no I/O,
no send, no merge and no persistence. These tests pin every derived metric and
every exception threshold, prove that an unmeasurable interval never claims a
breach, and prove the report is idempotent across repeated ticks. No live
GitHub, runtime, evaluator or network is involved.
"""
from __future__ import annotations

import unittest

from cao import engineering_metrics as m

HEAD = "b" * 40
EVAL = "c" * 64
BASE = "2026-01-01T00:00:00Z"


def tick(**over):
    value = {
        "watch_id": "sample-loop", "status": "READY_TO_ARM", "head": HEAD,
        "evaluation_sha256": EVAL, "first_seen": BASE, "observed_at": BASE,
        "merge_requested": False,
    }
    value.update(over)
    return value


def codes(derived):
    return sorted(e["code"] for e in derived["exceptions"])


class EngineeringWaitTests(unittest.TestCase):
    def test_healthy_plan_is_not_an_exception(self):
        derived = m.annotate(tick(status="READY_TO_ARM"))
        self.assertEqual(derived["exceptions"], [])
        self.assertIsNone(derived["metrics"]["wait"])
        self.assertEqual(derived["metrics"]["age_seconds"], 0.0)

    def test_review_latency_breach_only_past_the_sla(self):
        under = m.annotate(tick(status="INDEPENDENT_REVIEW_REQUIRED",
                                observed_at="2026-01-01T05:59:59Z"))
        self.assertEqual(under["exceptions"], [])
        edge = m.annotate(tick(status="INDEPENDENT_REVIEW_REQUIRED",
                               observed_at="2026-01-01T06:00:00Z"))
        self.assertEqual(edge["exceptions"], [])
        over = m.annotate(tick(status="INDEPENDENT_REVIEW_REQUIRED",
                               observed_at="2026-01-01T06:00:01Z"))
        self.assertEqual(codes(over), [m.REVIEW_LATENCY_EXCEEDED])
        self.assertEqual(over["metrics"]["sla_seconds"], m.REVIEW_SLA_SECONDS)
        self.assertEqual(over["metrics"]["head"], HEAD)

    def test_ci_pending_and_failure_share_the_exact_head_gate(self):
        for status in ("CI_NOT_GREEN_FOR_HEAD",):
            with self.subTest(status=status):
                fresh = m.annotate(tick(status=status))
                self.assertEqual(fresh["metrics"]["wait"], "ci")
                self.assertEqual(fresh["exceptions"], [])
                stale = m.annotate(tick(status=status,
                                        observed_at="2026-01-01T04:00:00Z"))
                self.assertEqual(codes(stale), [m.PR_CI_WAIT_EXCEEDED])

    def test_evaluation_wait_covers_a_missing_or_unimproved_measurement(self):
        stale = m.annotate(tick(status="NEEDS_IMPROVEMENT",
                                observed_at="2026-01-01T04:00:00Z"))
        self.assertEqual(codes(stale), [m.EVALUATION_WAIT_EXCEEDED])
        self.assertEqual(stale["metrics"]["wait"], "evaluation")

    def test_operator_grant_wait(self):
        fresh = m.annotate(tick(status="AWAITING_OPERATOR_APPROVAL",
                                observed_at="2026-01-01T23:00:00Z"))
        self.assertEqual(fresh["exceptions"], [])
        stale = m.annotate(tick(status="AWAITING_OPERATOR_APPROVAL",
                                observed_at="2026-01-02T00:00:01Z"))
        self.assertEqual(codes(stale), [m.OPERATOR_GRANT_WAIT_EXCEEDED])

    def test_ambiguous_submission_is_always_an_exception(self):
        derived = m.annotate(tick(status="AWAITING_GITHUB_RECONCILIATION"))
        self.assertEqual(codes(derived), [m.GITHUB_RECONCILIATION_PENDING])

    def test_refused_source_gates_are_reported_immediately(self):
        for status in ("PR_SOURCE_MISMATCH", "SCOPE_MISMATCH",
                       "BRANCH_RULES_NOT_VERIFIED",
                       "SEPARATE_OPERATOR_IDENTITY_REQUIRED"):
            with self.subTest(status=status):
                derived = m.annotate(tick(status=status))
                self.assertEqual(codes(derived), [m.SOURCE_GATE_HELD])
                self.assertIsNone(derived["metrics"]["wait"])

    def test_unmeasurable_or_future_interval_never_claims_a_breach(self):
        missing = m.annotate(tick(status="INDEPENDENT_REVIEW_REQUIRED",
                                  first_seen=None))
        self.assertIsNone(missing["metrics"]["age_seconds"])
        self.assertEqual(missing["exceptions"], [])
        future = m.annotate(tick(status="INDEPENDENT_REVIEW_REQUIRED",
                                 first_seen="2026-06-01T00:00:00Z"))
        self.assertIsNone(future["metrics"]["age_seconds"])
        self.assertEqual(future["exceptions"], [])

    def test_repeated_tick_is_identical_and_duplicate_free(self):
        first = m.annotate(tick(status="INDEPENDENT_REVIEW_REQUIRED",
                                observed_at="2026-01-01T12:00:00Z"))
        second = m.annotate(tick(status="INDEPENDENT_REVIEW_REQUIRED",
                                 observed_at="2026-01-01T12:00:00Z"))
        self.assertEqual(first, second)
        self.assertEqual(len(first["exceptions"]), 1)


class TeamDecisionTests(unittest.TestCase):
    def test_lost_terminal_receipt_reports_an_exception(self):
        derived = m.team([
            {"task_id": "inspect", "classification": "TERMINAL_RECEIPT_MISSING",
             "decision": "HOLD", "reason": "the terminal receipt is absent"},
        ])
        self.assertEqual(codes(derived), [m.TERMINAL_RECEIPT_LOSS])
        self.assertEqual(derived["metrics"]["held_decisions"], 1)
        self.assertEqual(derived["exceptions"][0]["subject"], "inspect")

    def test_resume_is_counted_as_a_retry_decision(self):
        derived = m.team([
            {"task_id": "read", "classification": "READ_TRANSPORT",
             "decision": "RESUME_READ", "auto_resume": True},
        ])
        self.assertEqual(derived["metrics"]["retry_decisions"], 1)
        self.assertEqual(derived["exceptions"], [])

    def test_spent_resume_budget_is_an_exception(self):
        derived = m.team([
            {"task_id": "read", "classification": "READ_TRANSPORT",
             "decision": "HOLD",
             "reason": "the bounded resume budget for this identity is spent"},
        ])
        self.assertEqual(codes(derived), [m.RETRY_BUDGET_SPENT])

    def test_refused_provider_route_is_reported(self):
        derived = m.team([
            {"task_id": "route", "classification": "INTERACTIVE_GATE",
             "decision": "HOLD", "reason": "provider gate"},
        ])
        self.assertEqual(codes(derived), [m.PROVIDER_ROUTE_HELD])

    def test_security_registry_and_git_holds_are_reported(self):
        derived = m.team([
            {"task_id": a, "classification": a, "decision": "HOLD"}
            for a in ("SECURITY_FAILURE", "REGISTRY_CONFLICT", "GIT_DRIFT",
                      "WRITE_COMMITTED_BUT_UNVERIFIED")
        ])
        self.assertEqual(codes(derived), [m.HELD_EVIDENCE] * 4)

    def test_running_progress_is_not_an_exception(self):
        derived = m.team([
            {"task_id": "live", "classification": "RUNNING_PROGRESS",
             "decision": "CONTINUE"},
        ])
        self.assertEqual(derived["exceptions"], [])
        self.assertEqual(derived["metrics"]["held_decisions"], 0)


class ClaimProvenanceTests(unittest.TestCase):
    def test_a_complete_task_needs_a_released_head(self):
        proven = m.claims([
            {"task_id": "a", "status": "complete",
             "claim": {"released": True, "outcome": "complete",
                       "released_head": HEAD}},
        ])
        self.assertEqual(proven["exceptions"], [])
        unproven = m.claims([
            {"task_id": "b", "status": "complete",
             "claim": {"released": True, "outcome": "complete"}},
            {"task_id": "c", "status": "complete",
             "claim": {"released": False}},
        ])
        self.assertEqual(codes(unproven), [m.CLAIM_RELEASE_UNPROVEN] * 2)

    def test_abandon_release_is_bounded_and_proven(self):
        derived = m.claims([
            {"task_id": "s", "status": "stopped",
             "claim": {"released": True, "outcome": "abandon"}},
        ])
        self.assertEqual(derived["exceptions"], [])
        self.assertEqual(derived["metrics"]["released_claims"], 1)

    def test_release_without_a_bounded_outcome_is_an_exception(self):
        derived = m.claims([
            {"task_id": "w", "status": "running",
             "claim": {"released": True, "outcome": "weird"}},
        ])
        self.assertEqual(codes(derived), [m.CLAIM_RELEASE_UNPROVEN])

    def test_refused_release_is_reported_not_hidden(self):
        derived = m.claims([
            {"task_id": "r", "status": "running",
             "error": {"code": "TASK_CLAIM_RELEASE_REJECTED"}},
        ])
        self.assertEqual(codes(derived), [m.CLAIM_RELEASE_UNPROVEN])


class ReportTests(unittest.TestCase):
    def test_healthy_snapshot_is_ok_and_collapses_nothing(self):
        value = m.report(now=BASE)
        self.assertEqual(value["status"], "OK")
        self.assertEqual(value["exceptions"], [])
        self.assertEqual(value["generated_at"], BASE)
        self.assertEqual(m.summary(value),
                         {"schema": m.SCHEMA, "status": "OK", "count": 0,
                          "codes": []})

    def test_exception_report_aggregates_and_deduplicates(self):
        wait = m.annotate(tick(status="INDEPENDENT_REVIEW_REQUIRED",
                               observed_at="2026-01-02T00:00:00Z"))
        value = m.report(
            exceptions=wait["exceptions"] + wait["exceptions"],
            team_decisions=[{"task_id": "inspect",
                             "classification": "TERMINAL_RECEIPT_MISSING",
                             "decision": "HOLD"}],
            tasks=[{"task_id": "b", "status": "complete", "claim": {}}],
            now=BASE)
        self.assertEqual(value["status"], "EXCEPTIONS")
        self.assertEqual(value["exceptions"][0]["code"],
                         m.REVIEW_LATENCY_EXCEEDED)
        self.assertEqual(sorted(m.summary(value)["codes"]),
                         [m.CLAIM_RELEASE_UNPROVEN, m.REVIEW_LATENCY_EXCEEDED,
                          m.TERMINAL_RECEIPT_LOSS])
        self.assertEqual(m.summary(value)["count"], 3)

    def test_report_is_idempotent_over_unchanged_evidence(self):
        inputs = dict(
            engineering_results=[tick(status="AWAITING_GITHUB_RECONCILIATION")],
            team_decisions=[{"task_id": "i",
                             "classification": "TERMINAL_RECEIPT_MISSING",
                             "decision": "HOLD"}],
            tasks=[], now=BASE)
        self.assertEqual(m.report(**inputs), m.report(**inputs))

    def test_summary_never_leaks_subject_or_detail(self):
        value = m.report(team_decisions=[
            {"task_id": "private-task", "classification": "GIT_DRIFT",
             "decision": "HOLD", "reason": "private-reason"}], now=BASE)
        view = m.summary(value)
        self.assertEqual(set(view), {"schema", "status", "count", "codes"})
        self.assertNotIn("private-task", repr(view))

    def test_schema_and_codes_are_explicit(self):
        self.assertEqual(m.SCHEMA, "cao.engineering.metrics.v1")
        self.assertEqual(len(m.CODES), len(set(m.CODES)))


if __name__ == "__main__":
    unittest.main()
