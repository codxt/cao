"""Evidence-driven improvement is qualified, bounded and non-authoritative."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from cao import improvement as m


ROOT = Path(__file__).resolve().parents[1]
BEFORE = json.loads((ROOT / "examples/improvement_case.json").read_text())
AFTER = json.loads((ROOT / "examples/improvement_after.json").read_text())


class InputValidationTests(unittest.TestCase):
    def setUp(self):
        self.case = copy.deepcopy(BEFORE)

    def bad(self):
        with self.assertRaises(m.ImprovementError):
            m.validate_case(self.case)

    def test_good_example_is_exactly_reconstructable(self):
        self.assertEqual(m.validate_case(self.case), self.case)

    def test_case_schema_and_extra_fields_fail_closed(self):
        self.case["schema"] = "future"
        self.bad()
        self.case = copy.deepcopy(BEFORE)
        self.case["client_secret"] = "never"
        self.bad()

    def test_empty_and_overlarge_signal_list(self):
        self.case["signals"] = []
        self.bad()
        self.case["signals"] = [copy.deepcopy(BEFORE["signals"][0])] * (m.MAX_SIGNALS + 1)
        self.bad()

    def test_duplicate_signal_and_node_identifiers(self):
        self.case["signals"][1]["id"] = self.case["signals"][0]["id"]
        self.bad()
        self.case = copy.deepcopy(BEFORE)
        self.case["context"]["nodes"][1]["id"] = "api"
        self.bad()

    def test_missing_subject_and_dangling_edges(self):
        self.case["subject"] = "nowhere"
        self.bad()
        self.case = copy.deepcopy(BEFORE)
        self.case["context"]["edges"][0]["to"] = "missing"
        self.bad()

    def test_unreferenced_evidence_is_rejected(self):
        self.case["signals"][0]["proof"]["ref"] = "file:///tmp/sensitive"
        self.bad()

    def test_invalid_fingerprint_and_weak_provenance(self):
        self.case["signals"][0]["proof"]["fingerprint"] = "sha256:deadbeef"
        self.bad()
        self.case = copy.deepcopy(BEFORE)
        self.case["signals"][0]["proof"]["verdict"] = "UNKNOWN"
        self.bad()

    def test_future_observation_does_not_turn_unknown_into_positive(self):
        self.case["signals"][0]["finding"] = "unknown"
        self.bad()
        self.case["signals"][0]["proof"]["verdict"] = "UNKNOWN"
        self.case["signals"][0].pop("threshold")
        m.validate_case(self.case)
        self.assertEqual(m.assess(self.case)["issues"][0]["status"], "CANDIDATE")

    def test_wrong_source_types_not_silently_accepted(self):
        self.case["signals"][0]["proof"]["source"] = "untrusted-root"
        self.bad()

    def test_threshold_contradiction_and_bool_numeric(self):
        self.case["signals"][0]["threshold"]["value"] = 0.001
        self.bad()
        self.case = copy.deepcopy(BEFORE)
        self.case["signals"][0]["threshold"]["value"] = True
        self.bad()

    def test_non_finite_and_wrong_metric_target(self):
        self.case["objectives"][0]["target"] = float("nan")
        self.bad()
        self.case = copy.deepcopy(BEFORE)
        self.case["objectives"][0]["target"] = "low"
        self.bad()

    def test_unknown_or_duplicate_objectives(self):
        self.case["objectives"][1]["id"] = "errors"
        self.bad()
        self.case = copy.deepcopy(BEFORE)
        self.case["objectives"][0]["resource"] = "not-in-graph"
        self.bad()

    def test_repo_and_path_must_be_source_declared(self):
        self.case["context"]["nodes"][0]["source_path"] = "src/../secrets"
        self.bad()
        self.case = copy.deepcopy(BEFORE)
        self.case["context"]["nodes"][0]["source_path"] = "/etc/secrets"
        self.bad()
        self.case = copy.deepcopy(BEFORE)
        self.case["context"]["nodes"][0]["owner_repo"] = "nothing"
        self.bad()

    def test_source_baseline_sha_and_time_are_bounded(self):
        self.case["objectives"][0]["before"]["release_sha"] = "abc"
        self.bad()
        self.case = copy.deepcopy(BEFORE)
        self.case["detected_at"] = "2026-01-01"
        self.bad()
        self.case = copy.deepcopy(BEFORE)
        self.case["signals"][0]["proof"]["observed_at"] = "yesterday"
        self.bad()

    def test_duplicate_context_edge_and_self_link(self):
        self.case["context"]["edges"].append(self.case["context"]["edges"][0].copy())
        self.bad()
        self.case = copy.deepcopy(BEFORE)
        self.case["context"]["edges"][0]["to"] = "api"
        self.bad()

    def test_baseline_release_must_match_resource_version(self):
        self.case["objectives"][0]["before"]["release_sha"] = "f" * 40
        self.bad()
        self.case = copy.deepcopy(BEFORE)
        del self.case["context"]["nodes"][0]["release_sha"]
        self.bad()

    def test_repo_identity_traversal_and_suffix_refused(self):
        for repo in ("../..", "a/..", "org/.git", "A/REPO", "org/repo.git"):
            with self.subTest(repo=repo):
                self.case["context"]["nodes"][0]["owner_repo"] = repo
                self.bad()

    def test_https_signed_url_and_source_namespace_refused(self):
        for ref in ("https://alice:token@example.org/trace",
                    "https://example.org/trace?sig=secret",
                    "https://example.org/trace#private",
                    "urn:ci:observation:factory-evidence"):
            with self.subTest(ref=ref):
                self.case["signals"][0]["proof"]["ref"] = ref
                self.bad()

    def test_alternate_ref_credential_bypasses_are_refused(self):
        for ref, source in (
            ("https://h:tok/x", "factory"),
            ("git:ci:https://u:p@h/r", "ci"),
            ("urn:ci:x?token=1", "ci"),
            ("https://example.org/x;param", "factory"),
            ("https://h:443/private@secret", "factory"),
            ("https://h:badport/x", "factory"),
        ):
            with self.subTest(ref=ref):
                self.case = copy.deepcopy(BEFORE)
                self.case["signals"][0]["proof"]["ref"] = ref
                self.case["signals"][0]["proof"]["source"] = source
                self.bad()

    def test_same_ref_or_digest_under_two_signal_names_is_refused(self):
        second = self.case["signals"][1]["proof"]
        first = self.case["signals"][0]["proof"]
        second["ref"] = first["ref"]
        self.bad()
        self.case = copy.deepcopy(BEFORE)
        self.case["signals"][1]["proof"]["fingerprint"] = first["fingerprint"]
        self.bad()

    def test_synthetic_proofs_disallowed_in_source_attested_case(self):
        self.case["data_class"] = "source_attested"
        self.case["signals"][0]["proof"]["source"] = "synthetic"
        self.case["signals"][0]["proof"]["ref"] = "urn:synthetic:observation:one"
        self.bad()

    def test_unhashable_objective_issue_fails_as_validation_error(self):
        self.case["objectives"][0]["issue"] = []
        self.bad()

    def test_bare_https_query_or_fragment_separator_is_refused(self):
        for ref in ("https://example.org/object?",
                    "https://example.org/object#"):
            self.case = copy.deepcopy(BEFORE)
            self.case["signals"][0]["proof"]["ref"] = ref
            self.bad()

    def test_huge_integer_does_not_overflow_uncaught(self):
        self.case["signals"][0]["threshold"]["value"] = 10 ** 400
        self.bad()

    def test_numeric_threshold_requires_metric_identity(self):
        del self.case["signals"][0]["metric"]
        self.bad()

    def test_proof_must_be_explicit_source_attestation(self):
        del self.case["signals"][0]["proof"]["fingerprint"]
        self.bad()
        self.case = copy.deepcopy(BEFORE)
        self.case["signals"][0]["proof"]["verdict"] = True
        self.bad()


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        self.case = copy.deepcopy(BEFORE)

    def test_two_independent_sources_correlate_and_preserve_refs(self):
        outcome = m.assess(self.case)
        self.assertEqual(outcome["status"], "INVESTIGATE")
        issue = outcome["issues"][0]
        self.assertEqual(issue["status"], "CORROBORATED")
        self.assertEqual(issue["source_count"], 2)
        self.assertEqual(issue["possible_owners"], ["example/platform"])
        self.assertEqual(issue["declared_source_paths"], [
            {"repo": "example/platform", "path": "src/api.py"}])
        self.assertEqual(len(issue["evidence"]), 2)
        self.assertEqual(outcome["permissions"]["write"], "NOT_GRANTED")
        self.assertEqual(outcome["permissions"]["deploy"], "NOT_GRANTED")

    def test_one_adverse_source_needs_investigation_not_confirmed_cause(self):
        self.case["signals"].pop()
        issue = m.assess(self.case)["issues"][0]
        self.assertEqual(issue["status"], "CANDIDATE")
        self.assertEqual(issue["next_action"], "READ_INVESTIGATION")

    def test_no_adverse_sources_means_no_action(self):
        self.case["signals"][0]["finding"] = "normal"
        self.case["signals"][0]["threshold"]["value"] = 0.001
        self.case["signals"][1]["finding"] = "normal"
        result = m.assess(self.case)
        self.assertEqual(result["status"], "NO_ACTION")
        self.assertEqual(result["issues"][0]["status"], "NO_ADVERSE_SIGNAL")
        self.assertEqual(len(result["issues"][0]["normal_evidence"]), 2)

    def test_unknown_source_is_not_dismissed_as_empty(self):
        for sig in self.case["signals"]:
            sig["finding"] = "unknown"
            sig["proof"]["verdict"] = "UNKNOWN"
            sig.pop("threshold", None)
        outcome = m.assess(self.case)
        self.assertEqual(outcome["status"], "HOLD")
        self.assertEqual(outcome["issues"][0]["status"], "SOURCE_UNCERTAIN")
        self.assertEqual(outcome["issues"][0]["unknown_count"], 2)
        self.assertEqual(len(outcome["issues"][0]["held_evidence"]), 2)
        self.assertEqual(outcome["issues"][0]["evidence"], [])

    def test_unlinked_resource_cannot_be_auto_correlated(self):
        self.case["context"]["nodes"].append({"id": "other", "kind": "queue"})
        self.case["signals"][1]["resource"] = "other"
        self.assertEqual(m.assess(self.case)["issues"][0]["status"], "UNLINKED_EVIDENCE")

    def test_linked_resources_are_correlated_only_when_edge_declared(self):
        self.case["signals"][1]["resource"] = "db"
        self.case["context"]["nodes"][0]["scope"] = "source-one"
        self.case["context"]["nodes"][1]["scope"] = "source-one"
        issue = m.assess(self.case)["issues"][0]
        self.assertEqual(issue["status"], "CORROBORATED")
        self.assertEqual(issue["resources"], ["api", "db"])
        self.assertEqual(len(issue["declared_source_paths"]), 2)

    def test_context_does_not_invent_repository_or_edges(self):
        view = m.context_for(self.case, "api")
        self.assertEqual(view["status"], "DECLARED_PROJECTION")
        self.assertEqual({x["id"] for x in view["nodes"]},
                         {"api", "db", "checkout"})
        self.assertEqual(len(view["edges"]), 2)
        self.assertEqual(m.context_for(self.case, "db", depth=0)["edges"], [])
        with self.assertRaises(m.ImprovementError):
            m.context_for(self.case, "invisible")
        with self.assertRaises(m.ImprovementError):
            m.context_for(self.case, "api", depth=3)

    def test_linked_resources_without_shared_scope_cannot_cross_correlate(self):
        self.case["signals"][1]["resource"] = "db"
        self.case["context"]["nodes"][0].pop("scope")
        self.case["context"]["nodes"][1].pop("scope")
        result = m.assess(self.case)["issues"][0]
        self.assertEqual(result["status"], "UNLINKED_EVIDENCE")
        self.case["context"]["nodes"][0]["scope"] = "source-one"
        self.case["context"]["nodes"][1]["scope"] = "source-two"
        self.assertEqual(m.assess(self.case)["issues"][0]["status"], "UNLINKED_EVIDENCE")

    def test_copy_of_one_artifact_is_not_independent_corroboration(self):
        self.case["signals"][1]["proof"]["fingerprint"] = (
            self.case["signals"][0]["proof"]["fingerprint"])
        with self.assertRaisesRegex(m.ImprovementError, "repeated evidence"):
            m.assess(self.case)

    def test_context_depth_is_one_hop_not_sibling_via_shared_hub(self):
        self.case["context"]["nodes"].append(
            {"id": "sibling", "kind": "service", "scope": "demo-one"})
        self.case["context"]["edges"].append(
            {"from": "sibling", "to": "db", "relation": "calls"})
        view = m.context_for(self.case, "api", depth=1)
        self.assertNotIn("sibling", {n["id"] for n in view["nodes"]})
        self.assertIn("sibling", {n["id"] for n in
                                  m.context_for(self.case, "api", depth=2)["nodes"]})

    def test_unscoped_context_never_crosses_to_other_unscoped_node(self):
        for node in self.case["context"]["nodes"]:
            node.pop("scope", None)
        result = m.context_for(self.case, "api", depth=2)
        self.assertEqual([node["id"] for node in result["nodes"]], ["api"])
        self.assertEqual(result["edges"], [])

    def test_context_never_projects_another_scope_through_shared_hub(self):
        self.case["context"]["nodes"].append(
            {"id": "other-scope", "kind": "service", "scope": "another-tenant",
             "owner_repo": "other/service", "source_path": "src/private.py"})
        self.case["context"]["edges"].append(
            {"from": "other-scope", "to": "db", "relation": "calls"})
        for depth in (1, 2):
            view = m.context_for(self.case, "api", depth=depth)
            self.assertNotIn("other-scope", {n["id"] for n in view["nodes"]})
        self.case["signals"][1]["resource"] = "other-scope"
        self.assertEqual(m.assess(self.case)["issues"][0]["status"],
                         "UNLINKED_EVIDENCE")

    def test_unchanged_input_has_stable_fingerprint(self):
        first = m.assess(self.case)
        second = m.assess(copy.deepcopy(self.case))
        self.assertEqual(first, second)
        self.case["signals"][0]["severity"] = "critical"
        self.assertNotEqual(first["case_fingerprint"], m.assess(self.case)["case_fingerprint"])


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.case = copy.deepcopy(BEFORE)
        self.cause = {
            "schema": m.CAUSE_SCHEMA, "case_id": self.case["case_id"],
            "issue": "deployment-failure", "repo": "example/platform",
            "hypothesis": "A newly introduced retry path appears to be defective.",
            "write_scope": ["src/api.py"],
            "evidence_ids": ["factory-observation", "ci-observation"],
        }

    def test_bounded_proposal_is_never_an_authority_grant(self):
        result = m.propose(self.case, self.cause)
        self.assertEqual(result["status"], "PROPOSAL_ONLY")
        self.assertTrue(result["no_execution_authority"])
        self.assertEqual(result["suggested_write_scope"], ["src/api.py"])
        self.assertIn("acquire separate Registry WRITE claim",
                      result["operator_must"])

    def test_scope_cannot_escape_declared_owner_or_touch_other_files(self):
        self.cause["write_scope"] = ["src/private.py"]
        with self.assertRaises(m.ImprovementError):
            m.propose(self.case, self.cause)
        self.cause["write_scope"] = ["src/api.py"]
        self.cause["repo"] = "another/repo"
        with self.assertRaises(m.ImprovementError):
            m.propose(self.case, self.cause)

    def test_case_issue_evidence_identity_must_match(self):
        self.cause["case_id"] = "some-other"
        with self.assertRaises(m.ImprovementError):
            m.propose(self.case, self.cause)
        self.cause["case_id"] = self.case["case_id"]
        self.cause["evidence_ids"] = ["foreign"]
        with self.assertRaises(m.ImprovementError):
            m.propose(self.case, self.cause)

    def test_unknown_evidence_cannot_promote_a_fix(self):
        for sig in self.case["signals"]:
            sig["finding"] = "unknown"
            sig["proof"]["verdict"] = "UNKNOWN"
            sig.pop("threshold", None)
        with self.assertRaises(m.ImprovementError):
            m.propose(self.case, self.cause)

    def test_reserved_governance_and_secret_paths_require_separate_approval(self):
        for path in (".github/workflows/release.yml", ".git/config",
                     "src/cao/registry.py", "config/.env"):
            with self.subTest(path=path):
                case = copy.deepcopy(self.case)
                case["context"]["nodes"][0]["source_path"] = path
                cause = copy.deepcopy(self.cause)
                cause["write_scope"] = [path]
                with self.assertRaises(m.ImprovementError):
                    m.propose(case, cause)

    def test_secret_sensitive_and_governance_filenames_fail_generic_proposal(self):
        for path in ("src/credentials.json", "conf/secrets.yaml",
                     "src/private_key.pem", "src/id_rsa",
                     "pyproject.toml", "Dockerfile", "CLAUDE.md"):
            with self.subTest(path=path):
                case, cause = copy.deepcopy(self.case), copy.deepcopy(self.cause)
                case["context"]["nodes"][0]["source_path"] = path
                cause["write_scope"] = [path]
                with self.assertRaises(m.ImprovementError):
                    m.propose(case, cause)

    def test_hidden_traversal_does_not_escape_scope(self):
        self.cause["write_scope"] = ["src/../secrets"]
        with self.assertRaises(m.ImprovementError):
            m.propose(self.case, self.cause)


class FitnessTests(unittest.TestCase):
    def setUp(self):
        self.case = copy.deepcopy(BEFORE)
        self.after = copy.deepcopy(AFTER)

    def test_full_outcome_evidence_retains_release_and_timing(self):
        result = m.verify(self.case, self.after)
        self.assertEqual(result["status"], "SYNTHETIC_ONLY")
        self.assertFalse(result["source_attested"])
        self.assertTrue(result["simulated_sources"])
        self.assertEqual(result["elapsed_seconds"]["detection_to_proposal"], 720)
        self.assertEqual(result["elapsed_seconds"]["detection_to_deployment"], 2700)
        self.assertFalse(result["accepted_as_canonical_operating_proof"])
        self.assertEqual(len(result["stage_evidence"]), 3)
        self.assertEqual(len(result["metrics"]), 2)

    def test_review_and_ci_must_match_exact_release_commit(self):
        self.after["intervention"]["ci"]["head"] = "c" * 40
        outcome = m.verify(self.case, self.after)
        self.assertEqual(outcome["status"], "UNQUALIFIED")
        self.assertTrue(any("different commit" in hold for hold in outcome["holds"]))
        self.after = copy.deepcopy(AFTER)
        self.after["intervention"]["review"]["status"] = "changes_requested"
        self.assertEqual(m.verify(self.case, self.after)["status"], "UNQUALIFIED")

    def test_source_only_or_failed_deployment_cannot_count_as_improvement(self):
        self.after["intervention"]["deployment"]["status"] = "not_deployed"
        self.assertEqual(m.verify(self.case, self.after)["status"], "UNQUALIFIED")

    def test_unsafe_guardrail_regression_outranks_goal_improvement(self):
        self.after["measurements"][1]["measurement"]["value"] = 250
        self.assertEqual(m.verify(self.case, self.after)["status"], "REGRESSION")

    def test_failed_guardrail_target_also_blocks(self):
        self.after["measurements"][1]["measurement"]["value"] = 302
        self.assertEqual(m.verify(self.case, self.after)["status"], "REGRESSION")

    def test_partial_change_below_target_is_not_accepted(self):
        self.after["measurements"][0]["measurement"]["value"] = 0.05
        self.assertEqual(m.verify(self.case, self.after)["status"], "TARGET_NOT_MET")

    def test_no_measured_gain_is_not_improvement(self):
        self.case["objectives"][0]["before"]["value"] = 0.005
        self.after["measurements"][0]["measurement"]["value"] = 0.005
        self.after["measurements"][1]["measurement"]["value"] = 240
        self.after["case_fingerprint"] = m.evidence.fingerprint(self.case)
        self.assertEqual(m.verify(self.case, self.after)["status"], "UNQUALIFIED")

    def test_missing_after_measurement_cannot_be_success(self):
        self.after["measurements"].pop()
        result = m.verify(self.case, self.after)
        self.assertEqual(result["status"], "UNQUALIFIED")
        self.assertTrue(any("missing after" in h for h in result["holds"]))

    def test_wrong_release_measurement_cannot_be_success(self):
        self.after["measurements"][0]["measurement"]["release_sha"] = "f" * 40
        self.assertEqual(m.verify(self.case, self.after)["status"], "UNQUALIFIED")

    def test_replayed_baseline_evidence_cannot_be_success(self):
        self.after["measurements"][0]["measurement"]["proof"]["fingerprint"] = (
            self.case["objectives"][0]["before"]["proof"]["fingerprint"])
        with self.assertRaisesRegex(m.ImprovementError, "repeated evidence"):
            m.verify(self.case, self.after)

    def test_measurement_predating_deployment_cannot_be_success(self):
        self.after["measurements"][0]["measurement"]["proof"]["observed_at"] = "2026-01-01T09:44:00Z"
        self.assertEqual(m.verify(self.case, self.after)["status"], "UNQUALIFIED")

    def test_unknown_attestation_cannot_claim_positive_review(self):
        self.after["intervention"]["review"]["proof"]["verdict"] = "UNKNOWN"
        with self.assertRaises(m.ImprovementError):
            m.verify(self.case, self.after)

    def test_followup_fingerprint_rejects_mutated_baseline(self):
        self.case["objectives"][0]["before"]["value"] = 0.5
        with self.assertRaisesRegex(m.ImprovementError, "different immutable case"):
            m.verify(self.case, self.after)

    def test_baseline_observed_after_proposal_is_rejected(self):
        self.case["objectives"][0]["before"]["proof"]["observed_at"] = (
            "2026-01-01T09:20:00Z")
        self.after["case_fingerprint"] = m.evidence.fingerprint(self.case)
        with self.assertRaisesRegex(m.ImprovementError, "baseline"):
            m.verify(self.case, self.after)

    def test_followup_repo_must_own_affected_issue(self):
        self.case["context"]["nodes"][2].update({
            "owner_repo": "unrelated/feature", "source_path": "src/unrelated.py"})
        self.after["intervention"]["repo"] = "unrelated/feature"
        self.after["case_fingerprint"] = m.evidence.fingerprint(self.case)
        self.assertEqual(m.verify(self.case, self.after)["status"], "UNQUALIFIED")

    def test_goal_regression_cannot_hide_behind_another_goal(self):
        self.case["objectives"][1]["guardrail"] = False
        self.after["case_fingerprint"] = m.evidence.fingerprint(self.case)
        self.after["measurements"][1]["measurement"]["value"] = 250
        self.assertEqual(m.verify(self.case, self.after)["status"], "REGRESSION")

    def test_numeric_signal_persists_even_if_goal_threshold_is_looser(self):
        self.case["objectives"][0]["target"] = 0.1
        self.after["case_fingerprint"] = m.evidence.fingerprint(self.case)
        self.after["measurements"][0]["measurement"]["value"] = 0.05
        result = m.verify(self.case, self.after)
        self.assertEqual(result["status"], "TARGET_NOT_MET")
        self.assertEqual(result["unresolved_adverse_conditions"],
                         ["factory-observation"])

    def test_mismatched_review_gate_source_is_rejected(self):
        self.after["intervention"]["review"]["proof"]["source"] = "telemetry"
        self.after["intervention"]["review"]["proof"]["ref"] = (
            "urn:telemetry:observation:review")
        with self.assertRaisesRegex(m.ImprovementError, "evidence source"):
            m.verify(self.case, self.after)

    def test_distinct_gate_artifacts_cannot_use_one_fingerprint(self):
        self.after["intervention"]["ci"]["proof"]["fingerprint"] = (
            self.after["intervention"]["review"]["proof"]["fingerprint"])
        with self.assertRaisesRegex(m.ImprovementError, "independent artifacts"):
            m.verify(self.case, self.after)

    def test_uncertainty_on_original_source_blocks_outcome(self):
        self.case["signals"][1]["finding"] = "unknown"
        self.case["signals"][1]["proof"]["verdict"] = "UNKNOWN"
        self.after["case_fingerprint"] = m.evidence.fingerprint(self.case)
        self.assertEqual(m.verify(self.case, self.after)["status"], "UNQUALIFIED")

    def test_wrong_case_identity_and_duplicate_measurements_refuse(self):
        self.after["case_id"] = "wrong"
        with self.assertRaises(m.ImprovementError):
            m.verify(self.case, self.after)
        self.after = copy.deepcopy(AFTER)
        self.after["measurements"][1]["objective_id"] = "errors"
        with self.assertRaises(m.ImprovementError):
            m.verify(self.case, self.after)


class LearningTests(unittest.TestCase):
    def _entry(self, name: str, sha: str):
        case, after = copy.deepcopy(BEFORE), copy.deepcopy(AFTER)
        case["data_class"] = "source_attested"
        case["case_id"] = name
        after["case_id"] = name
        after["intervention"]["commit_sha"] = sha
        for stage in ("review", "ci", "deployment"):
            after["intervention"][stage]["head"] = sha
        for sample in after["measurements"]:
            sample["measurement"]["release_sha"] = sha
        # New independent case requires new source artifacts, not merely a
        # changed case_id and a different text label.
        proofs = (
            [s["proof"] for s in case["signals"]]
            + [o["before"]["proof"] for o in case["objectives"]]
            + [x["measurement"]["proof"] for x in after["measurements"]]
        )
        for proof in proofs:
            proof["fingerprint"] = "sha256:" + hashlib.sha256(
                (name + "|" + proof["fingerprint"]).encode()).hexdigest()
            proof["ref"] = proof["ref"] + ":" + name
        if name.endswith("two"):
            # A separate incidence has its own detection and post-change window.
            def bump_dates(v):
                if type(v) is dict:
                    return {k: bump_dates(x) for k, x in v.items()}
                if type(v) is list:
                    return [bump_dates(x) for x in v]
                if type(v) is str and v.startswith("2026-01-01T"):
                    return v.replace("2026-01-01T", "2026-01-02T")
                return v
            case, after = bump_dates(case), bump_dates(after)
        after["case_fingerprint"] = m.evidence.fingerprint(case)
        record = m.verify(case, after)
        acceptance = {
            "decision": "accepted",
            "case_fingerprint": record["case_fingerprint"],
            "after_fingerprint": record["after_fingerprint"],
            "proof": {
                "source": "operator", "verdict": "SUPPORTED",
                "ref": "urn:operator:accepted:" + name,
                "fingerprint": "sha256:" + hashlib.sha256(
                    ("accepted|" + name).encode()).hexdigest(),
                "observed_at": ("2026-01-02T10:10:00Z"
                                if name.endswith("two") else "2026-01-01T10:10:00Z"),
            },
        }
        return {"case": case, "after": after, "acceptance": acceptance}

    def test_one_success_does_not_change_rule(self):
        result = m.learn({"schema": m.HISTORY_SCHEMA,
                          "entries": [self._entry("incident-one", "b"*40)]})
        self.assertEqual(result["suggestions"], [])
        self.assertEqual(result["rules_modified"], 0)

    def test_two_distinct_successes_propose_check_not_modify_one(self):
        history = {"schema": m.HISTORY_SCHEMA,
                   "entries": [self._entry("incident-one", "b"*40),
                               self._entry("incident-two", "c"*40)]}
        outcome = m.learn(history)
        self.assertEqual(outcome["status"], "PROPOSALS_ONLY")
        self.assertEqual(len(outcome["suggestions"]), 1)
        item = outcome["suggestions"][0]
        self.assertEqual(item["issue"], "deployment-failure")
        self.assertEqual(item["supporting_cases"], ["incident-one", "incident-two"])
        self.assertEqual(outcome["rules_modified"], 0)
        self.assertEqual(outcome["tasks_started"], 0)

    def test_repeated_same_release_is_not_independent_learning(self):
        h = {"schema": m.HISTORY_SCHEMA,
             "entries": [self._entry("incident-one", "b"*40),
                         self._entry("incident-two", "b"*40)]}
        self.assertEqual(m.learn(h)["suggestions"], [])

    def test_unqualified_or_regressed_runs_are_excluded_from_learning(self):
        one, two = self._entry("incident-one", "b"*40), self._entry("incident-two", "c"*40)
        two["after"]["measurements"][1]["measurement"]["value"] = 900
        two["acceptance"]["after_fingerprint"] = m.verify(
            two["case"], two["after"])["after_fingerprint"]
        self.assertEqual(m.learn({"schema": m.HISTORY_SCHEMA,
                                  "entries": [one, two]})["suggestions"], [])

    def test_relabelled_cases_reusing_same_artifacts_do_not_generate_learning(self):
        a, b = self._entry("incident-one", "b"*40), self._entry("incident-two", "c"*40)
        # Replay exact first-case evidence under a fresh case ID.
        for current, original in zip(b["case"]["signals"], a["case"]["signals"]):
            current["proof"] = copy.deepcopy(original["proof"])
        for current, original in zip(b["case"]["objectives"], a["case"]["objectives"]):
            current["before"]["proof"] = copy.deepcopy(original["before"]["proof"])
        for current, original in zip(b["after"]["measurements"], a["after"]["measurements"]):
            current["measurement"]["proof"] = copy.deepcopy(original["measurement"]["proof"])
        b["after"]["case_fingerprint"] = m.evidence.fingerprint(b["case"])
        replay = m.verify(b["case"], b["after"])
        b["acceptance"]["case_fingerprint"] = replay["case_fingerprint"]
        b["acceptance"]["after_fingerprint"] = replay["after_fingerprint"]
        self.assertEqual(m.learn({"schema": m.HISTORY_SCHEMA,
                                  "entries": [a, b]})["suggestions"], [])

    def test_learning_requires_accepted_operator_proof(self):
        a = self._entry("incident-one", "b"*40)
        a["acceptance"]["decision"] = "rejected"
        self.assertEqual(m.learn({"schema": m.HISTORY_SCHEMA,
                                  "entries": [a]})["suggestions"], [])
        a["acceptance"]["decision"] = "accepted"
        a["acceptance"]["proof"]["verdict"] = "UNKNOWN"
        with self.assertRaises(m.ImprovementError):
            m.learn({"schema": m.HISTORY_SCHEMA, "entries": [a]})

    def test_operator_acceptance_cannot_predate_the_after_metric(self):
        entry = self._entry("incident-one", "b"*40)
        entry["acceptance"]["proof"]["observed_at"] = "2026-01-01T09:46:00Z"
        with self.assertRaisesRegex(m.ImprovementError, "predates"):
            m.learn({"schema": m.HISTORY_SCHEMA, "entries": [entry]})

    def test_operator_acceptance_ref_is_independently_unique(self):
        a, b = self._entry("incident-one", "b"*40), self._entry("incident-two", "c"*40)
        b["acceptance"]["proof"]["ref"] = a["acceptance"]["proof"]["ref"]
        with self.assertRaisesRegex(m.ImprovementError, "replayed"):
            m.learn({"schema": m.HISTORY_SCHEMA, "entries": [a, b]})

    def test_prior_acceptance_cannot_reuse_future_case_proof(self):
        a, b = self._entry("incident-one", "b"*40), self._entry("incident-two", "c"*40)
        review = b["after"]["intervention"]["review"]["proof"]
        review["ref"] = "urn:operator:review:future-unique"
        review["fingerprint"] = "sha256:" + hashlib.sha256(
            b"unique future review").hexdigest()
        b["acceptance"]["after_fingerprint"] = m.verify(
            b["case"], b["after"])["after_fingerprint"]
        a["acceptance"]["proof"]["ref"] = review["ref"]
        with self.assertRaisesRegex(m.ImprovementError, "replayed"):
            m.learn({"schema": m.HISTORY_SCHEMA, "entries": [a, b]})
        a, b = self._entry("incident-one", "b"*40), self._entry("incident-two", "c"*40)
        a["acceptance"]["proof"]["fingerprint"] = (
            b["after"]["measurements"][0]["measurement"]["proof"]["fingerprint"])
        with self.assertRaisesRegex(m.ImprovementError, "replayed"):
            m.learn({"schema": m.HISTORY_SCHEMA, "entries": [a, b]})

    def test_operator_acceptance_fingerprint_is_independently_unique(self):
        a, b = self._entry("incident-one", "b"*40), self._entry("incident-two", "c"*40)
        b["acceptance"]["proof"]["fingerprint"] = (
            a["acceptance"]["proof"]["fingerprint"])
        with self.assertRaisesRegex(m.ImprovementError, "replayed"):
            m.learn({"schema": m.HISTORY_SCHEMA, "entries": [a, b]})

    def test_acceptance_cannot_reuse_the_review_artifact(self):
        a = self._entry("incident-one", "b"*40)
        a["acceptance"]["proof"]["ref"] = (
            a["after"]["intervention"]["review"]["proof"]["ref"])
        with self.assertRaisesRegex(m.ImprovementError, "cannot reuse"):
            m.learn({"schema": m.HISTORY_SCHEMA, "entries": [a]})

    def test_other_issue_on_same_resource_cannot_borrow_goal_gain(self):
        a, b = self._entry("incident-one", "b"*40), self._entry("incident-two", "c"*40)
        for entry in (a, b):
            second = copy.deepcopy(entry["case"]["signals"][1])
            second["id"] = "other-issue-source"
            second["issue"] = "separate-cause"
            second["proof"]["ref"] += ":second"
            second["proof"]["fingerprint"] = "sha256:" + hashlib.sha256(
                ("other|" + entry["case"]["case_id"]).encode()).hexdigest()
            entry["case"]["signals"].append(second)
            entry["after"]["case_fingerprint"] = m.evidence.fingerprint(entry["case"])
            result = m.verify(entry["case"], entry["after"])
            self.assertEqual(result["status"], "UNQUALIFIED")
            entry["acceptance"]["case_fingerprint"] = result["case_fingerprint"]
            entry["acceptance"]["after_fingerprint"] = result["after_fingerprint"]
        self.assertEqual(m.learn({"schema": m.HISTORY_SCHEMA,
                                  "entries": [a, b]})["suggestions"], [])

    def test_learning_acceptance_must_match_exact_outcome(self):
        a = self._entry("incident-one", "b"*40)
        a["acceptance"]["after_fingerprint"] = "sha256:" + "f"*64
        with self.assertRaisesRegex(m.ImprovementError, "disagrees"):
            m.learn({"schema": m.HISTORY_SCHEMA, "entries": [a]})

    def test_duplicate_run_identity_and_invalid_history_are_refused(self):
        entry = self._entry("incident-one", "b"*40)
        with self.assertRaises(m.ImprovementError):
            m.learn({"schema": m.HISTORY_SCHEMA, "entries": [entry, entry]})
        with self.assertRaises(m.ImprovementError):
            m.learn({"schema": "other", "entries": []})


if __name__ == "__main__":
    unittest.main()
