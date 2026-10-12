"""Hermetic Team Execution contract and lifecycle tests."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cao import product_config, product_daemon, product_state, registry, team
from tests.test_product_task import FakeDriver, GIT_ENV, git_repo
from cao.state import StateError

A = "a" * 40
B = "b" * 40
C = "c" * 40


def task(name, path, *, mode="write", deps=None, reviewer=None, corrections=0):
    item = {
        "id": name, "role": "developer" if mode == "write" else "analyst",
        "repo": path, "prompt": "Implement a bounded change" if mode == "write" else "Analyze source",
        "mode": mode, "write_scope": ["README.md"] if mode == "write" else [],
        "depends_on": deps or [],
    }
    if reviewer:
        item["reviewer"] = reviewer
        item["max_corrections"] = corrections
    return item


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"XDG_STATE_HOME": str(self.root / "state"),
                    "XDG_CONFIG_HOME": str(self.root / "config")}
        self.config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "id": "test", "model": "opencode/default"},
            "task": {"agent_kind": "opencode"},
        })
        self.paths = [str(self.root / ("repo" + str(i))) for i in range(5)]
        self.roles = {
            "developer": {"instruction": "Implement precise edits", "model": "opencode/dev"},
            "reviewer": {"instruction": "Independently review commits", "model": "opencode/review"},
            "analyst": {"instruction": "Read the repository", "model": "opencode/read"},
        }

    def spec(self, tasks, *, team_id="squad", max_writers=2, max_parallel=4):
        return {
            "schema": team.SPEC_SCHEMA, "team_id": team_id,
            "roles": self.roles, "tasks": tasks,
            "max_writers": max_writers, "max_parallel": max_parallel,
        }

    def init(self, spec):
        team.initialize(spec, env=self.env)
        team.set_desired(spec["team_id"], "running", env=self.env)

    def tick(self, fake, *, team_id="squad", writers=0, heads=None):
        return team.tick(
            team_id, env=self.env, config=self.config, driver=None, execute=fake,
            writer_snapshot=lambda _: (writers, set()),
            head_reader=lambda repo: (heads or {}).get(repo, A))

    @staticmethod
    def done(**fields):
        payload = dict(status="complete", claim={"released": True, "released_head": A},
                       output="completed", output_truncated=False)
        payload.update(fields)
        return payload

    def test_manifest_rejects_unknown_and_missing_fields(self):
        valid = self.spec([task("a", self.paths[0])])
        self.assertEqual(team.validate_spec(valid)["max_writers"], 2)
        for change in (
            {"schema": "other"},
            {"team_id": "../squad"},
            {"tasks": []},
            {"roles": {}},
            {"max_writers": True},
            {"max_writers": 3},
            {"max_parallel": 99},
            {"unsupported": "x"},
        ):
            with self.subTest(change=change), self.assertRaises(team.TeamError):
                team.validate_spec({**valid, **change})

    def test_manifest_rejects_invalid_roles_tasks_and_scope(self):
        valid = self.spec([task("a", self.paths[0])])
        invalid_tasks = [
            [{**task("a", self.paths[0]), "role": "missing"}],
            [{**task("a", self.paths[0]), "role": []}],
            [{**task("a", self.paths[0]), "mode": "DELETE"}],
            [{**task("a", self.paths[0]), "write_scope": []}],
            [{**task("a", self.paths[0]), "repo": "relative"}],
            [{**task("a", self.paths[0]), "depends_on": ["missing"]}],
            [task("a", self.paths[0]), task("a", self.paths[1])],
            [task("a", self.paths[0], deps=["b"]), task("b", self.paths[1], deps=["a"])],
            [task("a", self.paths[0], deps=["a"])],
            [{**task("a", self.paths[0]), "reviewer": "missing"}],
            [{**task("a", self.paths[0]), "max_corrections": 1}],
            [task("a", self.paths[0], reviewer="reviewer", corrections=4)],
        ]
        for case in invalid_tasks:
            with self.subTest(case=case), self.assertRaises(team.TeamError):
                team.validate_spec({**valid, "tasks": case})
        with self.assertRaises(team.TeamError):
            team.validate_spec({**valid, "roles": {"bad": {"instruction": "x", "model": ["a"]}}})

    def test_immutable_manifest_private_state_and_pause(self):
        spec = self.spec([task("a", self.paths[0])])
        first = team.initialize(spec, env=self.env)
        self.assertEqual(first["status"], "PAUSED")
        self.assertEqual(first, team.initialize(spec, env=self.env))
        self.assertEqual(len(team.list_teams(env=self.env)), 1)
        self.assertEqual(os.stat(team.team_path("squad", self.env)).st_mode & 0o777, 0o600)
        second = team.set_desired("squad", "running", env=self.env)
        self.assertEqual(second["desired"], "running")
        team.set_desired("squad", "paused", env=self.env)
        self.assertEqual(team.status("squad", env=self.env)["desired"], "paused")
        with self.assertRaises(team.TeamError):
            team.initialize({**spec, "max_parallel": 2}, env=self.env)
        with self.assertRaises(team.TeamError):
            team.status("missing", env=self.env)

    def test_tampered_manifest_refused(self):
        spec = self.spec([task("a", self.paths[0])])
        team.initialize(spec, env=self.env)
        p = Path(team.team_path("squad", self.env))
        record = json.loads(p.read_text())
        record["spec"]["tasks"][0]["prompt"] = "modified after approval"
        p.write_text(json.dumps(record))
        with self.assertRaises(team.TeamError):
            team.status("squad", env=self.env)

    def test_invalid_json_never_reinterpreted_as_empty_team(self):
        spec = self.spec([task("a", self.paths[0])])
        team.initialize(spec, env=self.env)
        Path(team.team_path("squad", self.env)).write_text("{invalid")
        with self.assertRaises(StateError):
            team.status("squad", env=self.env)

    def test_role_routing_is_specific_and_reviewer_separate(self):
        spec = self.spec([task("a", self.paths[0], reviewer="reviewer")])
        self.init(spec)
        calls = []
        def fake(**kwargs):
            args = product_config.task_defaults(kwargs["config"]).agent_args
            calls.append((kwargs["phase"], kwargs["execution_id"], args))
            if kwargs["phase"] == "review":
                return self.done(output="CAO_REVIEW_VERDICT: APPROVED\nCAO_REVIEWED_HEAD: " + A)
            return self.done()
        self.tick(fake)
        self.tick(fake)
        self.assertEqual([x[0] for x in calls], ["work", "review"])
        self.assertEqual(calls[0][2], ("mini", "--standalone", "--model", "opencode/dev"))
        self.assertEqual(calls[1][2], ("mini", "--standalone", "--model", "opencode/review"))
        self.assertEqual(team.status("squad", env=self.env)["status"], "COMPLETE")

    def test_review_requests_correction_then_approves_new_commit(self):
        spec = self.spec([task("code", self.paths[0], reviewer="reviewer", corrections=1)])
        self.init(spec)
        prompts = []
        def fake(**kwargs):
            prompts.append((kwargs["phase"], kwargs["execution_id"], kwargs["prompt"]))
            if kwargs["phase"] == "work":
                return self.done()
            if kwargs["phase"] == "correction":
                return {"status": "complete", "claim": {"released": True, "released_head": B},
                        "output": "fixed in second commit"}
            if "-review-0-" in kwargs["execution_id"]:
                return self.done(output="CAO_REVIEW_VERDICT: CHANGES_REQUESTED\nCAO_REVIEWED_HEAD: " + A + "\nFix X")
            return self.done(output="CAO_REVIEW_VERDICT: APPROVED\nCAO_REVIEWED_HEAD: " + B)
        self.tick(fake, heads={self.paths[0]: A})
        self.tick(fake, heads={self.paths[0]: A})
        self.tick(fake, heads={self.paths[0]: B})
        self.tick(fake, heads={self.paths[0]: B})
        self.assertEqual([x[0] for x in prompts],
                         ["work", "review", "correction", "review"])
        self.assertNotEqual(prompts[1][1], prompts[3][1])
        self.assertIn("Fix X", prompts[2][2])
        self.assertEqual(team.status("squad", env=self.env)["tasks"]["code"]["head"], B)
        self.assertEqual(team.status("squad", env=self.env)["status"], "COMPLETE")

    def test_review_exhausted_budget_does_not_approve(self):
        self.init(self.spec([task("a", self.paths[0], reviewer="reviewer")]))
        def fake(**kwargs):
            if kwargs["phase"] == "review":
                return self.done(output="CAO_REVIEW_VERDICT: CHANGES_REQUESTED\nCAO_REVIEWED_HEAD: " + A)
            return self.done()
        self.tick(fake)
        result = self.tick(fake)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["tasks"]["a"]["error"], "REVIEW_BUDGET_EXHAUSTED")

    def test_unverified_review_marker_and_head_drift_hold(self):
        for idx, output, head in ((0, "looks good", A),
                                  (1, "CAO_REVIEW_VERDICT: APPROVED\nCAO_REVIEWED_HEAD: " + B, A),
                                  (2, "CAO_REVIEW_VERDICT: APPROVED\nCAO_REVIEWED_HEAD: " + A, B)):
            team_id = "r" + str(idx)
            self.init(self.spec([task("a", self.paths[0], reviewer="reviewer")], team_id=team_id))
            def fake(**kwargs):
                return self.done(output=output) if kwargs["phase"] == "review" else self.done()
            self.tick(fake, team_id=team_id)
            result = self.tick(fake, team_id=team_id, heads={self.paths[0]: head})
            self.assertIn(result["status"], ("BLOCKED",))
            self.assertNotEqual(result["tasks"]["a"]["status"], "complete")

    def test_truncated_review_never_grants_approval(self):
        self.init(self.spec([task("a", self.paths[0], reviewer="reviewer")]))
        def fake(**kwargs):
            if kwargs["phase"] == "review":
                return {"status": "complete", "claim": {"released": True, "released_head": A},
                        "output": "CAO_REVIEW_VERDICT: APPROVED\nCAO_REVIEWED_HEAD: " + A,
                        "output_truncated": True}
            return self.done()
        self.tick(fake)
        self.assertEqual(self.tick(fake)["status"], "BLOCKED")

    def test_dependency_handoff_uses_only_completed_predecessors(self):
        spec = self.spec([
            task("build", self.paths[0], reviewer="reviewer"),
            task("qa", self.paths[1], mode="read", deps=["build"]),
        ])
        self.init(spec)
        calls = []
        def fake(**kwargs):
            calls.append((kwargs["phase"], kwargs["task"]["id"], kwargs["prompt"]))
            if kwargs["phase"] == "review":
                return self.done(output="CAO_REVIEW_VERDICT: APPROVED\nCAO_REVIEWED_HEAD: " + A)
            return self.done()
        self.tick(fake)
        self.assertEqual([x[1] for x in calls], ["build"])
        self.tick(fake)
        self.assertEqual([x[1] for x in calls], ["build", "build", "qa"])
        self.assertIn("Dependency build accepted at " + A, calls[-1][2])
        self.assertEqual(team.status("squad", env=self.env)["status"], "COMPLETE")

    def test_global_two_writer_cap_even_with_stale_snapshot(self):
        spec = self.spec([task("a", self.paths[0]), task("b", self.paths[1]),
                          task("c", self.paths[2])])
        self.init(spec)
        started = []
        def fake(**kwargs):
            started.append(kwargs["execution_id"])
            return {"status": "running"}
        self.tick(fake)
        self.assertEqual(len(started), 2)
        s = team.status("squad", env=self.env)
        self.assertEqual(s["tasks"]["c"]["status"], "pending")
        self.tick(fake)
        self.assertEqual(len(started), 4)
        self.assertEqual(team.status("squad", env=self.env)["tasks"]["c"]["status"], "pending")

    def test_external_writer_cap_and_read_bypass(self):
        spec = self.spec([task("a", self.paths[0]), task("read", self.paths[1], mode="read")],
                         max_writers=1)
        self.init(spec)
        started = []
        def fake(**kwargs):
            started.append(kwargs["task"]["id"])
            return self.done()
        self.tick(fake, writers=1)
        self.assertEqual(started, ["read"])
        self.assertEqual(team.status("squad", env=self.env)["tasks"]["a"]["status"], "pending")

    def test_same_checkout_not_dispatched_alongside_existing_write(self):
        self.init(self.spec([task("a", self.paths[0])]))
        called = []
        result = team.tick("squad", config=self.config, driver=None, env=self.env,
                           execute=lambda **x: called.append(x),
                           writer_snapshot=lambda _: (1, {self.paths[0]}))
        self.assertEqual(called, [])
        self.assertEqual(result["tasks"]["a"]["status"], "pending")

    def test_crash_before_external_effect_reuses_saved_prompt_and_execution(self):
        self.init(self.spec([task("a", self.paths[0])]))
        attempts = []
        def crashed(**kwargs):
            attempts.append((kwargs["execution_id"], kwargs["prompt"]))
            raise RuntimeError("simulated abrupt process death")
        failed = self.tick(crashed)
        self.assertEqual(failed["tasks"]["a"]["status"], "unknown")
        self.assertEqual(failed["tasks"]["a"]["error"], "RuntimeError")
        team.resume("squad", "a", env=self.env)
        self.tick(lambda **kwargs: (attempts.append((kwargs["execution_id"], kwargs["prompt"]))
                                  or self.done()))
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(team.status("squad", env=self.env)["status"], "COMPLETE")

    def test_unknown_provider_refuses_reprompt_and_remains_unknown(self):
        self.init(self.spec([task("a", self.paths[0])]))
        called = []
        def down(**kwargs):
            called.append(kwargs["execution_id"])
            raise product_config.ProductConfigError("provider down")
        self.tick(down)
        self.tick(down)
        self.assertEqual(len(called), 1)
        self.assertEqual(team.status("squad", env=self.env)["status"], "BLOCKED")

    def test_missing_released_commit_not_misreported_complete(self):
        self.init(self.spec([task("a", self.paths[0])]))
        self.tick(lambda **kwargs: {"status": "complete", "claim": {"released": False}})
        self.assertEqual(team.status("squad", env=self.env)["tasks"]["a"]["status"], "unknown")

    def test_paused_team_has_no_execution(self):
        team.initialize(self.spec([task("a", self.paths[0])]), env=self.env)
        called = []
        team.tick("squad", config=self.config, driver=None, env=self.env,
                  execute=lambda **x: called.append(x))
        self.assertEqual(called, [])

    def test_flock_blocks_concurrent_dispatch(self):
        team.initialize(self.spec([task("a", self.paths[0])]), env=self.env)
        with team.dispatch_lock(self.env):
            with self.assertRaises(team.TeamError):
                team.set_desired("squad", "running", env=self.env)

    def test_unreadable_registry_fail_closed(self):
        self.init(self.spec([task("a", self.paths[0])]))
        result = team.tick(
            "squad", config=self.config, driver=None, env=self.env,
            registry_db=str(self.root / "missing.sqlite3"),
            execute=lambda **x: self.fail("no task should launch"))
        self.assertEqual(result["tasks"]["a"]["status"], "pending")
        self.assertEqual(result["tasks"]["a"]["error"], "REGISTRY_OCCUPANCY_UNKNOWN")

    def test_team_daemon_tick_uses_existing_supervision_loop(self):
        record = product_state.default_daemon_record()
        record["desired"] = "running"
        product_state.write_daemon(record, self.env)
        class EmptyDriver:
            def workspace_list(self):
                return []
        daemon = product_daemon.Daemon(
            driver=EmptyDriver(), config=self.config, env=self.env)
        with mock.patch("cao.team.tick_all", return_value=[
            {"team_id": "squad", "status": "RUNNING"}
        ]) as called:
            result = daemon.tick()
        called.assert_called_once()
        self.assertEqual(result["status"], "RUNNING")
        self.assertTrue(any(x.get("action") == "team_reconcile"
                            for x in result["effects"]))

    def test_failed_team_adapter_keeps_daemon_alive_with_unknown(self):
        record = product_state.default_daemon_record()
        record["desired"] = "running"
        product_state.write_daemon(record, self.env)
        class EmptyDriver:
            def workspace_list(self):
                return []
        daemon = product_daemon.Daemon(
            driver=EmptyDriver(), config=self.config, env=self.env)
        with mock.patch("cao.team.tick_all", side_effect=team.TeamError("unsafe")):
            result = daemon.tick()
        self.assertEqual(result["status"], "RUNNING")
        self.assertTrue(any(x.get("action") == "team_hold" and
                            x.get("status") == "UNKNOWN"
                            for x in result["effects"]))

    def test_team_missing_real_result_receipt_never_marks_read_complete(self):
        # A runtime can return DONE without actually producing a useful model
        # answer (quota denied, boot splash, malformed prompt).
        ident = "ct-test-team-actor-work-0"
        driver = FakeDriver()
        driver.read_text = "Model credits exhausted. Agent done."
        record = {"checkpoint": {"agent_name": "actor", "prompt_ack": "accepted"}}
        with mock.patch("cao.product_task.run_task", return_value={
                    "status": "running", "claim": {"released": False}}) as launched, \
                mock.patch("cao.product_state.read_task", return_value=record):
            result = team._execute(
                execution_id=ident,
                task={"repo": self.paths[0], "mode": "read", "write_scope": []},
                prompt="read task", phase="work", config=self.config,
                driver=driver, env=self.env, wait_ms=1000, registry_db="unused")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"]["code"], "TEAM_RESULT_UNVERIFIED")
        launched.assert_called_once()
        self.assertEqual(launched.call_args.kwargs["wait_ms"], 0)

    def test_recovered_complete_with_receipt_rechecks_read_pane_close(self):
        ident = "ct-test-team-actor-work-0"
        answer = "CAO_TEAM_RESULT: " + team._result_tag(ident)
        payload = {"status": "complete", "claim": {
            "released": True, "released_head": A}, "output": answer}
        record = {"checkpoint": {"agent_name": "reader",
                                 "workspace_id": "w9", "pane_id": "w9:p1"}}
        with mock.patch("cao.product_task.run_task", return_value=payload) as run, \
                mock.patch("cao.product_state.read_task", return_value=record), \
                mock.patch("cao.product_task._close_owned_pane",
                           return_value=("w9:p1", None)) as close, \
                mock.patch("cao.product_state.update_task") as saved:
            result = team._execute(
                execution_id=ident,
                task={"repo": self.paths[0], "mode": "read", "write_scope": []},
                prompt="read task", phase="work", config=self.config,
                driver=FakeDriver(), env=self.env, wait_ms=1000,
                registry_db="unused")
        self.assertEqual(result["status"], "complete")
        run.assert_called_once()
        close.assert_called_once()
        saved.assert_called_once()

    def test_already_terminal_task_without_durable_receipt_cannot_advance_team(self):
        ident = "ct-test-team-actor-work-0"
        payload = {"status": "complete", "claim": {
            "released": True, "released_head": A}, "output": "only terminal splash"}
        with mock.patch("cao.product_task.run_task", return_value=payload) as runner:
            result = team._execute(
                execution_id=ident,
                task={"repo": self.paths[0], "mode": "read", "write_scope": []},
                prompt="read task", phase="work", config=self.config,
                driver=FakeDriver(), env=self.env, wait_ms=1000,
                registry_db="unused")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"]["code"], "TEAM_DURABLE_RESULT_UNVERIFIED")
        runner.assert_called_once()

    def test_team_idle_before_answer_remains_running_and_reuses_prompt(self):
        ident = "ct-test-team-actor-work-0"
        driver = FakeDriver()
        driver.wait_state = "idle"
        record = {"checkpoint": {"agent_name": "actor", "prompt_ack": "accepted"}}
        with mock.patch("cao.product_task.run_task", return_value={
                    "status": "running", "claim": {"released": False}}) as launched, \
                mock.patch("cao.product_state.read_task", return_value=record):
            result = team._execute(
                execution_id=ident,
                task={"repo": self.paths[0], "mode": "read", "write_scope": []},
                prompt="read task", phase="work", config=self.config,
                driver=driver, env=self.env, wait_ms=1000, registry_db="unused")
        self.assertEqual(result["status"], "running")
        launched.assert_called_once()
        self.assertNotIn("pane_close", driver.names())

    def test_receipt_token_not_present_in_submitted_prompt(self):
        spec = self.spec([task("a", self.paths[0], mode="read")])
        normalized = team.validate_spec(spec)
        slot = {"phase": "work", "round": 0, "output": None}
        prompt = team._prompt(normalized["tasks"][0], normalized, slot, {})
        ident = team._execution_id("squad", "a", "work", 0)
        import re
        challenge = re.search(r"CAO_TEAM_NONCE_V2: ([0-9a-f]{16})", prompt)
        self.assertIsNotNone(challenge)
        self.assertEqual(challenge.group(1), team._result_challenge(ident))
        self.assertEqual(team._receipt_for(ident, prompt),
                         "CAO_TEAM_RESULT: " + challenge.group(1))
        self.assertNotIn(team._receipt_for(ident, prompt), prompt)
        # A pre-upgrade durable task prompt keeps its exact old requirement.
        self.assertEqual(team._receipt_for(ident, "legacy prompt"),
                         "CAO_TEAM_RESULT: " + team._result_tag(ident))

    def test_result_receipt_matcher_tolerates_bounded_terminal_indentation(self):
        ident = "ct-squad-reader-work-0"
        receipt = team._receipt_for(ident, "legacy prompt")
        for indent in ("", " ", "  ", "   ", "    ", "\t", "\t\t",
                       "\t\t\t", "\t\t\t\t", " \t", "  \t "):
            with self.subTest(indent=indent):
                self.assertTrue(team._receipt_present(
                    "notes\n" + indent + receipt + "\nmore notes", receipt))

    def test_result_receipt_matcher_rejects_altered_lines(self):
        ident = "ct-squad-reader-work-0"
        receipt = team._receipt_for(ident, "legacy prompt")
        challenge = team._result_challenge(ident)
        for text in (
            None,
            "analysis without any receipt line",
            "     " + receipt,                       # indentation past the bound
            "\t    " + receipt,                       # five indentation columns
            " " + receipt + " plus trailing text",
            " " + receipt[:-1],                       # truncated nonce
            " CAO_TEAM_RESULT: " + ("f" * 16),        # changed nonce
            " CAO_TEAM_NONCE_V2: " + challenge,       # prompt echo, not a receipt
            "summary " + receipt,                     # leading text on the line
        ):
            with self.subTest(text=text):
                self.assertFalse(team._receipt_present(text, receipt))

    def test_indented_live_result_receipt_marks_task_complete(self):
        ident = "ct-squad-reader-work-0"
        challenge = team._result_challenge(ident)
        prompt = "Analyze source\nCAO_TEAM_NONCE_V2: " + challenge
        receipt = team._receipt_for(ident, prompt)
        driver = FakeDriver()
        driver.read_text = "analysis complete\n  " + receipt + "\n"
        checkpoint = {"checkpoint": {"agent_name": "actor",
                                     "prompt_ack": "accepted"}}
        with mock.patch("cao.product_task.run_task", side_effect=[
                {"status": "running", "claim": {"released": False}},
                {"status": "complete", "claim": {
                    "released": True, "released_head": A},
                 "output": receipt}]), \
                mock.patch("cao.product_state.read_task", return_value=checkpoint):
            result = team._execute(
                execution_id=ident,
                task={"repo": self.paths[0], "mode": "write",
                      "write_scope": ["README.md"]},
                prompt=prompt, phase="work", config=self.config,
                driver=driver, env=self.env, wait_ms=1000,
                registry_db="unused")
        self.assertEqual(result["status"], "complete")

    def test_indented_durable_result_receipt_marks_task_complete(self):
        ident = "ct-squad-reader-work-0"
        challenge = team._result_challenge(ident)
        prompt = "Analyze source\nCAO_TEAM_NONCE_V2: " + challenge
        receipt = team._receipt_for(ident, prompt)
        payload = {"status": "complete", "claim": {
            "released": True, "released_head": A},
            "output": "final summary\n\t" + receipt + "\n"}
        record = {"checkpoint": {"agent_name": "reader",
                                 "workspace_id": "w9", "pane_id": "w9:p1"}}
        with mock.patch("cao.product_task.run_task", return_value=payload) as runner, \
                mock.patch("cao.product_state.read_task", return_value=record), \
                mock.patch("cao.product_task._close_owned_pane",
                           return_value=("w9:p1", None)), \
                mock.patch("cao.product_state.update_task"):
            result = team._execute(
                execution_id=ident,
                task={"repo": self.paths[0], "mode": "read", "write_scope": []},
                prompt=prompt, phase="work", config=self.config,
                driver=FakeDriver(), env=self.env, wait_ms=1000,
                registry_db="unused")
        self.assertEqual(result["status"], "complete")
        runner.assert_called_once()

    def test_over_indented_or_altered_receipts_fail_closed_on_both_checks(self):
        ident = "ct-squad-reader-work-0"
        challenge = team._result_challenge(ident)
        prompt = "Analyze source\nCAO_TEAM_NONCE_V2: " + challenge
        receipt = team._receipt_for(ident, prompt)
        altered = ("     " + receipt,             # indentation past the bound
                   "  " + receipt + " extra",      # appended text
                   "  CAO_TEAM_RESULT: " + ("f" * 16))  # changed nonce
        for text in altered:
            driver = FakeDriver()
            driver.read_text = "analysis\n" + text
            checkpoint = {"checkpoint": {"agent_name": "actor",
                                         "prompt_ack": "accepted"}}
            with self.subTest(check="live", text=text), \
                    mock.patch("cao.product_task.run_task",
                               return_value={"status": "running"}), \
                    mock.patch("cao.product_state.read_task",
                               return_value=checkpoint):
                result = team._execute(
                    execution_id=ident,
                    task={"repo": self.paths[0], "mode": "write",
                          "write_scope": ["README.md"]},
                    prompt=prompt, phase="work", config=self.config,
                    driver=driver, env=self.env, wait_ms=1000,
                    registry_db="unused")
            self.assertEqual(result["error"]["code"], "TEAM_RESULT_UNVERIFIED")
        for text in altered:
            payload = {"status": "complete", "claim": {
                "released": True, "released_head": A}, "output": text}
            with self.subTest(check="durable", text=text), \
                    mock.patch("cao.product_task.run_task", return_value=payload):
                result = team._execute(
                    execution_id=ident,
                    task={"repo": self.paths[0], "mode": "read", "write_scope": []},
                    prompt=prompt, phase="work", config=self.config,
                    driver=FakeDriver(), env=self.env, wait_ms=1000,
                    registry_db="unused")
            self.assertEqual(result["error"]["code"],
                             "TEAM_DURABLE_RESULT_UNVERIFIED")

    def test_team_read_agent_pane_must_be_closed_before_handback(self):
        ident = "ct-squad-read-work-0"
        payload = {"status": "complete", "claim": {
            "released": True, "released_head": A},
            "output": "analysis\nCAO_TEAM_RESULT: " + team._result_tag("ct-squad-read-work-0")}
        checkpoint = {"checkpoint": {"workspace_id": "ws1",
                                      "pane_id": "ws1:p1",
                                      "agent_name": "test-reader",
                                      "prompt_ack": "accepted"}}
        driver = FakeDriver()
        driver.read_text = "CAO_TEAM_RESULT: " + team._result_tag(ident)
        with mock.patch("cao.product_task.run_task", side_effect=[
                {"status": "running"}, payload]), \
                mock.patch("cao.product_state.read_task", return_value=checkpoint), \
                mock.patch("cao.product_task._close_owned_pane", return_value=("ws1:p1", None)), \
                mock.patch("cao.product_state.update_task") as updated:
            result = team._execute(
                execution_id=ident,
                task={"repo": self.paths[0], "mode": "read", "write_scope": []},
                prompt="read", phase="work", config=self.config,
                driver=driver, env=self.env, wait_ms=1000, registry_db="ignored")
        self.assertEqual(result["status"], "complete")
        updated.assert_called_once()

        with mock.patch("cao.product_task.run_task", side_effect=[
                {"status": "running"}, payload]), \
                mock.patch("cao.product_state.read_task", return_value=checkpoint), \
                mock.patch("cao.product_task._close_owned_pane", return_value=(None, "still alive")):
            result = team._execute(
                execution_id=ident,
                task={"repo": self.paths[0], "mode": "read", "write_scope": []},
                prompt="read", phase="work", config=self.config,
                driver=driver, env=self.env, wait_ms=1000, registry_db="ignored")
        self.assertEqual(result["status"], "unknown")

    def test_manual_resume_unknown_reuses_exact_execution_identity(self):
        self.init(self.spec([task("a", self.paths[0])]))
        calls = []
        def fail(**kwargs):
            calls.append((kwargs["execution_id"], kwargs["prompt"]))
            raise product_config.ProductConfigError("route unavailable")
        self.tick(fail)
        old = team.status("squad", env=self.env)["tasks"]["a"]
        self.assertEqual(old["status"], "unknown")
        with self.assertRaises(team.TeamError):
            team.resume("squad", "missing", env=self.env)
        team.resume("squad", "a", env=self.env)
        self.tick(lambda **kwargs: (calls.append((kwargs["execution_id"], kwargs["prompt"]))
                                  or self.done()))
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(team.status("squad", env=self.env)["status"], "COMPLETE")
        with self.assertRaises(team.TeamError):
            team.resume("squad", "a", env=self.env)

    def test_invalid_read_receipt_allows_one_explicit_fresh_retry_after_stop(self):
        spec = self.spec([task("a", self.paths[0], mode="read"),
                          task("dependent", self.paths[1], mode="read",
                               deps=["a"])])
        self.init(spec)
        self.tick(lambda **kwargs: {
            "status": "unknown",
            "error": {"code": "TEAM_RESULT_UNVERIFIED"}})
        snapshot = team.status("squad", env=self.env)["tasks"]
        old_id = snapshot["a"]["execution_id"]
        self.assertEqual(snapshot["dependent"]["status"], "pending")
        team.set_desired("squad", "paused", env=self.env)
        old_record = {
            "task_id": old_id, "run_id": "cao-team-squad",
            "work_key": old_id, "repo": self.paths[0], "mode": "READ",
            "scope": [], "status": "stopped",
            "checkpoint": {"pane_id": "w1:p1"}, "pane_closed": "w1:p1",
            "claim": {"claim_id": 42, "status": "RELEASED", "released": True,
                      "outcome": "abandon"}}
        ledger_row = {"run_id": "cao-team-squad", "work_key": old_id,
                      "owner": "cao-product", "mode": "READ",
                      "released_at": "2026-01-01T01:00:00Z",
                      "outcome": "abandon"}
        database = mock.Mock()
        database.execute.return_value.fetchone.return_value = ledger_row
        with mock.patch("cao.product_state.read_task", return_value=old_record), \
                mock.patch("cao.registry.connect_readonly", return_value=database):
            retried = team.retry_read("squad", "a", env=self.env)
        self.assertEqual(retried["tasks"]["a"]["status"], "pending")
        self.assertEqual(retried["tasks"]["a"]["retry_no"], 1)
        self.assertEqual(retried["tasks"]["dependent"]["status"], "pending")
        self.assertEqual(retried["desired"], "paused")
        with self.assertRaises(team.TeamError):
            team.retry_read("squad", "a", env=self.env)
        dispatched = []
        team.set_desired("squad", "running", env=self.env)
        self.tick(lambda **kw: (dispatched.append((kw["execution_id"],
                  kw["prompt"])) or self.done()))
        self.assertEqual(len(dispatched), 2)
        self.assertNotEqual(dispatched[0][0], old_id)
        self.assertEqual(dispatched[0][0], team._execution_id(
            "squad", "a", "work", 0, 1))
        self.assertIn("CAO_TEAM_NONCE_V2:", dispatched[0][1])
        self.tick(lambda **kw: self.done())
        self.assertEqual(team.status("squad", env=self.env)["status"], "COMPLETE")

    def test_retry_refuses_unverified_stop_or_wrong_ledger(self):
        self.init(self.spec([task("a", self.paths[0], mode="read")]))
        self.tick(lambda **kw: {
            "status": "unknown",
            "error": {"code": "TEAM_RESULT_UNVERIFIED"}})
        old_id = team.status("squad", env=self.env)["tasks"]["a"]["execution_id"]
        with self.assertRaises(team.TeamError):
            team.retry_read("squad", "a", env=self.env)
        team.set_desired("squad", "paused", env=self.env)
        stale = {"task_id": old_id, "run_id": "cao-team-squad",
                 "work_key": old_id, "repo": self.paths[0], "mode": "READ",
                 "scope": [], "status": "running",
                 "checkpoint": {"pane_id": "w1:p1"}, "pane_closed": None,
                 "claim": {"claim_id": 42, "status": "RESUMED",
                           "released": False}}
        with mock.patch("cao.product_state.read_task", return_value=stale):
            with self.assertRaises(team.TeamError):
                team.retry_read("squad", "a", env=self.env)
        stale.update(status="stopped", pane_closed="w1:p1")
        stale["claim"].update(status="RELEASED", released=True, outcome="abandon")
        db = mock.Mock()
        db.execute.return_value.fetchone.return_value = {
            "run_id": "cao-team-squad", "work_key": old_id,
            "owner": "another-owner", "mode": "READ",
            "released_at": "2026-01-01T01:00:00Z", "outcome": "abandon"}
        with mock.patch("cao.product_state.read_task", return_value=stale), \
                mock.patch("cao.registry.connect_readonly", return_value=db):
            with self.assertRaises(team.TeamError):
                team.retry_read("squad", "a", env=self.env)
        self.assertEqual(team.status("squad", env=self.env)["tasks"]["a"]["status"],
                         "unknown")

    def test_review_and_writer_same_checkout_cannot_overlap(self):
        initial = task("reviewed", self.paths[0], reviewer="reviewer")
        another = task("other", self.paths[0])
        self.init(self.spec([initial, another]))
        def fake(**kwargs):
            if kwargs["phase"] == "review":
                return {"status": "running"}
            if kwargs["task"]["id"] == "reviewed":
                return self.done()
            self.fail("another writer started during review")
        self.tick(fake)
        self.tick(fake)
        snapshot = team.status("squad", env=self.env)["tasks"]
        self.assertEqual(snapshot["reviewed"]["phase"], "review")
        self.assertEqual(snapshot["reviewed"]["status"], "running")
        self.assertEqual(snapshot["other"]["status"], "pending")

    def test_role_model_mismatch_refuses_start_without_substitution(self):
        self.init(self.spec([task("a", self.paths[0])]))
        raw = self.config.as_dict()
        raw["task"] = {"agent_kind": "other-agent"}
        incompatible = product_config.validate_config(raw)
        result = team.tick("squad", config=incompatible, driver=None, env=self.env,
                           execute=lambda **kw: self.fail("must not start"),
                           writer_snapshot=lambda _: (0, set()))
        self.assertEqual(result["tasks"]["a"]["status"], "unknown")

    def test_reviewer_role_must_be_distinct(self):
        invalid = task("a", self.paths[0], reviewer="developer")
        with self.assertRaises(team.TeamError):
            team.validate_spec(self.spec([invalid]))

    def test_invalid_runner_payload_never_completes(self):
        self.init(self.spec([task("a", self.paths[0])]))
        result = self.tick(lambda **kw: ["not-a-task"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["tasks"]["a"]["error"], "TASK_PAYLOAD_INVALID")

    def test_real_read_quota_denial_keeps_exact_open_claim_and_single_prompt(self):
        # Real product_task + Git + SQLite Registry, with deterministic fake
        # runtime returning DONE but no verified model answer receipt.
        repo = git_repo(self.root, name="no-credit")
        db = str(self.root / "no-credit-ledger.sqlite3")
        driver = FakeDriver()
        driver.read_text = "Model requires credits. No generation occurred."
        ident = "ct-no-credit-real-work-0"
        opts = dict(
            execution_id=ident,
            task={"repo": str(repo), "mode": "read", "write_scope": []},
            prompt="Analyze source and emit a computed challenge receipt",
            phase="work", config=self.config, driver=driver, env=self.env,
            registry_db=db, wait_ms=1000,
        )
        first = team._execute(**opts)
        self.assertEqual(first["status"], "unknown", first)
        self.assertEqual(first["error"]["code"], "TEAM_RESULT_UNVERIFIED")
        second = team._execute(**opts)
        self.assertEqual(second["status"], "unknown", second)
        self.assertEqual(driver.names().count("agent_prompt"), 1)
        self.assertNotIn("pane_close", driver.names())
        record = product_state.read_task(ident, self.env)
        self.assertEqual(record["status"], "running")
        self.assertIsNone(record.get("pane_closed"))
        c = registry.connect_readonly(db)
        try:
            claims = c.execute(
                "SELECT mode,released_at FROM claims ORDER BY claim_id"
            ).fetchall()
            self.assertEqual(len(claims), 1)
            self.assertEqual(claims[0]["mode"], "READ")
            self.assertIsNone(claims[0]["released_at"])
        finally:
            c.close()

    def test_blocked_runtime_does_not_release_team_claim(self):
        driver = FakeDriver()
        driver.wait_state = "blocked"
        ident = "ct-blocked-runtime-work-0"
        record = {"checkpoint": {"agent_name": "agent",
                                 "prompt_ack": "accepted"}}
        with mock.patch("cao.product_task.run_task",
                        return_value={"status": "running",
                                      "claim": {"released": False}}) as run, \
                mock.patch("cao.product_state.read_task", return_value=record):
            result = team._execute(
                execution_id=ident,
                task={"repo": self.paths[0], "mode": "read", "write_scope": []},
                prompt="read", phase="work", config=self.config,
                driver=driver, env=self.env, wait_ms=1000, registry_db="unused")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"]["code"], "TEAM_AGENT_BLOCKED")
        run.assert_called_once()

    def test_unreadable_runtime_result_cannot_advance_or_release(self):
        driver = FakeDriver()
        def unreadable(*args, **kwargs):
            from cao.product_driver import DriverTransportError
            raise DriverTransportError("transient")
        driver.agent_read = unreadable
        record = {"checkpoint": {"agent_name": "agent",
                                 "prompt_ack": "accepted"}}
        with mock.patch("cao.product_task.run_task",
                        return_value={"status": "running",
                                      "claim": {"released": False}}) as run, \
                mock.patch("cao.product_state.read_task", return_value=record):
            result = team._execute(
                execution_id="ct-read-error-work-0",
                task={"repo": self.paths[0], "mode": "read", "write_scope": []},
                prompt="read", phase="work", config=self.config,
                driver=driver, env=self.env, wait_ms=1000, registry_db="unused")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"]["code"], "TEAM_RESULT_UNREADABLE")
        run.assert_called_once()

    def test_real_git_registry_and_runtime_adapter_team_write_then_review(self):
        # This is the actual product_task/Registry integration with real
        # scoped Git commits and a deterministic Herdr-shaped fake. It does
        # not contact a provider or the host's shared Registry.
        repo = git_repo(self.root, name="integrated", files=("a.txt",))
        bare = self.root / "bare.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env={**os.environ, **GIT_ENV})
        def git(*arguments):
            return subprocess.run(["git", "-C", str(repo),
                                   "-c", "commit.gpgsign=false", *arguments],
                                  check=True, capture_output=True, text=True,
                                  env={**os.environ, **GIT_ENV}).stdout.strip()
        git("remote", "add", "origin", str(bare))
        git("push", "-u", "origin", "main")
        db = str(self.root / "team-registry.sqlite3")
        registry.connect(db).close()
        model_routes = []

        class SimulatedHerdr(FakeDriver):
            def workspace_create(self, label, cwd=None):
                self._note("workspace_create", label, cwd)
                ident = "w" + str(len(self.workspaces) + 1)
                self.workspaces.append({
                    "workspace_id": ident, "label": label,
                    "active_tab_id": ident + ":t1",
                })
                return {"workspace_id": ident, "tab_id": ident + ":t1",
                        "pane_id": ident + ":p1"}

            def pane_list(self, workspace_id):
                self._note("pane_list", workspace_id)
                pane = workspace_id + ":p1"
                return [] if pane in self.closed_panes else [{
                    "pane_id": pane, "workspace_id": workspace_id,
                    "agent_status": "done",
                }]

            def pane_get(self, pane_id):
                self._note("pane_get", pane_id)
                if pane_id in self.closed_panes:
                    from cao import product_driver
                    raise product_driver.DriverNotFound("closed")
                return {"pane_id": pane_id,
                        "workspace_id": pane_id.split(":")[0]}

            def agent_start(self, name, kind, pane_id, *, argv=(), timeout_ms=None):
                model_routes.append(tuple(argv))
                return super().agent_start(
                    name, kind, pane_id, argv=argv, timeout_ms=timeout_ms)

            def agent_prompt(self, target, text, *, timeout_ms=None, wait=False):
                if "Role: Implement precise edits" in text:
                    (repo / "a.txt").write_text("changed\n")
                    git("add", "--", "a.txt")
                    git("commit", "-m", "team writer")
                    self.read_text = "Committed scoped change"
                elif "Role: Independently review commits" in text:
                    self.read_text = (
                        "Checked Git diff and tests.\n"
                        "CAO_REVIEW_VERDICT: APPROVED\nCAO_REVIEWED_HEAD: "
                        + git("rev-parse", "HEAD")
                    )
                challenge = team._result_challenge(
                    team._execution_id("squad", "feature",
                                       "review" if "Role: Independently review commits" in text
                                       else "work", 0)
                )
                self.read_text += "\nCAO_TEAM_RESULT: " + challenge
                return super().agent_prompt(
                    target, text, timeout_ms=timeout_ms, wait=wait)

        driver = SimulatedHerdr()
        spec = self.spec([
            {**task("feature", str(repo), reviewer="reviewer", corrections=1),
             "write_scope": ["a.txt"]}
        ])
        self.init(spec)
        first = team.tick("squad", config=self.config, driver=driver,
                          env=self.env, registry_db=db)
        self.assertEqual(first["tasks"]["feature"]["phase"], "review", first)
        second = team.tick("squad", config=self.config, driver=driver,
                           env=self.env, registry_db=db)
        self.assertEqual(second["status"], "COMPLETE")
        self.assertEqual([x[-1] for x in model_routes],
                         ["opencode/dev", "opencode/review"])
        self.assertEqual(driver.names().count("agent_prompt"), 2)
        self.assertEqual(driver.closed_panes, {"w1:p1", "w2:p1"})
        self.assertEqual(git("status", "--porcelain"), "")
        c = registry.connect_readonly(db)
        try:
            claims = c.execute(
                "SELECT mode,outcome,released_at FROM claims ORDER BY claim_id"
            ).fetchall()
        finally:
            c.close()
        self.assertEqual(len(claims), 2)
        self.assertEqual([x["mode"] for x in claims], ["WRITE", "READ"])
        self.assertTrue(all(x["released_at"] for x in claims))

    def test_colliding_hyphenated_team_task_names_never_reuse_execution(self):
        ids = [
            team._execution_id("a-b", "c", "work", 0),
            team._execution_id("a", "b-c", "work", 0),
        ]
        self.assertNotEqual(ids[0], ids[1])
        calls = []
        for name, ident, repo in (("a-b", "c", self.paths[0]),
                                  ("a", "b-c", self.paths[1])):
            self.init(self.spec([task(ident, repo)], team_id=name))
            self.tick(lambda **kw: (calls.append(kw["execution_id"])
                                  or self.done()), team_id=name)
        self.assertEqual(calls, ids)
        self.assertEqual(len(set(calls)), 2)

    def test_blocked_team_drains_running_siblings_without_new_dispatch(self):
        self.init(self.spec([task("a", self.paths[0]), task("b", self.paths[1]),
                             task("c", self.paths[2])]))
        counts = {}
        def fake(**kwargs):
            name = kwargs["task"]["id"]
            counts[name] = counts.get(name, 0) + 1
            if name == "a" and counts[name] == 2:
                return {"status": "unknown", "error": {"code": "PROVIDER_DOWN"}}
            if name == "b" and counts[name] == 3:
                return self.done()
            return {"status": "running"}
        self.tick(fake)
        self.assertEqual((counts["a"], counts["b"]), (1, 1))
        self.assertNotIn("c", counts)
        self.tick(fake)
        self.assertEqual(team.status("squad", env=self.env)["status"], "BLOCKED")
        self.assertEqual(team.status("squad", env=self.env)["tasks"]["b"]["status"], "running")
        original = team.tick
        def adapted(team_id, *, config, driver, env=None, registry_db=None):
            return original(team_id, config=config, driver=driver, env=env,
                            execute=fake, writer_snapshot=lambda _: (0, set()))
        with mock.patch("cao.team.tick", side_effect=adapted) as called:
            outcome = team.tick_all(config=self.config, driver=None, env=self.env)
        called.assert_called_once()
        self.assertEqual(outcome[0]["tasks"]["b"]["status"], "complete")
        self.assertEqual(outcome[0]["tasks"]["c"]["status"], "pending")
        self.assertNotIn("c", counts)

    def test_registry_failure_on_pending_does_not_skip_running_sibling(self):
        self.init(self.spec([task("a", self.paths[0]), task("b", self.paths[1])],
                            max_writers=2))
        calls = []
        def fake(**kw):
            calls.append(kw["task"]["id"])
            return self.done() if len(calls) > 1 else {"status": "running"}
        self.tick(fake, writers=1)
        self.assertEqual(calls, ["a"])
        result = team.tick("squad", config=self.config, driver=None,
                           env=self.env, execute=fake,
                           writer_snapshot=lambda _: (_ for _ in ())
                           .throw(team.TeamError("registry unavailable")))
        self.assertEqual(result["tasks"]["a"]["status"], "complete")
        self.assertEqual(result["tasks"]["b"]["status"], "pending")
        self.assertEqual(result["tasks"]["b"]["error"], "REGISTRY_OCCUPANCY_UNKNOWN")

    def test_non_mapping_slot_is_corrupt_not_unhandled(self):
        self.init(self.spec([task("a", self.paths[0])]))
        path = Path(team.team_path("squad", self.env))
        record = json.loads(path.read_text())
        record["slots"]["a"] = ["invalid"]
        path.write_text(json.dumps(record))
        self.assertEqual(team.list_teams(env=self.env)[0]["status"], "CORRUPT")
        with self.assertRaises(team.TeamError):
            team.status("squad", env=self.env)

    def test_provider_unknown_error_code_is_preserved(self):
        self.init(self.spec([task("a", self.paths[0])]))
        result = self.tick(lambda **kwargs: {
            "status": "unknown", "error": {"code": "TEAM_READ_PANE_NOT_CLOSED"}})
        self.assertEqual(result["tasks"]["a"]["status"], "unknown")
        self.assertEqual(result["tasks"]["a"]["error"], "TEAM_READ_PANE_NOT_CLOSED")

    def test_unexpected_correction_helper_exception_drains_running_siblings(self):
        self.init(self.spec([task("a", self.paths[0], reviewer="reviewer",
                                  corrections=1),
                             task("b", self.paths[1], mode="read")]))
        path = Path(team.team_path("squad", self.env))
        record = json.loads(path.read_text())
        record["slots"]["a"].update(
            phase="correction", round=1, status="running", head=A,
            execution_id=team._execution_id("squad", "a", "correction", 1),
            prompt="bounded correction", feedback="review findings")
        record["slots"]["b"].update(
            phase="work", round=0, status="running", head=None,
            execution_id=team._execution_id("squad", "b", "work", 0),
            prompt="read source")
        path.write_text(json.dumps(record))
        completed = []
        def fake(**kwargs):
            completed.append(kwargs["task"]["id"])
            return self.done()
        with mock.patch("cao.team._prepare_verified_correction",
                        side_effect=KeyError("untrusted malformed finding")), \
             mock.patch("cao.team._execute", side_effect=fake):
            result = team.tick(
                "squad", config=self.config, driver=None, env=self.env,
                writer_snapshot=lambda _: (0, set()), registry_db="scratch")
        self.assertEqual(completed, ["b"])
        self.assertEqual(result["tasks"]["a"]["status"], "unknown")
        self.assertEqual(result["tasks"]["a"]["error"], "KeyError")
        self.assertEqual(result["tasks"]["b"]["status"], "complete")

    def test_one_corrupt_team_never_stops_unrelated_teams(self):
        self.init(self.spec([task("a", self.paths[0])], team_id="bad"))
        self.init(self.spec([task("b", self.paths[1])], team_id="good"))
        Path(team.team_path("bad", self.env)).write_text("{invalid")
        entries = team.list_teams(env=self.env)
        self.assertEqual({x["team_id"]: x["status"] for x in entries},
                         {"bad": "CORRUPT", "good": "PENDING"})
        with mock.patch("cao.team.tick", return_value=team.status("good", env=self.env)) as tick:
            result = team.tick_all(config=self.config, driver=None, env=self.env)
        tick.assert_called_once()
        self.assertEqual({x["team_id"]: x["status"] for x in result},
                         {"bad": "CORRUPT", "good": "PENDING"})

    def test_running_checkpoint_identity_is_validated(self):
        self.init(self.spec([task("a", self.paths[0])]))
        p = Path(team.team_path("squad", self.env))
        data = json.loads(p.read_text())
        slot = data["slots"]["a"]
        slot.update(status="running", execution_id="colliding-old-id", prompt="review")
        p.write_text(json.dumps(data))
        with self.assertRaises(team.TeamError):
            team.status("squad", env=self.env)

    def test_role_model_option_injection_is_rejected(self):
        for model in ("-x", "--foo", "noslash", "a b", "x\x00y", "vendor/../other"):
            with self.subTest(model=model), self.assertRaises(team.TeamError):
                team.validate_spec(self.spec(
                    [task("a", self.paths[0])]) | {
                    "roles": {**self.roles,
                              "developer": {"instruction": "Develop", "model": model}},
                })
        spec = self.spec([task("a", self.paths[0])])
        spec["roles"]["developer"]["model"] = "opencode/step-5-preview-free#xhigh"
        self.assertEqual(team.validate_spec(spec)["roles"]["developer"]["model"],
                         "opencode/step-5-preview-free#xhigh")

    def test_resume_after_checkpoint_before_claim_respects_external_cap(self):
        self.init(self.spec([task("a", self.paths[0])]))
        p = Path(team.team_path("squad", self.env))
        data = json.loads(p.read_text())
        data["slots"]["a"].update(
            status="running", execution_id=team._execution_id("squad", "a", "work", 0),
            prompt="Previously checkpointed exact user task")
        p.write_text(json.dumps(data))
        # No product_task record exists yet. Even though the slot is RUNNING,
        # cap admission must be checked before first real task effect.
        result = team.tick("squad", config=self.config, driver=None,
                           env=self.env, writer_snapshot=lambda _: (2, set()),
                           registry_db="unused")
        self.assertEqual(result["tasks"]["a"]["status"], "running")
        self.assertFalse(Path(product_state.task_record_path(
            data["slots"]["a"]["execution_id"], self.env)).exists())

    def test_real_git_correction_uses_exact_verified_registry_history(self):
        repo = git_repo(self.root, name="correctable", files=("a.txt",))
        bare = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                       capture_output=True, env={**os.environ, **GIT_ENV})

        def git(*arguments):
            return subprocess.run(
                ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *arguments],
                check=True, capture_output=True, text=True,
                env={**os.environ, **GIT_ENV}).stdout.strip()

        git("remote", "add", "origin", str(bare))
        git("push", "-u", "origin", "main")
        db = str(self.root / "correction-ledger.sqlite3")
        registry.connect(db).close()

        class Runtime(FakeDriver):
            def workspace_create(self, label, cwd=None):
                ident = "w" + str(len(self.workspaces) + 1)
                self.workspaces.append({
                    "workspace_id": ident, "label": label,
                    "active_tab_id": ident + ":t1",
                })
                return {"workspace_id": ident, "tab_id": ident + ":t1",
                        "pane_id": ident + ":p1"}

            def pane_list(self, workspace_id):
                pane = workspace_id + ":p1"
                return [] if pane in self.closed_panes else [
                    {"pane_id": pane, "workspace_id": workspace_id,
                     "agent_status": "done"}]

            def pane_get(self, pane_id):
                if pane_id in self.closed_panes:
                    from cao import product_driver
                    raise product_driver.DriverNotFound("closed")
                return {"pane_id": pane_id, "workspace_id": pane_id.split(":")[0]}

            def agent_prompt(self, target, prompt, *, timeout_ms=None, wait=False):
                if "Role: Implement precise edits" in prompt:
                    correction = "Address the bounded review findings" in prompt
                    version = git("rev-list", "--count", "origin/main..HEAD")
                    (repo / "a.txt").write_text(
                        ("fixed" + version + "\n") if correction else "first\n"
                    )
                    git("add", "--", "a.txt")
                    git("commit", "-m", "correct" if correction else "initial")
                    self.read_text = "committed"
                elif "Role: Independently review commits" in prompt:
                    verdict = ("CHANGES_REQUESTED" if
                               git("rev-list", "--count", "origin/main..HEAD") in ("1", "2")
                               else "APPROVED")
                    self.read_text = (
                        "CAO_REVIEW_VERDICT: " + verdict
                        + "\nCAO_REVIEWED_HEAD: " + git("rev-parse", "HEAD")
                    )
                # Derive the requested receipt from the *prompt challenge*,
                # never paste an already completed receipt into the prompt.
                import re
                shown = re.search(r"CAO_TEAM_NONCE_V2: ([0-9a-f]{16})", prompt)
                assert shown is not None
                self.read_text += "\nCAO_TEAM_RESULT: " + shown.group(1)
                return super().agent_prompt(
                    target, prompt, timeout_ms=timeout_ms, wait=wait)

        self.init(self.spec([
            {**task("a", str(repo), reviewer="reviewer", corrections=2),
             "write_scope": ["a.txt"]}
        ]))
        driver = Runtime()
        states = []
        for index in range(6):
            if index == 2:
                record = team._required("squad", self.env)
                slot = record["slots"]["a"]
                declared = record["spec"]["tasks"][0]
                def verify():
                    return team._prepare_verified_correction(
                        record["spec"], declared, slot,
                        driver=driver, env=self.env, db=db)
                (repo / "unexpected.txt").write_text("untracked")
                with self.assertRaisesRegex(team.TeamError, "dirty state"):
                    verify()
                (repo / "unexpected.txt").unlink()
                old_id = team._execution_id("squad", "a", "work", 0)
                prior = product_state.read_task(old_id, self.env)
                forged = {**prior, "claim": {
                    **prior["claim"], "released_head": B,
                }}
                with mock.patch("cao.product_state.read_task", return_value=forged):
                    with self.assertRaisesRegex(team.TeamError, "verified task"):
                        verify()
                pane_missing = {**prior, "pane_closed": None, "checkpoint": {}}
                with mock.patch("cao.product_state.read_task", return_value=pane_missing):
                    with self.assertRaisesRegex(team.TeamError, "verified task"):
                        verify()
                forged_baseline = {**prior, "claim": {
                    **prior["claim"], "base_head": B,
                }}
                with mock.patch("cao.product_state.read_task", return_value=forged_baseline):
                    with self.assertRaisesRegex(team.TeamError, "predates"):
                        verify()
                from cao import product_driver
                with mock.patch.object(driver, "workspace_list",
                                       side_effect=product_driver.DriverTransportError(
                                           "simulated runtime unreadable")):
                    with self.assertRaisesRegex(team.TeamError, "evidence unavailable"):
                        verify()
                c = registry.connect_readonly(db)
                try:
                    self.assertEqual(c.execute(
                        "SELECT COUNT(*) FROM dispositions").fetchone()[0], 0)
                finally:
                    c.close()
            value = team.tick("squad", config=self.config, driver=driver,
                              env=self.env, registry_db=db)
            states.append((value["tasks"]["a"]["phase"],
                           value["tasks"]["a"]["status"],
                           value["tasks"]["a"]["error"]))
        self.assertEqual(states[0][:2], ("review", "pending"), states)
        self.assertEqual(states[1][:2], ("correction", "pending"), states)
        self.assertEqual(states[2][:2], ("review", "pending"), states)
        self.assertEqual(states[3][:2], ("correction", "pending"), states)
        self.assertEqual(states[4][:2], ("review", "pending"), states)
        self.assertEqual(states[5][:2], ("review", "complete"), states)
        self.assertEqual(git("rev-list", "--count", "origin/main..HEAD"), "3")
        c = registry.connect_readonly(db)
        try:
            claims = c.execute(
                "SELECT mode,outcome,released_at FROM claims ORDER BY claim_id").fetchall()
            decisions = c.execute(
                "SELECT disposition,authority FROM dispositions").fetchall()
        finally:
            c.close()
        self.assertEqual([x["mode"] for x in claims],
                         ["WRITE", "READ", "WRITE", "READ", "WRITE", "READ"])
        self.assertTrue(all(x["outcome"] == "complete" and x["released_at"]
                            for x in claims))
        self.assertEqual([(x["disposition"], x["authority"]) for x in decisions],
                         [("historical", "cao-automated-team:squad")] * 2)

    def test_role_without_model_preserves_explicit_product_default(self):
        role = {"instruction": "Read only"}
        self.roles = {"analyst": role}
        self.init(self.spec([task("a", self.paths[0], mode="read")]))
        def fake(**kwargs):
            args = product_config.task_defaults(kwargs["config"]).agent_args
            self.assertEqual(args, ("mini", "--standalone", "--model", "opencode/default"))
            return self.done()
        self.tick(fake)
        self.assertEqual(team.status("squad", env=self.env)["status"], "COMPLETE")

    def test_correction_lineage_ignores_manifest_write_scope_order(self):
        """A manifest may list the exact same paths in a different order.

        The product task record and the Registry row both store the canonically
        sorted form, so lineage verification must compare the normalized list:
        ordering alone can never break an exact match, while a genuinely
        different scope still fails.
        """
        repo = git_repo(self.root, name="reordered", files=("a.txt",))
        bare = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                       capture_output=True, env={**os.environ, **GIT_ENV})

        def git(*arguments):
            return subprocess.run(
                ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *arguments],
                check=True, capture_output=True, text=True,
                env={**os.environ, **GIT_ENV}).stdout.strip()

        git("remote", "add", "origin", str(bare))
        git("push", "-u", "origin", "main")
        db = str(self.root / "reordered-ledger.sqlite3")
        registry.connect(db).close()

        class Runtime(FakeDriver):
            def workspace_create(self, label, cwd=None):
                ident = "w" + str(len(self.workspaces) + 1)
                self.workspaces.append({
                    "workspace_id": ident, "label": label,
                    "active_tab_id": ident + ":t1"})
                return {"workspace_id": ident, "tab_id": ident + ":t1",
                        "pane_id": ident + ":p1"}

            def pane_list(self, workspace_id):
                pane = workspace_id + ":p1"
                return [] if pane in self.closed_panes else [
                    {"pane_id": pane, "workspace_id": workspace_id,
                     "agent_status": "done"}]

            def pane_get(self, pane_id):
                if pane_id in self.closed_panes:
                    from cao import product_driver
                    raise product_driver.DriverNotFound("closed")
                return {"pane_id": pane_id, "workspace_id": pane_id.split(":")[0]}

            def agent_prompt(self, target, prompt, *, timeout_ms=None, wait=False):
                if "Role: Implement precise edits" in prompt:
                    (repo / "a.txt").write_text("first\n")
                    (repo / "b.txt").write_text("staged\n")
                    git("add", "--", "a.txt", "b.txt")
                    git("commit", "-m", "initial")
                    self.read_text = "committed"
                import re
                shown = re.search(r"CAO_TEAM_NONCE_V2: ([0-9a-f]{16})", prompt)
                assert shown is not None
                self.read_text += "\nCAO_TEAM_RESULT: " + shown.group(1)
                return super().agent_prompt(
                    target, prompt, timeout_ms=timeout_ms, wait=wait)

        driver = Runtime()
        # The manifest deliberately lists the reverse of the canonical order.
        self.init(self.spec([
            {**task("a", str(repo), reviewer="reviewer", corrections=1),
             "write_scope": ["b.txt", "a.txt"]}
        ]))
        team.tick("squad", config=self.config, driver=driver, env=self.env,
                  registry_db=db)
        self.assertEqual(team.status("squad", env=self.env)["tasks"]["a"]["phase"],
                         "review")
        path = Path(team.team_path("squad", self.env))
        record = json.loads(path.read_text())
        record["slots"]["a"].update(phase="correction", round=1, status="pending")
        path.write_text(json.dumps(record))
        record = team._required("squad", self.env)
        declared = record["spec"]["tasks"][0]
        slot = record["slots"]["a"]
        team._prepare_verified_correction(
            record["spec"], declared, slot, driver=driver, env=self.env, db=db)
        c = registry.connect_readonly(db)
        try:
            self.assertEqual(
                [(x["disposition"], x["authority"]) for x in c.execute(
                    "SELECT disposition,authority FROM dispositions")],
                [("historical", "cao-automated-team:squad")])
        finally:
            c.close()
        # Content still matters: a claimed scope that no longer matches the
        # exact Registry row fails instead of being normalized into agreement.
        old_id = team._execution_id("squad", "a", "work", 0)
        conn = registry.connect(db)
        try:
            conn.execute(
                "UPDATE works SET paths=? WHERE run_id=? AND work_key=?",
                ("a.txt\nb.txt\nextra.txt", "cao-team-squad", old_id))
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(team.TeamError, "Registry row"):
            team._prepare_verified_correction(
                record["spec"], declared, slot, driver=driver, env=self.env, db=db)
        forged = dict(product_state.read_task(old_id, self.env), scope=["a.txt"])
        with mock.patch("cao.product_state.read_task", return_value=forged):
            with self.assertRaisesRegex(team.TeamError, "verified task"):
                team._prepare_verified_correction(
                    record["spec"], declared, slot, driver=driver, env=self.env, db=db)
        c = registry.connect_readonly(db)
        try:
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM dispositions").fetchone()[0], 1)
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
