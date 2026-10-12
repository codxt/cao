"""Deterministic feedback watch and authorized correction integration tests."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cao import evidence, feedback_loop as feedback, improvement, product_config, team

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}
ROOT = Path(__file__).resolve().parents[1]
CASE = json.loads((ROOT / "examples/improvement_case.json").read_text())
AFTER = json.loads((ROOT / "examples/improvement_after.json").read_text())


def git(*args: str, cwd: Path) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True,
                       env={**os.environ, **GIT_ENV}, timeout=12)
    if p.returncode:
        raise AssertionError(p.stderr)
    return p.stdout.strip()


class FeedbackWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.inbox.mkdir()
        self.repo = self.root / "source"
        self.repo.mkdir()
        git("init", "-b", "main", cwd=self.repo)
        git("remote", "add", "origin", "https://github.com/example/platform.git", cwd=self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "src/api.py").write_text("value = 1\n")
        (self.repo / "src/database.py").write_text("value = 2\n")
        git("add", ".", cwd=self.repo)
        git("commit", "-m", "first", cwd=self.repo)
        self.head = git("rev-parse", "HEAD", cwd=self.repo)
        git("checkout", "--detach", self.head, cwd=self.repo)
        self.case = copy.deepcopy(CASE)
        self.case["data_class"] = "source_attested"
        for node in self.case["context"]["nodes"]:
            if node.get("release_sha"):
                node["release_sha"] = self.head
        for objective in self.case["objectives"]:
            objective["before"]["release_sha"] = self.head
        self.watch = {
            "schema": feedback.SCHEMA, "watch_id": "sample-one",
            "inbox_dir": str(self.inbox), "repo": str(self.repo),
            "owner_repo": "example/platform", "model": "opencode-go/model-a",
            "max_cases_per_tick": 4,
        }
        self.watch_path = self.root / "watch.json"
        self.case_path = self.inbox / "case.json"
        self.write_json(self.watch_path, self.watch)
        self.write_json(self.case_path, self.case)
        self.env_patch = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    @staticmethod
    def write_json(path: Path, payload: dict):
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    def test_watch_dry_run_is_read_only_and_exposes_bound_queries_impact(self):
        result = feedback.tick(str(self.watch_path), admit=False)
        self.assertEqual(result["status"], "INSPECTED")
        self.assertEqual(len(result["observations"]), 1)
        record = result["observations"][0]
        self.assertEqual(record["assessment"], "CORROBORATED")
        self.assertEqual(record["decision"], "INVESTIGATE")
        self.assertEqual(record["queries"][0]["metric"], "error_rate")
        self.assertEqual(record["potential_impact"], ["checkout"])
        self.assertFalse((self.root / "state").exists())
        with self.assertRaises(team.TeamError):
            team.status(record["team_id"])

    def test_watch_dispatches_one_read_team_once_without_writer_claim(self):
        first = feedback.tick(str(self.watch_path))
        key = first["observations"][0]["team_id"]
        self.assertEqual(first["status"], "RUNNING")
        manifest = team._required(key)
        self.assertEqual(manifest["desired"], "running")
        self.assertEqual(manifest["spec"]["max_writers"], 0)
        self.assertEqual(manifest["spec"]["tasks"][0]["mode"], "read")
        self.assertIn(evidence.fingerprint(self.case), manifest["spec"]["tasks"][0]["prompt"])
        second = feedback.tick(str(self.watch_path))
        self.assertEqual(second["observations"][0]["team_id"], key)
        self.assertEqual(len(team.list_teams()), 1)
        self.assertEqual(team.status(key)["tick_count"], 0)

    def test_watch_preserves_manual_pause_across_ticks(self):
        initial = feedback.tick(str(self.watch_path))
        ident = initial["observations"][0]["team_id"]
        team.set_desired(ident, "paused")
        observed = feedback.tick(str(self.watch_path))
        self.assertEqual(observed["status"], "HOLD")
        self.assertEqual(observed["observations"][0]["team_status"], "PAUSED")
        self.assertEqual(team.status(ident)["desired"], "paused")
        self.assertEqual(team.status(ident)["tick_count"], 0)

    def test_later_bad_outcome_refuses_all_new_admissions(self):
        additional = copy.deepcopy(self.case)
        additional["case_id"] = "second-incident"
        self.write_json(self.inbox / "second.json", additional)
        self.watch["outcomes_dir"] = str(self.root / "outcomes")
        outcomes = self.root / "outcomes"
        outcomes.mkdir()
        (outcomes / "second-incident.json").write_text(
            '{"schema":"x","schema":"y"}', encoding="utf-8")
        self.write_json(self.watch_path, self.watch)
        with self.assertRaises((ValueError, feedback.FeedbackError)):
            feedback.tick(str(self.watch_path))
        self.assertFalse((self.root / "state").exists())

    def test_bad_history_refuses_new_admission_before_side_effects(self):
        history = self.root / "history.json"
        history.write_text('{"schema":"bad"}', encoding="utf-8")
        self.watch["history_file"] = str(history)
        self.write_json(self.watch_path, self.watch)
        with self.assertRaises((ValueError, feedback.FeedbackError)):
            feedback.tick(str(self.watch_path))
        self.assertFalse((self.root / "state").exists())

    def test_changed_case_same_identity_refuses_duplicate_team(self):
        feedback.tick(str(self.watch_path))
        self.case["detected_at"] = "2026-01-01T08:59:00Z"
        self.write_json(self.case_path, self.case)
        with self.assertRaises(feedback.FeedbackError):
            feedback.tick(str(self.watch_path))
        self.assertEqual(len(team.list_teams()), 1)

    def test_hold_on_synthetic_or_wrong_owner_or_revision(self):
        for mutate in (
                lambda d: d.update(data_class="synthetic"),
                lambda d: d["context"]["nodes"][0].update(owner_repo="example/elsewhere"),
                lambda d: d["context"]["nodes"][0].update(release_sha="f" * 40),
        ):
            with self.subTest(mutate=mutate):
                case = copy.deepcopy(self.case)
                mutate(case)
                # Wrong revision also requires a valid baseline to remain
                # internally consistent. Use synthetic for a separate test.
                if case["context"]["nodes"][0].get("release_sha") == "f" * 40:
                    for o in case["objectives"]:
                        if o["resource"] == case["context"]["nodes"][0]["id"]:
                            o["before"]["release_sha"] = "f" * 40
                self.write_json(self.case_path, case)
                result = feedback.tick(str(self.watch_path))
                self.assertEqual(result["status"], "HOLD")
                self.assertEqual(result["observations"][0]["decision"], "HOLD")
                self.assertFalse((self.root / "state").exists())
                self.write_json(self.case_path, self.case)

    def test_invalid_case_prevents_all_admission(self):
        self.write_json(self.inbox / "another.json", self.case)
        with self.assertRaises(feedback.FeedbackError):
            feedback.tick(str(self.watch_path))
        self.assertFalse((self.root / "state").exists())
        (self.inbox / "another.json").unlink()
        (self.inbox / "bad.json").write_text('{"schema":"x","schema":"y"}')
        with self.assertRaises(feedback.FeedbackError):
            feedback.tick(str(self.watch_path))
        self.assertFalse((self.root / "state").exists())

    def test_symlinks_and_unbounded_directory_refused(self):
        self.case_path.unlink()
        self.case_path.symlink_to(self.watch_path)
        with self.assertRaises(feedback.FeedbackError):
            feedback.tick(str(self.watch_path))
        self.case_path.unlink()
        self.write_json(self.case_path, self.case)
        self.watch["max_cases_per_tick"] = 1
        self.write_json(self.watch_path, self.watch)
        self.write_json(self.inbox / "another.json", self.case)
        with self.assertRaises(feedback.FeedbackError):
            feedback.tick(str(self.watch_path))
        self.assertFalse((self.root / "state").exists())

    def test_dirty_attached_or_wrong_origin_read_checkout_holds(self):
        git("checkout", "main", cwd=self.repo)
        self.assertEqual(feedback.tick(str(self.watch_path))["status"], "HOLD")
        git("checkout", "--detach", self.head, cwd=self.repo)
        (self.repo / "src/api.py").write_text("uncommitted\n")
        self.assertEqual(feedback.tick(str(self.watch_path))["status"], "HOLD")
        (self.repo / "src/api.py").write_text("value = 1\n")
        git("remote", "set-url", "origin", "https://github.com/elsewhere/unknown.git",
            cwd=self.repo)
        self.assertEqual(feedback.tick(str(self.watch_path))["status"], "HOLD")
        self.assertFalse((self.root / "state").exists())

    def test_manifest_validation_fails_closed(self):
        for key, value in (("inbox_dir", "../x"), ("repo", "/foo/../bar"),
                           ("model", "../model"), ("max_cases_per_tick", 0),
                           ("watch_id", None), ("unknown", 5)):
            raw = {**self.watch, key: value}
            with self.subTest(key=key), self.assertRaises((ValueError, TypeError)):
                feedback.validate_watch(raw)
        with self.assertRaises(feedback.FeedbackError):
            feedback.validate_watch({"schema": feedback.SCHEMA})

    def _accept_read(self):
        result = feedback.tick(str(self.watch_path))
        name = result["observations"][0]["team_id"]
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "opencode-go/model-a"},
        })
        team.tick(name, config=config, driver=None,
                  execute=lambda **kw: {
                      "status": "complete",
                      "claim": {"released": True, "released_head": self.head},
                      "output": "Source inspection and bounded cause context",
                      "output_truncated": False,
                  },
                  head_reader=lambda _: self.head)
        self.assertEqual(team.status(name)["status"], "COMPLETE")
        return name

    def _cause_file(self) -> Path:
        path = self.root / "cause.json"
        self.write_json(path, {
            "schema": improvement.CAUSE_SCHEMA,
            "case_id": self.case["case_id"], "issue": "deployment-failure",
            "hypothesis": "Check one exact retry path, then test outcomes.",
            "repo": "example/platform", "write_scope": ["src/api.py"],
            "evidence_ids": ["factory-observation", "ci-observation"],
        })
        return path

    def test_write_requires_read_completion_and_new_clean_task_branch(self):
        cause = self._cause_file()
        work = self.root / "fix"
        git("worktree", "add", "-b", "fix/incident", str(work), cwd=self.repo)
        with self.assertRaises(feedback.FeedbackError):
            feedback.authorize_correction(
                str(self.watch_path), str(self.case_path), str(cause),
                str(work), "opencode-go/code", "opencode-go/review")
        self._accept_read()
        admitted = feedback.authorize_correction(
            str(self.watch_path), str(self.case_path), str(cause),
            str(work), "opencode-go/code", "opencode-go/review")
        self.assertEqual(admitted["registry_claim_pending"], True)
        self.assertFalse(admitted["deployment_authorized"])
        self.assertEqual(admitted["write_scope"], ["src/api.py"])
        spec = team._required(admitted["team_id"])["spec"]
        self.assertEqual(spec["max_writers"], 1)
        self.assertEqual(spec["tasks"][0]["write_scope"], ["src/api.py"])
        self.assertEqual(spec["tasks"][0]["reviewer"], "reviewer")
        self.assertIn("Source inspection", spec["tasks"][0]["prompt"])
        again = feedback.authorize_correction(
            str(self.watch_path), str(self.case_path), str(cause),
            str(work), "opencode-go/code", "opencode-go/review")
        self.assertEqual(admitted["team_id"], again["team_id"])
        team.set_desired(admitted["team_id"], "paused")
        paused = feedback.authorize_correction(
            str(self.watch_path), str(self.case_path), str(cause),
            str(work), "opencode-go/code", "opencode-go/review")
        self.assertEqual(paused["status"], "PAUSED")
        self.assertEqual(team.status(admitted["team_id"])["desired"], "paused")
        (work / "src/api.py").write_text("changed\n")
        with self.assertRaises(feedback.FeedbackError):
            feedback.authorize_correction(
                str(self.watch_path), str(self.case_path), str(cause),
                str(work), "opencode-go/code", "opencode-go/review")

    def test_write_cannot_reuse_read_checkout_or_substitute_model(self):
        self._accept_read()
        cause = self._cause_file()
        with self.assertRaises(feedback.FeedbackError):
            feedback.authorize_correction(
                str(self.watch_path), str(self.case_path), str(cause),
                str(self.repo), "opencode-go/code", "opencode-go/review")
        work = self.root / "fix"
        git("worktree", "add", "-b", "fix/correction", str(work), cwd=self.repo)
        with self.assertRaises(feedback.FeedbackError):
            feedback.authorize_correction(
                str(self.watch_path), str(self.case_path), str(cause),
                str(work), "opencode-go/code", "opencode-go/code")

    def test_complete_fixture_loop_keeps_external_release_and_learning_separate(self):
        # Real Git worktrees and team checkpoints; deterministic fake workers
        # stand in for runtime messages. No provider, Registry WRITE claim,
        # publication, deployment or tenant state is changed by this test.
        report = feedback.tick(str(self.watch_path))
        read_team = report["observations"][0]["team_id"]
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "opencode-go/model-a"},
        })
        team.tick(read_team, config=config, driver=None,
                  execute=lambda **kw: {
                      "status": "complete",
                      "claim": {"released": True, "released_head": self.head},
                      "output": "Observed original failure, proposed exact test",
                      "output_truncated": False,
                  }, head_reader=lambda _: self.head)
        self.assertEqual(team.status(read_team)["status"], "COMPLETE")
        work = self.root / "bounded-fix"
        git("worktree", "add", "-b", "fix/bounded", str(work), cwd=self.repo)
        correction = feedback.authorize_correction(
            str(self.watch_path), str(self.case_path), str(self._cause_file()),
            str(work), "opencode-go/developer", "opencode-go/reviewer")
        fix_team = correction["team_id"]
        before = feedback.tick(str(self.watch_path))["observations"][0]
        self.assertEqual(before["correction_team_status"], "PENDING")

        # The code author uses one task/trace to investigate, fix and commit.
        (work / "src/api.py").write_text("value = 3\n")
        git("add", "src/api.py", cwd=work)
        git("commit", "-m", "scoped correction", cwd=work)
        new_head = git("rev-parse", "HEAD", cwd=work)
        self.assertNotEqual(new_head, self.head)
        team.tick(fix_team, config=config, driver=None,
                  execute=lambda **kw: {
                      "status": "complete",
                      "claim": {"released": True, "released_head": new_head},
                      "output": "Scoped fix committed",
                      "output_truncated": False,
                  }, writer_snapshot=lambda _: (0, set()),
                  head_reader=lambda _: new_head)
        phase = team._required(fix_team)["slots"]["correction"]["phase"]
        self.assertEqual(phase, "review")
        team.tick(fix_team, config=config, driver=None,
                  execute=lambda **kw: {
                      "status": "complete",
                      "claim": {"released": True, "released_head": new_head},
                      "output": "CAO_REVIEW_VERDICT: APPROVED\nCAO_REVIEWED_HEAD: " + new_head,
                      "output_truncated": False,
                  }, writer_snapshot=lambda _: (0, set()),
                  head_reader=lambda _: new_head)
        self.assertEqual(team.status(fix_team)["status"], "COMPLETE")
        self.watch["outcomes_dir"] = str(self.root / "outcomes")
        (self.root / "outcomes").mkdir()
        self.write_json(self.watch_path, self.watch)
        after = copy.deepcopy(AFTER)
        after["case_fingerprint"] = evidence.fingerprint(self.case)
        after["intervention"]["commit_sha"] = new_head
        for stage in ("review", "ci", "deployment"):
            after["intervention"][stage]["head"] = new_head
        for measure in after["measurements"]:
            measure["measurement"]["release_sha"] = new_head
        self.write_json(self.root / "outcomes" /
                        (self.case["case_id"] + ".json"), after)
        accepted = feedback.tick(str(self.watch_path))
        self.assertEqual(accepted["status"], "INSPECTED")
        issue = accepted["observations"][0]
        self.assertEqual(issue["team_status"], "COMPLETE")
        self.assertEqual(issue["correction_team_status"], "COMPLETE")
        self.assertEqual(issue["outcome"]["status"], "SOURCE_REPORTED_IMPROVEMENT")
        self.assertFalse(issue["outcome"]["accepted_as_canonical_operating_proof"])

    def enable_chat_approval(self):
        path = Path(product_config.config_path({
            "XDG_CONFIG_HOME": str(self.root / "config")}))
        path.parent.mkdir(parents=True, exist_ok=True)
        product_config.write_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "opencode-go/model-a"},
            "team_approval": {"mode": "chat"},
        }, path)

    def admit_investigation(self):
        """The operator's explicit admission of the held READ investigation."""
        held = feedback.tick(str(self.watch_path))["observations"][0]
        watch = feedback.validate_watch(self.watch)
        issue = feedback.improvement.assess(self.case)["issues"][0]
        admitted = team.initialize(feedback._investigation_spec(watch, self.case, issue),
                                   env=None, approved=True)
        return held, admitted

    def test_unattended_watch_holds_instead_of_bypassing_chat_approval(self):
        self.enable_chat_approval()
        report = feedback.tick(str(self.watch_path))
        self.assertEqual(report["status"], "HOLD")
        self.assertTrue(report["chat_admission_held"])
        record = report["observations"][0]
        self.assertEqual(record["decision"], "INVESTIGATE")
        self.assertTrue(record["admission_held"])
        self.assertIn("chat team approval is required", record["reason"])
        # No team was created, so no duplicate identity can ever appear.
        self.assertIsNone(record["team_status"])
        self.assertEqual(team.list_teams(), [])
        # A dry run stays read-only and creates nothing.
        again = feedback.tick(str(self.watch_path), admit=False)
        self.assertEqual(again["status"], "INSPECTED")
        self.assertFalse(again["observations"][0]["admission_held"])
        self.assertEqual(team.list_teams(), [])
        # An operator admission is the only way the watch proceeds.
        held, admitted = self.admit_investigation()
        self.assertEqual(admitted["team_id"], held["team_id"])
        self.assertEqual(admitted["approval"]["method"], "operator-declared")
        admitted = team.set_desired(admitted["team_id"], "running")
        self.assertEqual(admitted["desired"], "running")
        self.assertEqual(feedback.tick(str(self.watch_path))["status"], "RUNNING")
        self.assertFalse(feedback.tick(str(self.watch_path))["chat_admission_held"])
        self.assertEqual(len(team.list_teams()), 1)

    def test_operator_invoked_correction_declares_chat_approval_without_identity(self):
        self.enable_chat_approval()
        _, admitted = self.admit_investigation()
        team.set_desired(admitted["team_id"], "running")
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "opencode-go/model-a"},
            "team_approval": {"mode": "chat"},
        })
        team.tick(admitted["team_id"], config=config, driver=None,
                  execute=lambda **kw: {
                      "status": "complete",
                      "claim": {"released": True, "released_head": self.head},
                      "output": "Source inspection and bounded cause context",
                      "output_truncated": False,
                  },
                  head_reader=lambda _: self.head)
        self.assertEqual(team.status(admitted["team_id"], env=None)["status"], "COMPLETE")
        work = self.root / "fix"
        git("worktree", "add", "-b", "fix/incident", str(work), cwd=self.repo)
        result = feedback.authorize_correction(
            str(self.watch_path), str(self.case_path), str(self._cause_file()),
            str(work), "opencode-go/code", "opencode-go/review")
        self.assertTrue(result["chat_approval_declared_by_this_invocation"])
        # Invoking the command is the declaration; it authenticates no person
        # and grants no publication or deployment authority.
        self.assertTrue(result["operator_invocation_not_identity_proof"])
        self.assertFalse(result["publication_authorized"])
        self.assertFalse(result["deployment_authorized"])
        record = team.status(result["team_id"], env=None)
        self.assertEqual(record["approval"]["method"], "operator-declared")
        self.assertEqual(record["desired"], "running")
        self.assertEqual(team._required(result["team_id"], env=None)["spec"][
            "initiative"], result["team_id"])
        # The unattended watch reports the admitted READ investigation without
        # re-declaring anything, and creates no second team.
        watch_report = feedback.tick(str(self.watch_path))
        self.assertFalse(watch_report["chat_admission_held"])
        self.assertEqual(watch_report["observations"][0]["team_status"], "COMPLETE")
        self.assertEqual(len(team.list_teams()), 2)
        # A repeated invocation stays idempotent and never overrides a pause.
        team.set_desired(result["team_id"], "paused")
        again = feedback.authorize_correction(
            str(self.watch_path), str(self.case_path), str(self._cause_file()),
            str(work), "opencode-go/code", "opencode-go/review")
        self.assertEqual(again["team_id"], result["team_id"])
        self.assertEqual(team.status(result["team_id"], env=None)["desired"], "paused")

    def test_legacy_policy_is_unchanged_by_the_chat_admission_hold(self):
        report = feedback.tick(str(self.watch_path))
        self.assertEqual(report["status"], "RUNNING")
        self.assertFalse(report["chat_admission_held"])
        self.assertFalse(report["observations"][0]["admission_held"])
        self.assertEqual(len(team.list_teams()), 1)

    def test_source_reported_outcome_requires_matching_cao_correction_sha(self):
        self._accept_read()
        self.watch["outcomes_dir"] = str(self.root / "outcomes")
        outdir = self.root / "outcomes"
        outdir.mkdir()
        self.write_json(self.watch_path, self.watch)
        after = copy.deepcopy(AFTER)
        after["case_fingerprint"] = evidence.fingerprint(self.case)
        self.write_json(outdir / (self.case["case_id"] + ".json"), after)
        original = feedback.tick(str(self.watch_path))
        self.assertEqual(original["status"], "HOLD")
        first = original["observations"][0]["outcome"]
        self.assertEqual(first["status"], "UNQUALIFIED")
        self.assertIn("no authorized", first["reason"])
        accepted = {"status": "COMPLETE",
                    "tasks": {"correction": {
                        "status": "complete", "head": after["intervention"]["commit_sha"]}}}
        with mock.patch("cao.feedback_loop.team.status", return_value=accepted):
            second = feedback.tick(str(self.watch_path))["observations"][0]["outcome"]
        self.assertEqual(second["status"], "SOURCE_REPORTED_IMPROVEMENT")
        self.assertFalse(second["accepted_as_canonical_operating_proof"])
        stale = copy.deepcopy(accepted)
        stale["tasks"]["correction"]["head"] = "c" * 40
        with mock.patch("cao.feedback_loop.team.status", return_value=stale):
            third = feedback.tick(str(self.watch_path))["observations"][0]["outcome"]
        self.assertEqual(third["status"], "UNQUALIFIED")

    def test_report_without_adverse_signal_does_not_trigger_investigation(self):
        for sig in self.case["signals"]:
            sig["finding"] = "normal"
            sig.pop("metric", None)
            sig.pop("threshold", None)
        self.write_json(self.case_path, self.case)
        result = feedback.tick(str(self.watch_path))
        self.assertEqual(result["status"], "INSPECTED")
        self.assertEqual(result["observations"][0]["decision"], "NONE")
        self.assertFalse((self.root / "state").exists())

    def test_watch_cli_and_config_are_optional_and_fail_closed(self):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        p = subprocess.run([str(ROOT / "cao"), "improve", "watch",
                            "--file", str(self.watch_path), "--dry-run"],
                           capture_output=True, text=True, env=env, timeout=12)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["status"], "INSPECTED")
        self.assertFalse((self.root / "state").exists())
        configured = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "daemon": {"feedback_watch_file": str(self.watch_path)}})
        self.assertEqual(product_config.daemon_defaults(configured).feedback_watch_file,
                         str(self.watch_path))
        for invalid in ("relative.json", "/bad/../watch.json", "", 12):
            with self.subTest(invalid=invalid):
                invalid_config = product_config.validate_config({
                    "schema": product_config.SCHEMA,
                    "daemon": {"feedback_watch_file": invalid}})
                with self.assertRaises(product_config.ProductConfigError):
                    product_config.daemon_defaults(invalid_config)
        self.case_path.write_text('{"schema":"x","schema":"y"}')
        p = subprocess.run([str(ROOT / "cao"), "improve", "watch",
                            "--file", str(self.watch_path)],
                           capture_output=True, text=True, env=env, timeout=12)
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertEqual(json.loads(p.stdout)["status"], "UNAVAILABLE")
        self.assertNotIn("Traceback", p.stderr)

    def test_unattested_source_never_starts_team(self):
        self.case["signals"][1]["proof"]["verdict"] = "UNKNOWN"
        # The accepted classification would contradict a missing source.
        self.write_json(self.case_path, self.case)
        with self.assertRaises(feedback.FeedbackError):
            feedback.tick(str(self.watch_path))


if __name__ == "__main__":
    unittest.main()
