"""No-network regression tests for opt-in engineering automation and authority."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from cao import engineering_loop as e, product_config

HEAD = "b" * 40
BASE = "a" * 40
SUITE_SHA = "c" * 64
NOW = dt.datetime(2026, 10, 11, tzinfo=dt.timezone.utc)


class EngineeringTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.manifest_file = self.root / "watch.json"
        self.evidence_file = self.root / "comparison.json"
        self.state_file = self.root / "state.json"
        self.manifest = {
            "schema": e.SCHEMA,
            "watch_id": "test-example", "repo": "example/repo", "pr": 12,
            "base_branch": "main", "suite_sha256": SUITE_SHA,
            "evaluation_file": str(self.evidence_file), "min_gain": 0.1,
            "required_checks": ["validate (3.12)", "gbrain-evals"],
            "operators": ["operator"], "reviewers": ["reviewer"],
            "allowed_paths": ["src/search.py", "tests"],
            "expires_at": "2027-01-01T00:00:00Z",
            "min_interval_seconds": 60, "state_file": str(self.state_file),
            "auto_merge": True,
        }
        self.evaluation = {
            "schema": e.METRIC_SCHEMA,
            "status": "SOURCE_REPORTED_IMPROVEMENT",
            "baseline_sha": BASE, "candidate_sha": HEAD,
            "suite_sha256": SUITE_SHA,
            "objective": "mrr", "objective_gain": 0.25,
            "source_only": True, "canonical_operating_proof": False,
        }
        self.save()
        self.github = {
            "repos/example/repo/pulls/12": {
                "number": 12, "state": "open", "draft": False,
                "base": {"ref": "main"},
                "head": {"sha": HEAD, "repo": {"full_name": "example/repo"}},
                "changed_files": 2, "auto_merge": None,
                "merged": False, "user": {"login": "author"},
            },
            "repos/example/repo/pulls/12/files?per_page=100": [
                {"filename": "src/search.py"}, {"filename": "tests/test_search.py"},
            ],
            "repos/example/repo/commits/" + HEAD + "/check-runs?per_page=100": {
                "total_count": 2,
                "check_runs": [
                    {"name": key, "head_sha": HEAD, "status": "completed",
                     "conclusion": "success", "app": {"slug": "github-actions"}}
                    for key in self.manifest["required_checks"]
                ],
            },
            "repos/example/repo/pulls/12/reviews?per_page=100": [
                {"user": {"login": "reviewer"}, "commit_id": HEAD,
                 "state": "APPROVED"},
            ],
            "repos/example/repo/branches/main/protection/required_status_checks": {
                "contexts": self.manifest["required_checks"][:], "strict": True,
            },
            "user": {"login": "automation-bot"},
            "repos/example/repo/issues/12/comments?per_page=100": [],
        }
        self.calls = []
        self.merges = []

    def save(self):
        self.manifest_file.write_text(json.dumps(self.manifest))
        self.evidence_file.write_text(json.dumps(self.evaluation))

    def grant(self):
        digest = e._digest(self.evaluation)
        self.github["repos/example/repo/issues/12/comments?per_page=100"] = [
            {"body": "CAO-MERGE-AUTHORIZE " + HEAD + " " + digest,
             "user": {"login": "operator"}},
        ]

    def fetch(self, path):
        self.calls.append(path)
        return copy.deepcopy(self.github[path])

    def submit(self, repo, pr, sha):
        self.merges.append((repo, pr, sha))

    def tick(self, execute=False, now=NOW):
        return e.tick(str(self.manifest_file), execute=execute, now=now,
                      fetch=self.fetch, submit=self.submit)

    def test_missing_operator_grant_holds_without_merge(self):
        d = self.tick()
        self.assertEqual(d["status"], "AWAITING_OPERATOR_APPROVAL")
        self.assertTrue(d["operator_approval_required"])
        self.assertFalse(self.state_file.exists())
        self.assertEqual(self.merges, [])

    def test_dry_run_produces_exact_head_plan_without_writes(self):
        self.grant()
        d = self.tick()
        self.assertEqual(d["status"], "READY_TO_ARM")
        self.assertEqual(d["head"], HEAD)
        self.assertEqual(d["evaluation_sha256"], e._digest(self.evaluation))
        self.assertFalse(d["deployment_authorized"])
        self.assertFalse(self.state_file.exists())

    def test_authenticated_operator_arm_is_exact_head_and_idempotent(self):
        self.grant()
        d = self.tick(execute=True)
        self.assertEqual(d["status"], "AUTO_MERGE_REQUESTED")
        self.assertTrue(d["merge_requested"])
        self.assertEqual(self.merges, [("example/repo", 12, HEAD)])
        saved = json.loads(self.state_file.read_text())
        self.assertEqual(saved["phase"], "REQUESTED")
        d = self.tick(execute=True, now=NOW + dt.timedelta(minutes=10))
        self.assertEqual(d["status"], "AWAITING_GITHUB_RECONCILIATION")
        self.assertEqual(len(self.merges), 1)

    def test_lost_external_response_keeps_uncertain_boundary(self):
        self.grant()
        def fail_submit(*args):
            raise e.EngineeringError("transport lost")
        with self.assertRaises(e.EngineeringError):
            e.tick(str(self.manifest_file), execute=True, fetch=self.fetch,
                   submit=fail_submit, now=NOW)
        self.assertEqual(json.loads(self.state_file.read_text())["phase"], "SUBMITTING")
        self.assertEqual(self.tick(execute=True)["status"], "AWAITING_GITHUB_RECONCILIATION")
        self.assertEqual(self.merges, [])

    def test_remote_authoritative_merge_reconciliation_after_submission(self):
        self.grant()
        self.tick(execute=True)
        pr = self.github["repos/example/repo/pulls/12"]
        pr["auto_merge"] = {"merge_method": "SQUASH"}
        self.assertEqual(self.tick(execute=True)["status"], "AUTO_MERGE_ALREADY_ARMED")
        pr["auto_merge"] = None
        pr["state"] = "closed"
        pr["merged"] = True
        self.assertEqual(self.tick(execute=True)["status"], "MERGED_SOURCE_ONLY")
        self.assertEqual(len(self.merges), 1)

    def test_concurrent_effects_cannot_arm_same_pr_twice(self):
        self.grant()
        entered = threading.Event()
        release = threading.Event()
        completed = []

        def slow_submit(repo, pr, head):
            entered.set()
            if not release.wait(timeout=4):
                raise RuntimeError("timed out waiting to finish isolated fake merge")
            completed.append((repo, pr, head))

        def background():
            try:
                e.tick(str(self.manifest_file), execute=True, now=NOW,
                       fetch=self.fetch, submit=slow_submit)
            except Exception as exc:
                completed.append(type(exc).__name__)

        worker = threading.Thread(target=background, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(timeout=3))
        try:
            with self.assertRaises(e.EngineeringError):
                self.tick(execute=True)
        finally:
            release.set()
            worker.join(timeout=4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(completed, [("example/repo", 12, HEAD)])
        self.assertEqual(json.loads(self.state_file.read_text())["phase"],
                         "REQUESTED")

    def test_expired_grant_cannot_erase_inflight_checkpoint(self):
        self.grant()
        self.tick(execute=True)
        before = self.state_file.read_bytes()
        later = dt.datetime(2027, 1, 2, tzinfo=dt.timezone.utc)
        self.assertEqual(self.tick(execute=True, now=later)["status"],
                         "AWAITING_GITHUB_RECONCILIATION")
        self.assertEqual(self.state_file.read_bytes(), before)

    def test_cadence_bounded_and_operator_pause_respected(self):
        self.tick(execute=True)
        self.assertEqual(self.tick(execute=True, now=NOW + dt.timedelta(seconds=30))["status"],
                         "COOLDOWN")
        self.assertEqual(self.merges, [])
        self.grant()
        self.assertEqual(self.tick(execute=True, now=NOW + dt.timedelta(seconds=61))["status"],
                         "AUTO_MERGE_REQUESTED")
        self.assertEqual(len(self.merges), 1)

    def test_distinct_ci_review_branch_scope_rejections(self):
        self.grant()
        scenarios = [
            ("SCOPE_MISMATCH", "repos/example/repo/pulls/12/files?per_page=100",
             [{"filename": ".github/workflows/ci.yml"}, {"filename": "tests/test_search.py"}]),
            ("CI_NOT_GREEN_FOR_HEAD",
             "repos/example/repo/commits/" + HEAD + "/check-runs?per_page=100",
             {"total_count": 1, "check_runs": []}),
            ("INDEPENDENT_REVIEW_REQUIRED",
             "repos/example/repo/pulls/12/reviews?per_page=100",
             [{"user": {"login": "reviewer"}, "state": "APPROVED", "commit_id": BASE}]),
            ("BRANCH_RULES_NOT_VERIFIED",
             "repos/example/repo/branches/main/protection/required_status_checks",
             {"contexts": ["validate (3.12)"], "strict": True}),
            ("SEPARATE_OPERATOR_IDENTITY_REQUIRED", "user",
             {"login": "operator"}),
            ("AWAITING_OPERATOR_APPROVAL",
             "repos/example/repo/issues/12/comments?per_page=100",
             [{"body": "CAO-MERGE-AUTHORIZE " + BASE + " " + e._digest(self.evaluation),
               "user": {"login": "operator"}}]),
        ]
        for label, key, data in scenarios:
            with self.subTest(label=label):
                old = self.github[key]
                self.github[key] = data
                try:
                    self.assertEqual(self.tick(execute=False)["status"], label)
                    self.assertEqual(self.merges, [])
                finally:
                    self.github[key] = old

    def test_pr_author_cannot_independently_approve_own_changes(self):
        self.grant()
        self.github["repos/example/repo/pulls/12"]["user"]["login"] = "reviewer"
        self.assertEqual(self.tick()["status"], "INDEPENDENT_REVIEW_REQUIRED")
        self.assertEqual(self.merges, [])

    def test_authority_revocation_and_renamed_protected_source_refused(self):
        self.grant()
        comments = self.github["repos/example/repo/issues/12/comments?per_page=100"]
        comments.append({
            "body": "CAO-MERGE-REVOKE " + HEAD + " " + e._digest(self.evaluation),
            "user": {"login": "operator"},
        })
        self.assertEqual(self.tick()["status"], "AWAITING_OPERATOR_APPROVAL")
        comments.pop()
        files = self.github["repos/example/repo/pulls/12/files?per_page=100"]
        files[0] = {"status": "renamed", "filename": "src/search.py",
                    "previous_filename": ".github/workflows/ci.yml"}
        self.assertEqual(self.tick()["status"], "SCOPE_MISMATCH")
        self.assertEqual(self.merges, [])

    def test_outdated_base_status_checks_block_promotion(self):
        self.grant()
        policy = self.github[
            "repos/example/repo/branches/main/protection/required_status_checks"]
        policy["strict"] = False
        self.assertEqual(self.tick()["status"], "BRANCH_RULES_NOT_VERIFIED")
        self.assertEqual(self.merges, [])

    def test_mismatch_evaluation_head_or_protected_manifest(self):
        self.grant()
        self.evaluation["candidate_sha"] = "d" * 40
        self.save()
        self.assertEqual(self.tick()["status"], "PR_SOURCE_MISMATCH")
        self.evaluation["candidate_sha"] = HEAD
        for prefix in (".github/workflows", "deployment-targets", "tenants", "pyproject.toml"):
            bad = copy.deepcopy(self.manifest)
            bad["allowed_paths"] = [prefix]
            with self.subTest(prefix=prefix), self.assertRaises(e.EngineeringError):
                e.validate(bad)

    def test_unimproved_or_unknown_evaluation_holds_before_github(self):
        for status in ("REGRESSION", "NO_MEASURED_GAIN", "UNKNOWN"):
            self.evaluation["status"] = status
            self.save()
            with self.subTest(status=status):
                d = self.tick()
                self.assertEqual(d["status"], "NEEDS_IMPROVEMENT")
                self.assertEqual(self.calls, [])
        self.evaluation["status"] = "SOURCE_REPORTED_IMPROVEMENT"
        self.evaluation["objective_gain"] = 0.01
        self.save()
        with self.assertRaises(e.EngineeringError):
            self.tick()

    def test_expiry_holds_without_external_calls(self):
        d = self.tick(now=dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(d["status"], "EXPIRED")
        self.assertEqual(self.calls, [])

    def test_suspicious_manifest_or_state_never_accepted(self):
        malicious = copy.deepcopy(self.manifest)
        malicious["auto_merge"] = "yes"
        with self.assertRaises(e.EngineeringError):
            e.validate(malicious)
        malicious = copy.deepcopy(self.manifest)
        malicious["operators"] = ["reviewer"]
        with self.assertRaises(e.EngineeringError):
            e.validate(malicious)
        malicious = copy.deepcopy(self.manifest)
        malicious["required_checks"] = ["only"]
        with self.assertRaises(e.EngineeringError):
            e.validate(malicious)
        self.state_file.write_text('{"schema":"incorrect"}')
        with self.assertRaises(e.EngineeringError):
            self.tick()
        self.assertEqual(self.calls, [])

    def test_partial_checkpoint_is_never_overwritten(self):
        self.grant()
        self.tick(execute=True)
        frozen = self.state_file.read_bytes()
        new = copy.deepcopy(self.manifest)
        new["allowed_paths"] = ["src/new.py"]
        self.manifest = new
        self.save()
        with self.assertRaises(e.EngineeringError):
            self.tick(execute=True)
        self.assertEqual(self.state_file.read_bytes(), frozen)

    def test_no_auto_merge_mode_never_submits(self):
        self.grant()
        self.manifest["auto_merge"] = False
        self.save()
        self.assertEqual(self.tick(execute=True)["status"], "APPROVED_PLAN_ONLY")
        self.assertEqual(self.merges, [])

    def test_queue_blocks_later_merges_until_prior_pr_is_merged(self):
        queue = self.root / "queue"
        queue.mkdir()
        (queue / "001-first.json").write_text(json.dumps(self.manifest))
        second = copy.deepcopy(self.manifest)
        second.update(watch_id="second-pr", pr=13,
                      state_file=str(self.root / "state2.json"))
        (queue / "002-second.json").write_text(json.dumps(second))
        first = e.tick_queue(str(queue), execute=True, fetch=self.fetch,
                             submit=self.submit, now=NOW)
        self.assertEqual(first["in_flight"], "test-example")
        self.assertEqual(first["items"][0]["status"], "AWAITING_OPERATOR_APPROVAL")
        self.assertEqual(self.merges, [])
        self.github["repos/example/repo/pulls/12"]["merged"] = True
        self.github["repos/example/repo/pulls/12"]["state"] = "closed"
        p13 = copy.deepcopy(self.github["repos/example/repo/pulls/12"])
        p13.update(number=13, state="open", merged=False)
        self.github["repos/example/repo/pulls/13"] = p13
        for path in list(self.github):
            if "pulls/12/" in path or "issues/12/" in path:
                self.github[path.replace("12/", "13/")] = copy.deepcopy(self.github[path])
        second_result = e.tick_queue(str(queue), execute=False, fetch=self.fetch,
                                     submit=self.submit, now=NOW)
        self.assertEqual(second_result["in_flight"], "second-pr")
        self.assertEqual(second_result["items"][0]["status"], "MERGED_SOURCE_ONLY")
        self.assertEqual(second_result["items"][1]["status"], "AWAITING_OPERATOR_APPROVAL")

    def test_queue_preflights_all_cases_before_any_external_effect(self):
        queue = self.root / "queue"
        queue.mkdir()
        self.grant()
        (queue / "001-first.json").write_text(json.dumps(self.manifest))
        (queue / "002-invalid.json").write_text('{"schema":"invalid","schema":"duplicate"}')
        with self.assertRaises(e.EngineeringError):
            e.tick_queue(str(queue), execute=True,
                         fetch=self.fetch, submit=self.submit, now=NOW)
        self.assertEqual(self.merges, [])
        self.assertFalse(self.state_file.exists())
        (queue / "002-invalid.json").unlink()
        linked = self.root / "linked-queue"
        linked.symlink_to(queue)
        with self.assertRaises(e.EngineeringError):
            e.tick_queue(str(linked), execute=True, fetch=self.fetch)

    def test_cli_source_report_is_dry_by_default(self):
        self.evaluation["status"] = "REGRESSION"
        self.save()
        source = Path(__file__).resolve().parents[1]
        argv = [str(source / "cao"), "improve", "engineering", "--file",
                str(self.manifest_file)]
        p = subprocess.run(argv, capture_output=True, text=True, timeout=12)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["status"], "NEEDS_IMPROVEMENT")
        self.assertFalse(self.state_file.exists())
        self.manifest_file.write_text('{"schema":"incorrect","schema":"duplicate"}')
        p = subprocess.run(argv, capture_output=True, text=True, timeout=12)
        self.assertEqual(p.returncode, 3)
        self.assertEqual(json.loads(p.stdout)["status"], "UNAVAILABLE")

    def test_config_opt_in_strict_absolute_and_disabled_by_default(self):
        self.assertIsNone(product_config.daemon_defaults().engineering_watch_file)
        valid = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "daemon": {"engineering_watch_file": str(self.manifest_file)},
        })
        self.assertEqual(
            product_config.daemon_defaults(valid).engineering_watch_file,
            str(self.manifest_file))
        for bad in ("relative.json", "/root/../bad.json", "", 1):
            with self.subTest(bad=bad):
                configured = product_config.validate_config({
                    "schema": product_config.SCHEMA,
                    "daemon": {"engineering_watch_file": bad},
                })
                with self.assertRaises(product_config.ProductConfigError):
                    product_config.daemon_defaults(configured)

    def test_no_credential_or_untrusted_content_reflected(self):
        self.manifest_file.write_text('{"schema":"x","schema":"x"}')
        with self.assertRaises(e.EngineeringError):
            self.tick()
        self.assertFalse(self.state_file.exists())


if __name__ == "__main__":
    unittest.main()
