"""Hermetic Team Execution contract and lifecycle tests."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cao import (product_config, product_daemon, product_state, product_task,
                 registry, team, team_recovery)
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

    def review_payload(self, output, **bounds):
        """A bounded review capture exactly as product_task stores one."""
        captured = len(output)
        return {
            "status": "complete", "claim": {"released": True, "released_head": A},
            "output": output, "output_truncated": True,
            "output_bounds": {"limit": captured, "captured_chars": captured,
                              **bounds},
        }

    def verdict_review(self, output, **bounds):
        """Dispatch work once, then deliver one bounded review capture."""
        seen = []

        def fake(**kwargs):
            seen.append(kwargs["phase"])
            if kwargs["phase"] != "review":
                return self.done()
            payload = self.review_payload(output, **bounds)
            return payload

        self.tick(fake)
        return self.tick(fake, heads={self.paths[0]: A})

    def test_bounded_review_needs_a_sized_tail_that_kept_the_nonce(self):
        """Only a provably bounded tail may decide a review.

        The nonce is bound to this execution and never appears in the sent
        prompt, so a capture without it is not this task's verified answer.
        """
        self.init(self.spec([task("a", self.paths[0], reviewer="reviewer")]))
        receipt = "CAO_TEAM_RESULT: " + team._result_challenge(
            team._execution_id("squad", "a", "review", 0))
        verdict = "CAO_REVIEW_VERDICT: APPROVED\nCAO_REVIEWED_HEAD: " + A
        accepted = self.verdict_review(
            "long answer\n" + verdict + "\n" + receipt + "\n",
            kept="tail", dropped_chars=9000)
        self.assertEqual(accepted["status"], "COMPLETE")
        # Every negative case runs under its own team identity, so the receipt
        # is built from that attempt's actual execution nonce. Cases whose
        # capture is not a provable tail fail the bound check; cases with a
        # valid tail but a bad verdict reach, and are decided by, the
        # verdict/head check. Both predicate families are asserted explicitly.
        cases = (
            ("head only",
             lambda r: "long answer\n" + verdict + "\n" + r + "\n",
             {"kept": "head", "dropped_chars": 9000},
             "REVIEW_OUTPUT_BOUND_UNVERIFIED"),
            ("unknown provenance",
             lambda r: "long answer\n" + verdict + "\n" + r + "\n",
             {"kept": "tail", "dropped_chars": 0},
             "REVIEW_OUTPUT_BOUND_UNVERIFIED"),
            ("missing nonce",
             lambda r: "long answer\n" + verdict + "\n",
             {"kept": "tail", "dropped_chars": 9000},
             "REVIEW_OUTPUT_BOUND_UNVERIFIED"),
            ("forged nonce",
             lambda r: "long answer\n" + verdict + "\nCAO_TEAM_RESULT: " + "f" * 16 + "\n",
             {"kept": "tail", "dropped_chars": 9000},
             "REVIEW_OUTPUT_BOUND_UNVERIFIED"),
            ("unsized window",
             lambda r: "long answer\n" + verdict + "\n" + r + "\n",
             {"kept": "tail"},
             "REVIEW_OUTPUT_BOUND_UNVERIFIED"),
            ("claimed size disagrees",
             lambda r: "long answer\n" + verdict + "\n" + r + "\n",
             {"kept": "tail", "dropped_chars": 9000, "captured_chars": 99999},
             "REVIEW_OUTPUT_BOUND_UNVERIFIED"),
            ("no verdict",
             lambda r: "long answer\n" + r + "\n",
             {"kept": "tail", "dropped_chars": 9000},
             "REVIEW_VERDICT_UNVERIFIED"),
            ("wrong reviewed head",
             lambda r: "long answer\nCAO_REVIEW_VERDICT: APPROVED\n"
                       "CAO_REVIEWED_HEAD: " + B + "\n" + r + "\n",
             {"kept": "tail", "dropped_chars": 9000},
             "REVIEW_VERDICT_UNVERIFIED"),
            ("two verdicts",
             lambda r: "long answer\n" + verdict + "\n" + verdict + "\n" + r + "\n",
             {"kept": "tail", "dropped_chars": 9000},
             "REVIEW_VERDICT_UNVERIFIED"),
        )
        for index, (label, build, bounds, expected) in enumerate(cases):
            with self.subTest(label=label):
                ident = "b" + str(index)
                self.init(self.spec(
                    [task("a", self.paths[0], reviewer="reviewer")], team_id=ident))
                output = build("CAO_TEAM_RESULT: " + team._result_challenge(
                    team._execution_id(ident, "a", "review", 0)))

                def fake(**kwargs):
                    if kwargs["phase"] != "review":
                        return self.done()
                    return self.review_payload(output, **bounds)

                self.tick(fake, team_id=ident)
                held = self.tick(fake, team_id=ident, heads={self.paths[0]: A})
                self.assertEqual(held["tasks"]["a"]["status"], "unknown")
                self.assertEqual(held["tasks"]["a"]["error"], expected)

    def test_bounded_review_of_another_head_still_blocks_on_drift(self):
        self.init(self.spec([task("a", self.paths[0], reviewer="reviewer")]))
        receipt = "CAO_TEAM_RESULT: " + team._result_challenge(
            team._execution_id("squad", "a", "review", 0))
        output = ("long answer\nCAO_REVIEW_VERDICT: APPROVED\nCAO_REVIEWED_HEAD: "
                  + A + "\n" + receipt + "\n")

        def fake(**kwargs):
            if kwargs["phase"] != "review":
                return self.done()
            return self.review_payload(output, kept="tail", dropped_chars=9000)

        self.tick(fake)
        drifted = self.tick(fake, heads={self.paths[0]: B})
        self.assertEqual(drifted["tasks"]["a"]["status"], "blocked")
        self.assertEqual(drifted["tasks"]["a"]["error"], "REVIEW_HEAD_DRIFT")

    # --- retained durable observation windows ------------------------------
    #
    # A runtime pane is a scrolling window, so an earlier longer read can omit
    # the receipt that a shorter later read carries. product_task retains every
    # bounded observation with its prompt/terminal provenance; Team Execution
    # must accept the receipt from any admissible window while still refusing a
    # pre-prompt, unsized, echoed or cross-task text.

    def observation(self, output, *, kept="all", limit=None, captured=None,
                    dropped=0, prompt_ack="accepted", stage="prompted",
                    terminal=True, observed_at="2026-01-01T00:00:00Z"):
        """One observation entry exactly as product_task records it."""
        captured = len(output) if captured is None else captured
        return {
            "output": output,
            "bounds": {"kept": kept,
                       "limit": len(output) if limit is None else limit,
                       "captured_chars": captured,
                       "dropped_chars": dropped},
            "observed_at": observed_at,
            "prompt_ack": prompt_ack,
            "stage": stage,
            "terminal": terminal,
        }

    def durable_payload(self, *, output, observations=(), truncated=False,
                        bounds=None, released_head=A):
        return {
            "status": "complete",
            "claim": {"released": True, "released_head": released_head},
            "output": output,
            "output_truncated": truncated,
            "output_bounds": bounds,
            "output_observations": list(observations),
        }

    def read_prompt(self, execution_id):
        challenge = team._result_challenge(execution_id)
        return ("Analyze source\nCAO_TEAM_NONCE_V2: " + challenge,
                "CAO_TEAM_RESULT: " + challenge)

    def execute_read(self, payload, execution_id, prompt):
        record = {"checkpoint": {"agent_name": "reader",
                                 "workspace_id": "w9", "pane_id": "w9:p1"}}
        with mock.patch("cao.product_task.run_task", return_value=payload), \
                mock.patch("cao.product_state.read_task", return_value=record), \
                mock.patch("cao.product_task._close_owned_pane",
                           return_value=("w9:p1", None)), \
                mock.patch("cao.product_state.update_task"):
            return team._execute(
                execution_id=execution_id,
                task={"repo": self.paths[0], "mode": "read", "write_scope": []},
                prompt=prompt, phase="work", config=self.config,
                driver=FakeDriver(), env=self.env, wait_ms=1000,
                registry_db="unused")

    def test_durable_receipt_in_a_shorter_later_window_is_accepted(self):
        """Falsification 1: longer earlier receipt-free, shorter later valid."""
        ident = "ct-squad-reader-work-0"
        prompt, receipt = self.read_prompt(ident)
        long_read = "".join(f"scrollback line {i:04d}\n" for i in range(200))
        short_read = "final answer\n" + receipt + "\n"
        payload = self.durable_payload(
            output=long_read[-400:], truncated=True,
            bounds={"kept": "tail", "limit": 400, "captured_chars": 400,
                    "dropped_chars": 9000},
            observations=[
                self.observation(long_read[-400:], kept="tail", limit=400,
                                 captured=400, dropped=9000),
                self.observation(short_read),
            ])
        result = self.execute_read(payload, ident, prompt)
        self.assertEqual(result["status"], "complete")
        # The verified capture, not merely the longest one, is what is used.
        self.assertEqual(result["output"], short_read)

    def test_earlier_valid_receipt_is_not_lost_to_a_later_short_invalid(self):
        """Falsification 2: earlier valid then later short invalid."""
        ident = "ct-squad-reader-work-0"
        prompt, receipt = self.read_prompt(ident)
        valid = "done\n" + receipt + "\n"
        invalid = "still working, no proof yet\n"
        payload = self.durable_payload(
            output=invalid,
            observations=[self.observation(valid), self.observation(invalid)])
        result = self.execute_read(payload, ident, prompt)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["output"], valid)

    def test_wrong_nonce_is_never_a_durable_receipt(self):
        """Falsification 3: a changed nonce is refused in every window."""
        ident = "ct-squad-reader-work-0"
        prompt, _ = self.read_prompt(ident)
        forged = "done\nCAO_TEAM_RESULT: " + ("f" * 16) + "\n"
        payload = self.durable_payload(
            output="receipt-free scrollback",
            observations=[self.observation(forged)])
        result = self.execute_read(payload, ident, prompt)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"]["code"],
                         "TEAM_DURABLE_RESULT_UNVERIFIED")

    def test_prompt_echo_is_not_a_receipt(self):
        """Falsification 4: the prompt's own nonce line is not a result."""
        ident = "ct-squad-reader-work-0"
        prompt, _ = self.read_prompt(ident)
        echo = "echoed prompt\n" + prompt + "\n"
        payload = self.durable_payload(
            output="receipt-free scrollback",
            observations=[self.observation(echo)])
        result = self.execute_read(payload, ident, prompt)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"]["code"],
                         "TEAM_DURABLE_RESULT_UNVERIFIED")

    def test_provenance_unknown_window_is_never_terminal_proof(self):
        """Falsification 5: truncated output whose provenance is unknown."""
        ident = "ct-squad-reader-work-0"
        prompt, receipt = self.read_prompt(ident)
        text = "".join(f"line {i:04d}\n" for i in range(120)) + receipt + "\n"
        for label, broken in (
            ("no prompt provenance",
             self.observation(text, prompt_ack=None)),
            ("not a terminal state",
             self.observation(text, terminal=False)),
            ("no recorded bounds",
             {"output": text, "observed_at": "2026-01-01T00:00:00Z"}),
            ("claimed size lies",
             self.observation(text, captured=1, dropped=5)),
        ):
            with self.subTest(label=label):
                payload = self.durable_payload(
                    output="receipt-free scrollback",
                    observations=[broken])
                result = self.execute_read(payload, ident, prompt)
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["error"]["code"],
                                 "TEAM_DURABLE_RESULT_UNVERIFIED")

    def test_another_tasks_receipt_never_verifies_this_task(self):
        """Falsification 6a: a sibling execution's receipt is not ours."""
        ident = "ct-squad-reader-work-0"
        prompt, _ = self.read_prompt(ident)
        foreign = ("done\nCAO_TEAM_RESULT: "
                   + team._result_challenge("ct-squad-reader-work-0-sibling")
                   + "\n")
        payload = self.durable_payload(
            output="receipt-free scrollback",
            observations=[self.observation(foreign)])
        result = self.execute_read(payload, ident, prompt)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["error"]["code"],
                         "TEAM_DURABLE_RESULT_UNVERIFIED")

    def test_repeat_evaluation_reuses_the_exact_execution_identity(self):
        """Falsification 6b/7: evaluation is read-only and idempotent."""
        ident = "ct-squad-reader-work-0"
        prompt, receipt = self.read_prompt(ident)
        payload = self.durable_payload(
            output=("x" * 400), truncated=True,
            bounds={"kept": "tail", "limit": 400, "captured_chars": 400,
                    "dropped_chars": 9000},
            observations=[self.observation("done\n" + receipt + "\n")])
        record = {"checkpoint": {"agent_name": "reader",
                                 "workspace_id": "w9", "pane_id": "w9:p1"}}
        with mock.patch("cao.product_task.run_task",
                        return_value=payload) as runner, \
                mock.patch("cao.product_state.read_task", return_value=record), \
                mock.patch("cao.product_task._close_owned_pane",
                           return_value=("w9:p1", None)), \
                mock.patch("cao.product_state.update_task"):
            for _ in range(3):
                result = team._execute(
                    execution_id=ident,
                    task={"repo": self.paths[0], "mode": "read",
                          "write_scope": []},
                    prompt=prompt, phase="work", config=self.config,
                    driver=FakeDriver(), env=self.env, wait_ms=1000,
                    registry_db="unused")
                self.assertEqual(result["status"], "complete")
            # One run per evaluation, always the same task identity: no
            # re-prompt and never a replacement claim.
            self.assertEqual(runner.call_count, 3)
            self.assertEqual(runner.call_args.kwargs["task_id"], ident)

    def test_review_accepts_a_receipt_from_a_retained_shorter_window(self):
        self.init(self.spec([task("a", self.paths[0], reviewer="reviewer")]))
        ident = team._execution_id("squad", "a", "review", 0)
        challenge = team._result_challenge(ident)
        receipt = "CAO_TEAM_RESULT: " + challenge
        long_read = "".join(f"inspected line {i:04d}\n" for i in range(200))
        short_read = ("findings\nCAO_REVIEW_VERDICT: APPROVED\n"
                      "CAO_REVIEWED_HEAD: " + A + "\n" + receipt + "\n")

        def fake(**kwargs):
            if kwargs["phase"] != "review":
                return self.done()
            return self.durable_payload(
                output=long_read[-400:], truncated=True,
                bounds={"kept": "tail", "limit": 400, "captured_chars": 400,
                        "dropped_chars": 9000},
                observations=[
                    self.observation(long_read[-400:], kept="tail", limit=400,
                                     captured=400, dropped=9000),
                    self.observation(short_read),
                ])

        self.tick(fake)
        result = self.tick(fake, heads={self.paths[0]: A})
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["tasks"]["a"]["status"], "complete")

    def test_retained_window_receipt_still_blocks_on_head_drift(self):
        self.init(self.spec([task("a", self.paths[0], reviewer="reviewer")]))
        ident = team._execution_id("squad", "a", "review", 0)
        challenge = team._result_challenge(ident)
        receipt = "CAO_TEAM_RESULT: " + challenge
        short_read = ("findings\nCAO_REVIEW_VERDICT: APPROVED\n"
                      "CAO_REVIEWED_HEAD: " + A + "\n" + receipt + "\n")

        def fake(**kwargs):
            if kwargs["phase"] != "review":
                return self.done()
            return self.durable_payload(
                output="receipt-free scrollback",
                observations=[self.observation(short_read)])

        self.tick(fake)
        drifted = self.tick(fake, heads={self.paths[0]: B})
        self.assertEqual(drifted["tasks"]["a"]["status"], "blocked")
        self.assertEqual(drifted["tasks"]["a"]["error"], "REVIEW_HEAD_DRIFT")

    def test_completed_retained_receipt_issues_no_further_dispatch(self):
        self.init(self.spec([task("a", self.paths[0], mode="read")]))
        ident = team._execution_id("squad", "a", "work", 0)
        receipt = "CAO_TEAM_RESULT: " + team._result_challenge(ident)
        calls = []

        def fake(**kwargs):
            calls.append(kwargs["execution_id"])
            return self.durable_payload(
                output=("scrollback " * 60)[-400:], truncated=True,
                bounds={"kept": "tail", "limit": 400, "captured_chars": 400,
                        "dropped_chars": 9000},
                observations=[self.observation("done\n" + receipt + "\n")])

        self.tick(fake)
        self.assertEqual(team.status("squad", env=self.env)["status"],
                         "COMPLETE")
        dispatched = len(calls)
        self.tick(fake)
        self.assertEqual(len(calls), dispatched)

    def test_bounded_review_requests_correction_from_the_kept_suffix(self):
        self.init(self.spec([task("a", self.paths[0], reviewer="reviewer",
                                  corrections=1)]))
        receipt = "CAO_TEAM_RESULT: " + team._result_challenge(
            team._execution_id("squad", "a", "review", 0))
        output = ("findings\nCAO_REVIEW_VERDICT: CHANGES_REQUESTED\n"
                  "CAO_REVIEWED_HEAD: " + A + "\nFix the retry path\n"
                  + receipt + "\n")
        captured = []

        def fake(**kwargs):
            if kwargs["phase"] != "review":
                return self.done()
            captured.append(kwargs["execution_id"])
            return self.review_payload(output, kept="tail", dropped_chars=9000)

        self.tick(fake)
        result = self.tick(fake, heads={self.paths[0]: A})
        self.assertEqual(result["tasks"]["a"]["phase"], "correction")
        self.assertIn("Fix the retry path",
                      team._required("squad", self.env)["slots"]["a"]["feedback"])

    def test_clipped_review_feedback_keeps_the_findings_end(self):
        """A clipped tail's findings are at the end, not its first 5000 chars."""
        self.init(self.spec([task("a", self.paths[0], reviewer="reviewer",
                                  corrections=1)]))
        receipt = "CAO_TEAM_RESULT: " + team._result_challenge(
            team._execution_id("squad", "a", "review", 0))
        oldest = "OLDEST-CONTEXT-MARKER " + ("x" * 6000)
        output = (oldest + "\nCAO_REVIEW_VERDICT: CHANGES_REQUESTED\n"
                  "CAO_REVIEWED_HEAD: " + A + "\nNEWEST-FINDING fixes the retry path\n"
                  + receipt + "\n")

        def fake(**kwargs):
            if kwargs["phase"] != "review":
                return self.done()
            return self.review_payload(output, kept="tail", dropped_chars=6000)

        self.tick(fake)
        result = self.tick(fake, heads={self.paths[0]: A})
        self.assertEqual(result["tasks"]["a"]["phase"], "correction")
        feedback = team._required("squad", self.env)["slots"]["a"]["feedback"]
        self.assertIn("NEWEST-FINDING fixes the retry path", feedback)
        self.assertNotIn("OLDEST-CONTEXT-MARKER", feedback)

    def test_clipped_completed_output_hands_findings_end_to_dependents(self):
        """A dependent receives the newest end of a clipped predecessor read."""
        self.init(self.spec([
            task("a", self.paths[0], mode="read"),
            task("b", self.paths[1], mode="read", deps=["a"]),
        ]))
        oldest = "OLDEST-READ-MARKER " + ("y" * 6000)
        output = oldest + "\nFINAL-CONCLUSION for the dependent\n"
        calls = []

        def fake(**kwargs):
            calls.append((kwargs["task"]["id"], kwargs["prompt"]))
            if kwargs["task"]["id"] == "a":
                return {"status": "complete",
                        "claim": {"released": True, "released_head": A},
                        "output": output, "output_truncated": True,
                        "output_bounds": {"kept": "tail", "limit": len(output),
                                          "captured_chars": len(output),
                                          "dropped_chars": 6000}}
            return self.done()

        self.tick(fake)
        self.assertEqual(team.status("squad", env=self.env)["tasks"]["a"]["status"],
                         "complete")
        self.assertIn("FINAL-CONCLUSION for the dependent",
                      team._required("squad", self.env)["slots"]["a"]["output"])
        self.tick(fake)
        handoff = next(prompt for name, prompt in calls if name == "b")
        self.assertIn("FINAL-CONCLUSION for the dependent", handoff)
        self.assertNotIn("OLDEST-READ-MARKER", handoff)

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

    def test_team_local_two_writer_cap_across_ticks(self):
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
        # The next tick reconciles a and b but still holds c: this team's own
        # two running WRITE slots already fill max_writers, independent of the
        # (here stale) shared Registry count.
        self.tick(fake)
        self.assertEqual(len(started), 4)
        self.assertEqual(team.status("squad", env=self.env)["tasks"]["c"]["status"], "pending")

    def test_team_local_one_writer_cap_holds_second_across_ticks(self):
        spec = self.spec([task("a", self.paths[0]), task("b", self.paths[1])],
                         max_writers=1)
        self.init(spec)
        started = []
        def fake(**kwargs):
            started.append(kwargs["task"]["id"])
            return {"status": "running"}
        self.tick(fake, writers=3)
        self.assertEqual(started, ["a"])
        self.tick(fake, writers=3)
        self.assertEqual(started, ["a", "a"])
        self.assertEqual(team.status("squad", env=self.env)["tasks"]["b"]["status"], "pending")

    def test_foreign_writes_do_not_consume_team_writer_cap(self):
        # max_writers bounds THIS team's simultaneous WRITE slots only. Two
        # unrelated worktrees held by other claimants are foreign occupancy:
        # they must not block this team's own writer cap of one.
        spec = self.spec([task("a", self.paths[0]), task("read", self.paths[1], mode="read")],
                         max_writers=1)
        self.init(spec)
        started = []
        def fake(**kwargs):
            started.append(kwargs["task"]["id"])
            return self.done()
        self.tick(fake, writers=2)
        self.assertEqual(started, ["a", "read"])
        self.assertEqual(team.status("squad", env=self.env)["status"], "COMPLETE")

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

    def review_retry_refusal(self, execution_id):
        """The verified stopped + abandon-released READ evidence a retry needs."""
        return {
            "task_id": execution_id, "run_id": "cao-team-squad",
            "work_key": execution_id, "repo": self.paths[0], "mode": "READ",
            "scope": [], "status": "stopped",
            "checkpoint": {"pane_id": "w1:p1"}, "pane_closed": "w1:p1",
            "claim": {"claim_id": 42, "status": "RELEASED", "released": True,
                      "outcome": "abandon"},
        }

    def released_read_row(self):
        return {"run_id": "cao-team-squad", "work_key": "any-review",
                "owner": "cao-product", "mode": "READ",
                "released_at": "2026-01-01T01:00:00Z", "outcome": "abandon"}

    def retry_review_once(self, task_id, execution_id, backing=None):
        row = self.released_read_row()
        row["work_key"] = execution_id
        database = mock.Mock()
        database.execute.return_value.fetchone.return_value = row
        with mock.patch("cao.product_state.read_task",
                        return_value=backing or self.review_retry_refusal(
                            execution_id)), \
                mock.patch("cao.registry.connect_readonly", return_value=database):
            return team.retry_read("squad", task_id, env=self.env)

    def test_review_phase_read_receipt_failure_allows_one_explicit_retry(self):
        self.init(self.spec([task("a", self.paths[0], reviewer="reviewer")]))
        prompts = []

        def fake(**kwargs):
            prompts.append(kwargs)
            if kwargs["phase"] == "review":
                return {"status": "unknown",
                        "error": {"code": "TEAM_RESULT_UNVERIFIED"}}
            return self.done()

        self.tick(fake)
        failed = self.tick(fake)
        self.assertEqual(failed["tasks"]["a"]["phase"], "review")
        self.assertEqual(failed["tasks"]["a"]["status"], "unknown")
        self.assertEqual(failed["tasks"]["a"]["error"], "TEAM_RESULT_UNVERIFIED")
        old_id = failed["tasks"]["a"]["execution_id"]
        self.assertEqual(old_id, team._execution_id("squad", "a", "review", 0))
        team.set_desired("squad", "paused", env=self.env)
        retried = self.retry_review_once("a", old_id)
        self.assertEqual(retried["tasks"]["a"]["status"], "pending")
        self.assertEqual(retried["tasks"]["a"]["retry_no"], 1)
        # The same accepted source revision, handoff and findings are retained.
        record = team._required("squad", self.env)["slots"]["a"]
        self.assertEqual(record["phase"], "review")
        self.assertEqual(record["head"], A)
        self.assertEqual(record["output"], "completed")
        self.assertIsNone(record["execution_id"])
        # One retry only, and the historical receipt stays durable.
        with self.assertRaises(team.TeamError):
            self.retry_review_once("a", old_id)
        dispatched = []
        team.set_desired("squad", "running", env=self.env)
        self.tick(lambda **kw: (dispatched.append(kw) or {"status": "running"}))
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["phase"], "review")
        self.assertEqual(dispatched[0]["execution_id"],
                         team._execution_id("squad", "a", "review", 0, 1))
        self.assertNotEqual(dispatched[0]["execution_id"], old_id)
        self.assertIn("CAO_TEAM_NONCE_V2:", dispatched[0]["prompt"])
        # The new attempt's nonce differs and its exact receipt line is absent
        # from the prompt, so a prompt echo can never impersonate success.
        new_id = dispatched[0]["execution_id"]
        self.assertNotEqual(team._result_challenge(new_id),
                            team._result_challenge(old_id))
        self.assertNotIn("CAO_TEAM_RESULT: " + team._result_challenge(new_id),
                         dispatched[0]["prompt"])
        self.assertIn("Review the actual repository against Git HEAD " + A,
                      dispatched[0]["prompt"])
        self.assertEqual(team.status("squad", env=self.env)["tasks"]["a"]["head"], A)

    def test_review_phase_retry_after_a_correction_reviews_the_same_accepted_head(self):
        self.init(self.spec([task("a", self.paths[0], reviewer="reviewer",
                                  corrections=1)]))
        reviews = []

        def fake(**kwargs):
            if kwargs["phase"] == "work":
                return self.done()
            if kwargs["phase"] == "correction":
                return {"status": "complete",
                        "claim": {"released": True, "released_head": B}}
            if not reviews:
                reviews.append(kwargs["execution_id"])
                return self.done(output="CAO_REVIEW_VERDICT: CHANGES_REQUESTED\n"
                                       "CAO_REVIEWED_HEAD: " + A + "\nfix X")
            if len(reviews) == 1:
                reviews.append(kwargs["execution_id"])
                return {"status": "unknown",
                        "error": {"code": "TEAM_DURABLE_RESULT_UNVERIFIED"}}
            return self.done(output="CAO_REVIEW_VERDICT: APPROVED\n"
                                   "CAO_REVIEWED_HEAD: " + B)

        self.tick(fake, heads={self.paths[0]: A})
        self.tick(fake, heads={self.paths[0]: A})
        self.tick(fake, heads={self.paths[0]: B})
        failed = self.tick(fake, heads={self.paths[0]: B})
        self.assertEqual(failed["tasks"]["a"]["phase"], "review")
        self.assertEqual(failed["tasks"]["a"]["round"], 1)
        self.assertEqual(failed["tasks"]["a"]["error"], "TEAM_DURABLE_RESULT_UNVERIFIED")
        old_id = failed["tasks"]["a"]["execution_id"]
        self.assertEqual(old_id, team._execution_id("squad", "a", "review", 1))
        team.set_desired("squad", "paused", env=self.env)
        retried = self.retry_review_once("a", old_id)
        self.assertEqual(retried["tasks"]["a"]["retry_no"], 1)
        self.assertEqual(retried["tasks"]["a"]["head"], B)
        self.assertEqual(retried["tasks"]["a"]["round"], 1)
        dispatched = []
        team.set_desired("squad", "running", env=self.env)
        self.tick(lambda **kw: (dispatched.append(kw) or {"status": "running"}),
                  heads={self.paths[0]: B})
        self.assertEqual(dispatched[0]["execution_id"],
                         team._execution_id("squad", "a", "review", 1, 1))
        self.assertNotEqual(dispatched[0]["execution_id"], old_id)
        # The retried review still names the same accepted HEAD it always did.
        self.assertIn("Review the actual repository against Git HEAD " + B,
                      dispatched[0]["prompt"])

    def test_review_retry_change_request_starts_a_valid_correction_round(self):
        """A retried review that still requests changes must not corrupt state.

        The retry carries retry_no=1; the CHANGES_REQUESTED transition opens a
        new correction round, which is a fresh identity with its own budget, so
        retry_no must reset to 0 or the stored checkpoint fails its own
        validation and ``cao team status`` raises.
        """
        self.init(self.spec([task("a", self.paths[0], reviewer="reviewer",
                                  corrections=1)]))
        review_retry_id = team._execution_id("squad", "a", "review", 0, 1)
        corrections = []

        def fake(**kwargs):
            if kwargs["phase"] == "work":
                return self.done()
            if kwargs["phase"] == "correction":
                corrections.append(kwargs["execution_id"])
                return {"status": "complete",
                        "claim": {"released": True, "released_head": B},
                        "output": "bounded correction committed"}
            if kwargs["execution_id"] == review_retry_id:
                return self.done(output="CAO_REVIEW_VERDICT: CHANGES_REQUESTED\n"
                                       "CAO_REVIEWED_HEAD: " + A + "\nFix the retry path")
            if kwargs["execution_id"] == team._execution_id("squad", "a", "review", 0):
                return {"status": "unknown",
                        "error": {"code": "TEAM_RESULT_UNVERIFIED"}}
            return self.done(output="CAO_REVIEW_VERDICT: APPROVED\n"
                                   "CAO_REVIEWED_HEAD: " + B)

        self.tick(fake, heads={self.paths[0]: A})
        self.tick(fake, heads={self.paths[0]: A})
        old_id = team._execution_id("squad", "a", "review", 0)
        team.set_desired("squad", "paused", env=self.env)
        retried = self.retry_review_once("a", old_id)
        self.assertEqual(retried["tasks"]["a"]["retry_no"], 1)
        team.set_desired("squad", "running", env=self.env)
        transitioned = self.tick(fake, heads={self.paths[0]: A})
        opened = transitioned["tasks"]["a"]
        self.assertEqual(opened["phase"], "correction")
        self.assertEqual(opened["round"], 1)
        self.assertEqual(opened["retry_no"], 0)
        self.assertEqual(opened["status"], "pending")
        # The checkpoint revalidates, so `cao team status` stays well-formed.
        reloaded = team.status("squad", env=self.env)["tasks"]["a"]
        self.assertEqual((reloaded["phase"], reloaded["retry_no"]), ("correction", 0))
        stored = team._required("squad", self.env)["slots"]["a"]
        self.assertIn("Fix the retry path", stored["feedback"])
        self.assertEqual(stored["head"], A)
        # A correction round is never a retryable READ; no WRITE retry exists.
        team.set_desired("squad", "paused", env=self.env)
        with self.assertRaises(team.TeamError):
            team.retry_read("squad", "a", env=self.env)
        team.set_desired("squad", "running", env=self.env)
        # The correction dispatches normally under its own identity.
        after = self.tick(fake, heads={self.paths[0]: A})
        self.assertEqual(after["tasks"]["a"]["phase"], "review")
        self.assertEqual(corrections,
                         [team._execution_id("squad", "a", "correction", 1)])
        self.tick(fake, heads={self.paths[0]: B})
        self.assertEqual(team.status("squad", env=self.env)["status"], "COMPLETE")

    def test_review_phase_retry_refuses_a_write_attempt(self):
        self.init(self.spec([task("a", self.paths[0], reviewer="reviewer")]))

        def fail_review(**kwargs):
            if kwargs["phase"] == "review":
                return {"status": "unknown",
                        "error": {"code": "TEAM_RESULT_UNVERIFIED"}}
            return {"status": "unknown", "error": {"code": "PROVIDER_DOWN"}}

        # The WRITE work attempt is never retryable, in any state.
        self.tick(fail_review)
        self.tick(fail_review)
        team.set_desired("squad", "paused", env=self.env)
        with self.assertRaises(team.TeamError):
            team.retry_read("squad", "a", env=self.env)
        # A WRITE task record can never stand in for the READ review attempt.
        work_id = team._execution_id("squad", "a", "work", 0)
        write_backing = self.review_retry_refusal(work_id)
        write_backing["mode"] = "WRITE"
        with self.assertRaises(team.TeamError):
            self.retry_review_once("a", work_id, backing=write_backing)

    def test_review_phase_retry_refuses_unverified_stop_or_wrong_ledger(self):
        self.init(self.spec([task("a", self.paths[0], reviewer="reviewer")]))

        def fake(**kwargs):
            if kwargs["phase"] == "review":
                return {"status": "unknown",
                        "error": {"code": "TEAM_RESULT_UNVERIFIED"}}
            return self.done()

        self.tick(fake)
        failed = self.tick(fake)
        old_id = failed["tasks"]["a"]["execution_id"]
        team.set_desired("squad", "paused", env=self.env)
        # Not stopped, or the CAO pane never handed back.
        for stale in ({**self.review_retry_refusal(old_id), "status": "running"},
                      {**self.review_retry_refusal(old_id), "pane_closed": None},
                      {**self.review_retry_refusal(old_id),
                       "claim": {"claim_id": 42, "status": "RESUMED",
                                 "released": False}}):
            with mock.patch("cao.product_state.read_task", return_value=stale):
                with self.assertRaises(team.TeamError):
                    team.retry_read("squad", "a", env=self.env)
        # A ledger row that is not this team's released READ claim.
        for wrong in ({"owner": "another-owner"}, {"mode": "WRITE"},
                      {"outcome": "complete"}, {"released_at": None},
                      {"work_key": "another-work"}):
            row = dict(self.released_read_row())
            row.update(wrong)
            row["work_key"] = wrong.get("work_key", old_id)
            database = mock.Mock()
            database.execute.return_value.fetchone.return_value = row
            with mock.patch("cao.product_state.read_task",
                            return_value=self.review_retry_refusal(old_id)), \
                    mock.patch("cao.registry.connect_readonly", return_value=database):
                with self.assertRaises(team.TeamError):
                    team.retry_read("squad", "a", env=self.env)
        self.assertEqual(team.status("squad", env=self.env)["tasks"]["a"]["status"],
                         "unknown")
        self.assertEqual(team.status("squad", env=self.env)["tasks"]["a"]["retry_no"], 0)

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

    def test_real_long_review_answer_keeps_verdict_and_nonce_and_completes(self):
        # The actual product_task path: a review answer longer than the task's
        # bounded capture. product_task keeps the terminal suffix and states
        # truthfully how much it dropped, so the exact verdict, reviewed HEAD
        # and this execution's nonce all survive and the review is decided.
        repo = git_repo(self.root, name="verbose-review", files=("a.txt",))
        bare = self.root / "verbose.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env={**os.environ, **GIT_ENV})

        def git(*arguments):
            return subprocess.run(
                ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *arguments],
                check=True, capture_output=True, text=True,
                env={**os.environ, **GIT_ENV}).stdout.strip()

        git("remote", "add", "origin", str(bare))
        git("push", "-u", "origin", "main")
        db = str(self.root / "verbose-registry.sqlite3")
        registry.connect(db).close()
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "id": "test", "model": "opencode/default"},
            "task": {"agent_kind": "opencode", "max_output_chars": 1200},
        })

        class VerboseHerdr(FakeDriver):
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
                pane = workspace_id + ":p1"
                return [] if pane in self.closed_panes else [{
                    "pane_id": pane, "workspace_id": workspace_id,
                    "agent_status": "done",
                }]

            def pane_get(self, pane_id):
                if pane_id in self.closed_panes:
                    from cao import product_driver
                    raise product_driver.DriverNotFound("closed")
                return {"pane_id": pane_id,
                        "workspace_id": pane_id.split(":")[0]}

            def agent_prompt(self, target, text, *, timeout_ms=None, wait=False):
                if "Role: Implement precise edits" in text:
                    (repo / "a.txt").write_text("changed\n")
                    git("add", "--", "a.txt")
                    git("commit", "-m", "team writer")
                    self.read_text = "Committed scoped change"
                else:
                    self.read_text = (
                        "".join(f"inspected src/module_{i:04d}.py line by line\n"
                                for i in range(120))
                        + "CAO_REVIEW_VERDICT: APPROVED\nCAO_REVIEWED_HEAD: "
                        + git("rev-parse", "HEAD"))
                challenge = team._result_challenge(
                    team._execution_id("squad", "feature",
                                       "review" if "Independently review" in text
                                       else "work", 0))
                self.read_text += "\nCAO_TEAM_RESULT: " + challenge
                return super().agent_prompt(
                    target, text, timeout_ms=timeout_ms, wait=wait)

        driver = VerboseHerdr()
        self.init(self.spec([
            {**task("feature", str(repo), reviewer="reviewer", corrections=1),
             "write_scope": ["a.txt"]}
        ]))
        first = team.tick("squad", config=config, driver=driver,
                          env=self.env, registry_db=db)
        self.assertEqual(first["tasks"]["feature"]["phase"], "review", first)
        review_id = team._execution_id("squad", "feature", "review", 0)
        second = team.tick("squad", config=config, driver=driver,
                           env=self.env, registry_db=db)
        self.assertEqual(second["status"], "COMPLETE")
        record = product_state.read_task(review_id, self.env)
        self.assertTrue(record["output_truncated"])
        self.assertEqual(record["output_bounds"]["kept"], "tail")
        self.assertGreater(record["output_bounds"]["dropped_chars"], 0)
        self.assertEqual(record["output_bounds"]["captured_chars"],
                         len(record["output"]))
        self.assertTrue(team._receipt_present(
            record["output"],
            "CAO_TEAM_RESULT: " + team._result_challenge(review_id)))
        self.assertEqual(team.status("squad", env=self.env)["tasks"]["feature"]["head"],
                         git("rev-parse", "HEAD"))

    def test_real_long_review_without_the_nonce_cannot_complete(self):
        # Same real path, but the captured tail does not carry this execution's
        # nonce, so the bounded answer is never a verified review.
        repo = git_repo(self.root, name="silent-review", files=("a.txt",))
        bare = self.root / "silent.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       env={**os.environ, **GIT_ENV})

        def git(*arguments):
            return subprocess.run(
                ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *arguments],
                check=True, capture_output=True, text=True,
                env={**os.environ, **GIT_ENV}).stdout.strip()

        git("remote", "add", "origin", str(bare))
        git("push", "-u", "origin", "main")
        db = str(self.root / "silent-registry.sqlite3")
        registry.connect(db).close()
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "id": "test", "model": "opencode/default"},
            "task": {"agent_kind": "opencode", "max_output_chars": 400},
        })

        class SilentHerdr(FakeDriver):
            def workspace_create(self, label, cwd=None):
                self._note("workspace_create", label, cwd)
                ident = "w" + str(len(self.workspaces) + 1)
                self.workspaces.append({
                    "workspace_id": ident, "label": label,
                    "active_tab_id": ident + ":t1"})
                return {"workspace_id": ident, "tab_id": ident + ":t1",
                        "pane_id": ident + ":p1"}

            def pane_list(self, workspace_id):
                pane = workspace_id + ":p1"
                return [] if pane in self.closed_panes else [{
                    "pane_id": pane, "workspace_id": workspace_id,
                    "agent_status": "done"}]

            def pane_get(self, pane_id):
                if pane_id in self.closed_panes:
                    from cao import product_driver
                    raise product_driver.DriverNotFound("closed")
                return {"pane_id": pane_id,
                        "workspace_id": pane_id.split(":")[0]}

            def agent_prompt(self, target, text, *, timeout_ms=None, wait=False):
                if "Role: Implement precise edits" in text:
                    (repo / "a.txt").write_text("changed\n")
                    git("add", "--", "a.txt")
                    git("commit", "-m", "team writer")
                    self.read_text = "Committed scoped change"
                else:
                    # The verdict and HEAD scroll out of the bounded window and
                    # the worker never emits its completion receipt.
                    self.read_text = (
                        "CAO_REVIEW_VERDICT: APPROVED\nCAO_REVIEWED_HEAD: "
                        + git("rev-parse", "HEAD") + "\n"
                        + "".join(f"more analysis {i:04d}\n" for i in range(60)))
                return super().agent_prompt(
                    target, text, timeout_ms=timeout_ms, wait=wait)

        driver = SilentHerdr()
        self.init(self.spec([
            {**task("feature", str(repo), reviewer="reviewer", corrections=1),
             "write_scope": ["a.txt"]}
        ]))
        team.tick("squad", config=config, driver=driver, env=self.env, registry_db=db)
        held = team.tick("squad", config=config, driver=driver,
                         env=self.env, registry_db=db)
        self.assertEqual(held["tasks"]["feature"]["status"], "unknown")
        self.assertIn(held["tasks"]["feature"]["error"],
                      ("TEAM_RESULT_UNVERIFIED", "REVIEW_OUTPUT_BOUND_UNVERIFIED",
                       "TEAM_DURABLE_RESULT_UNVERIFIED"))
        self.assertNotEqual(team.status("squad", env=self.env)["status"], "COMPLETE")
        c = registry.connect_readonly(db)
        try:
            rows = c.execute("SELECT released_at FROM claims WHERE mode='READ'").fetchall()
        finally:
            c.close()
        self.assertTrue(all(row["released_at"] for row in rows))

    START = "2026-01-01T00:00:00Z"

    def at(self, seconds):
        """A clock-controlled absolute moment, second resolution like CAO."""
        moment = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
        return lambda: moment

    def stalled_task_harness(self, *, present=False, output="partial work"):
        """A dispatched READ task whose claim exists and whose worker may be gone.

        Durable start and progress stamps are pinned so the liveness bound is
        measured against the supplied clock, never against wall time.
        """
        self.init(self.spec([task("a", self.paths[0], mode="read")], max_writers=0))
        team.tick("squad", config=self.config, driver=None, env=self.env,
                  execute=lambda **kw: {"status": "running"},
                  writer_snapshot=lambda _: (0, set()))
        execution_id = team.status("squad", env=self.env)["tasks"]["a"]["execution_id"]
        db = str(self.root / "stall-registry.sqlite3")
        registry.connect(db).close()
        conn = registry.connect(db)
        try:
            registry.propose(conn, "cao-team-squad", execution_id,
                             self.paths[0], components=["review"])
            registry.claim(conn, registry.ClaimSpec(
                run_id="cao-team-squad", work_key=execution_id,
                owner=product_task.OWNER, mode="READ", issue=""), None)
        finally:
            conn.close()
        product_state.write_task({
            "schema": product_state.TASK_SCHEMA, "task_id": execution_id,
            "run_id": "cao-team-squad", "work_key": execution_id,
            "repo": self.paths[0], "prompt": "Analyze source", "mode": "READ",
            "scope": [], "agent_kind": "opencode", "agent_args": [],
            "workspace_label": "cao-review-" + execution_id,
            "status": "running",
            "checkpoint": {"workspace_id": "w1", "pane_id": "w1:p1",
                           "agent_name": "cao-worker", "agent_started": True,
                           "prompt_issued": True, "prompt_ack": "accepted"},
            "created_at": self.START, "updated_at": self.START,
            "claim": {"claim_id": 1, "status": "CLAIMED", "mode": "READ"},
            "output": output, "output_truncated": False, "output_bounds": None,
            "observation": None, "error": None, "refusal": None,
        }, self.env)
        path = Path(team.team_path("squad", self.env))
        record = json.loads(path.read_text())
        record["slots"]["a"].update(started_at=self.START, progress_at=self.START,
                                    progress_key=team._progress_key(
                                        product_state.read_task(execution_id, self.env)))
        path.write_text(json.dumps(record))
        driver = FakeDriver()
        driver.agent_present = present
        return execution_id, db, driver

    def liveness_tick(self, db, driver, seconds, *, config=None):
        """One real-runtime tick that observes but never re-dispatches work."""
        with mock.patch.object(team, "_execute", return_value={"status": "running"}):
            return team.tick("squad", config=config or self.config, driver=driver,
                             env=self.env, registry_db=db, clock=self.at(seconds))

    def open_claims(self, db):
        conn = registry.connect_readonly(db)
        try:
            return conn.execute("SELECT claim_id, released_at FROM claims").fetchall()
        finally:
            conn.close()

    def test_long_authorized_computation_is_not_reported_as_a_stall(self):
        execution_id, db, driver = self.stalled_task_harness(present=True)
        # Far beyond the bound, but the runtime still reports the worker, so
        # this is a long authorized computation and not a stall.
        held = self.liveness_tick(db, driver, 10 ** 6)
        self.assertEqual(held["tasks"]["a"]["status"], "running")
        self.assertIsNone(held["tasks"]["a"]["error"])
        self.assertEqual(held["tasks"]["a"]["execution_id"], execution_id)
        self.assertEqual(held["tasks"]["a"]["started_at"], self.START)
        self.assertEqual(len(self.open_claims(db)), 1)

    def test_unreachable_worker_without_observable_progress_is_reported(self):
        execution_id, db, driver = self.stalled_task_harness(present=False)
        prompt = team._required("squad", self.env)["slots"]["a"]["prompt"]
        # Still inside the bound: nothing is reported and nothing is released.
        early = self.liveness_tick(db, driver, 600)
        self.assertEqual(early["tasks"]["a"]["status"], "running")
        self.assertIsNone(self.open_claims(db)[0]["released_at"])
        stalled = self.liveness_tick(db, driver, 7_200)
        self.assertEqual(stalled["tasks"]["a"]["status"], "unknown")
        self.assertEqual(stalled["tasks"]["a"]["error"], "TEAM_TASK_STALLED")
        # The exact existing execution identity, prompt and claim are retained:
        # never released, never replaced, never re-prompted.
        self.assertEqual(stalled["tasks"]["a"]["execution_id"], execution_id)
        record = team._required("squad", self.env)["slots"]["a"]
        self.assertEqual(record["prompt"], prompt)
        self.assertTrue(prompt)
        self.assertEqual(len(self.open_claims(db)), 1)
        self.assertIsNone(self.open_claims(db)[0]["released_at"])
        self.assertFalse(driver.names().count("agent_prompt"))
        self.assertFalse(driver.names().count("agent_start"))
        # It holds rather than churning: the same identity, never a new one.
        again = self.liveness_tick(db, driver, 8_000)
        self.assertEqual(again["tasks"]["a"]["status"], "unknown")
        self.assertEqual(again["tasks"]["a"]["execution_id"], execution_id)

    def test_observable_progress_resets_the_liveness_bound(self):
        execution_id, db, driver = self.stalled_task_harness(present=False)

        def stamp(seconds):
            return team.evidence.utc_now(self.at(seconds))

        first = self.liveness_tick(db, driver, 0)
        self.assertEqual(first["tasks"]["a"]["progress_at"], self.START)
        # A changed durable capture is observable progress.
        product_state.update_task(execution_id, {"output": "more evidence"}, self.env)
        second = self.liveness_tick(db, driver, 3_600)
        self.assertEqual(second["tasks"]["a"]["progress_at"], stamp(3_600))
        self.assertEqual(second["tasks"]["a"]["status"], "running")
        # Well past the bound measured from the dispatch, but inside the bound
        # measured from the last observable progress: not a stall.
        third = self.liveness_tick(db, driver, 9_000)
        self.assertEqual(third["tasks"]["a"]["status"], "running")
        self.assertEqual(third["tasks"]["a"]["progress_at"], stamp(3_600))
        product_state.update_task(execution_id, {"output": "final evidence"}, self.env)
        fourth = self.liveness_tick(db, driver, 10_000)
        self.assertEqual(fourth["tasks"]["a"]["progress_at"], stamp(10_000))
        self.assertEqual(fourth["tasks"]["a"]["status"], "running")
        # A tick never grants itself progress: the bound is always measured
        # from the last durable observation, so evidence discovered during the
        # same tick cannot quietly reset a bound that had already expired.
        fifth = self.liveness_tick(db, driver, 12 * 3_600)
        self.assertEqual(fifth["tasks"]["a"]["status"], "unknown")
        self.assertEqual(fifth["tasks"]["a"]["error"], "TEAM_TASK_STALLED")
        self.assertEqual(fifth["tasks"]["a"]["progress_at"], stamp(10_000))

    def test_configured_stall_bound_is_validated_and_honoured(self):
        self.assertEqual(product_config.team_stall_timeout_ms(None),
                         product_config.DEFAULT_TEAM_STALL_TIMEOUT_MS)
        configured = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "task": {"team_stall_timeout_ms": 90_000}})
        self.assertEqual(product_config.team_stall_timeout_ms(configured), 90_000)
        for bad in (0, -1, True, "60000", 10 ** 9):
            with self.subTest(bad=bad):
                broken = product_config.validate_config({
                    "schema": product_config.SCHEMA,
                    "task": {"team_stall_timeout_ms": bad}})
                with self.assertRaises(product_config.ProductConfigError):
                    product_config.team_stall_timeout_ms(broken)
        brief = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "id": "test", "model": "opencode/default"},
            "task": {"agent_kind": "opencode", "team_stall_timeout_ms": 60_000}})
        _, db, driver = self.stalled_task_harness(present=False)
        self.assertEqual(
            self.liveness_tick(db, driver, 30, config=brief)["tasks"]["a"]["status"],
            "running")
        self.assertEqual(
            self.liveness_tick(db, driver, 90, config=brief)["tasks"]["a"]["error"],
            "TEAM_TASK_STALLED")

    def test_missing_liveness_stamps_are_never_treated_as_a_stall(self):
        # A record checkpointed before this release carries no stamps at all.
        # It must never be reported stalled on the strength of a missing value.
        _, db, driver = self.stalled_task_harness(present=False)
        path = Path(team.team_path("squad", self.env))
        record = json.loads(path.read_text())
        for field in ("started_at", "progress_at", "progress_key"):
            record["slots"]["a"].pop(field, None)
        path.write_text(json.dumps(record))
        # A pre-release record still validates and reports no liveness evidence.
        self.assertEqual(team.status("squad", env=self.env)["tasks"]["a"]["started_at"],
                         None)
        held = self.liveness_tick(db, driver, 10 ** 7)
        self.assertEqual(held["tasks"]["a"]["status"], "running")
        self.assertIsNone(held["tasks"]["a"]["error"])
        # A terminal task record can never be reported as stalled at all.
        product_state.update_task(record["slots"]["a"]["execution_id"],
                                  {"status": "stopped"}, self.env)
        done = self.liveness_tick(db, driver, 10 ** 7)
        self.assertEqual(done["tasks"]["a"]["status"], "running")
        self.assertIsNone(done["tasks"]["a"]["error"])

    def test_malformed_liveness_state_is_never_reinterpreted(self):
        self.init(self.spec([task("a", self.paths[0], mode="read")]))
        path = Path(team.team_path("squad", self.env))
        for field, value in (("started_at", "not-a-stamp"),
                             ("progress_at", "2026-13-45T99:99:99Z"),
                             ("progress_key", ["not", "a", "fingerprint"])):
            with self.subTest(field=field):
                record = json.loads(path.read_text())
                record["slots"]["a"][field] = value
                path.write_text(json.dumps(record))
                with self.assertRaises(team.TeamError):
                    team.status("squad", env=self.env)

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
        def adapted(team_id, *, config, driver, env=None, registry_db=None,
                    clock=None):
            return original(team_id, config=config, driver=driver, env=env,
                            execute=fake, writer_snapshot=lambda _: (0, set()),
                            clock=clock)
        with mock.patch("cao.team.tick", side_effect=adapted) as called:
            outcome = team.tick_all(config=self.config, driver=None, env=self.env)
        called.assert_called_once()
        self.assertEqual(outcome[0]["tasks"]["b"]["status"], "complete")
        self.assertEqual(outcome[0]["tasks"]["c"]["status"], "pending")
        self.assertNotIn("c", counts)

    def test_registry_failure_on_pending_does_not_skip_running_sibling(self):
        self.init(self.spec([task("a", self.paths[0]), task("b", self.paths[1])],
                            max_writers=1))
        calls = []
        def fake(**kw):
            calls.append(kw["task"]["id"])
            return self.done() if len(calls) > 1 else {"status": "running"}
        self.tick(fake)
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

    def test_checkpoint_before_claim_respects_team_writer_cap(self):
        self.init(self.spec([task("a", self.paths[0]), task("b", self.paths[1])],
                            max_writers=1))
        p = Path(team.team_path("squad", self.env))
        data = json.loads(p.read_text())
        for name in ("a", "b"):
            data["slots"][name].update(
                status="running",
                execution_id=team._execution_id("squad", name, "work", 0),
                prompt="Previously checkpointed exact user task")
        p.write_text(json.dumps(data))
        # No product_task record exists for either slot. Both are this team's
        # own durable WRITE intents, so max_writers=1 admits neither first
        # claim even though the shared Registry is otherwise empty: the bound
        # is the team's own reserved floor, not the global writer count.
        result = team.tick("squad", config=self.config, driver=None,
                           env=self.env, writer_snapshot=lambda _: (0, set()),
                           registry_db="unused")
        for name in ("a", "b"):
            self.assertEqual(result["tasks"][name]["status"], "running")
            self.assertFalse(Path(product_state.task_record_path(
                data["slots"][name]["execution_id"], self.env)).exists())

    def test_exclusive_registry_claim_refuses_team_dispatch(self):
        self.init(self.spec([task("a", self.paths[0])], max_writers=1))
        db = str(self.root / "exclusive-registry.sqlite3")
        registry.connect(db).close()
        conn = registry.connect(db)
        try:
            registry.propose(conn, "other-run", "other-work", self.paths[1],
                             paths=["a.txt"])
            conn.execute(
                "INSERT INTO claims (run_id, work_key, issue, owner, mode, "
                "branch, worktree, claimed_at) VALUES (?,?,?,?,?,?,?,?)",
                ("other-run", "other-work", "", "other-owner", "EXCLUSIVE",
                 "", "", registry.now()),
            )
        finally:
            conn.close()
        called = []
        result = team.tick(
            "squad", config=self.config, driver=None, env=self.env,
            execute=lambda **kw: called.append(kw),
            registry_db=db, writer_snapshot=team._active_writes)
        self.assertEqual(called, [])
        self.assertEqual(result["tasks"]["a"]["status"], "pending")
        self.assertEqual(result["tasks"]["a"]["error"], "REGISTRY_OCCUPANCY_UNKNOWN")

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


    # -- B9 recovery claim-row safety -------------------------------------

    def _b9_open_write(self):
        repo = str(git_repo(self.root, "b9-checkout", files=("README.md",)))
        db = str(self.root / "b9-registry.sqlite3")
        identity = team._execution_id("b9", "fix", "work", 0)
        conn = registry.connect(db)
        try:
            claim_id = conn.execute(
                "INSERT INTO claims "
                "(run_id, work_key, issue, owner, mode, branch, worktree, claimed_at) "
                "VALUES (?, ?, '', ?, 'WRITE', 'main', ?, ?)",
                ("cao-team-b9", identity, product_task.OWNER,
                 repo, "2026-10-15T00:00:00Z")).lastrowid
            conn.commit()
        finally:
            conn.close()
        git_head = subprocess.check_output(
            ["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
        durable = {"claim": {"claim_id": claim_id, "mode": "WRITE",
                             "base_branch": "main", "base_head": git_head},
                   "checkpoint": {}}
        slot = {"phase": "work", "status": "unknown",
                "error": "TASK_STATUS_UNVERIFIED", "execution_id": identity,
                "prompt": "Fix safely", "head": None,
                "pinned_head": git_head, "pinned_branch": "main"}
        spec = task("fix", repo, mode="write")

        def observe(*, backing=None, rev=None, branch="main"):
            with mock.patch("cao.product_state.read_task",
                            return_value=durable if backing is None else backing):
                return team._recovery_evidence(
                    "b9", spec, slot, driver=None, db=db,
                    head_reader=lambda _: git_head if rev is None else rev,
                    branch_reader=lambda _: branch,
                    occupancy=team._active_writes,
                    env=self.env, clock=None, prior_resumes=0)
        return db, repo, claim_id, durable, observe

    def test_b9_self_only_write_is_not_foreign_or_resumable(self):
        db, repo, claim_id, durable, observe = self._b9_open_write()
        for _ in range(2):
            snapshot = observe()
            verdict = team_recovery.classify(snapshot)
            self.assertFalse(snapshot["checkout_conflict"])
            self.assertFalse(snapshot["registry_unreadable"])
            self.assertEqual(snapshot["registry_claim"]["claim_id"], claim_id)
            self.assertEqual(verdict["classification"], team_recovery.WRITE_UNVERIFIED)
            self.assertEqual(verdict["decision"], team_recovery.HOLD)
            self.assertFalse(verdict["auto_resume"])
        self.assertEqual(self._claim_count(db), 1)

    def test_b9_foreign_claim_same_checkout_still_conflicts(self):
        db, repo, claim_id, durable, observe = self._b9_open_write()
        conn = registry.connect(db)
        try:
            conn.execute(
                "INSERT INTO claims "
                "(run_id, work_key, issue, owner, mode, branch, worktree, claimed_at) "
                "VALUES ('foreign', 'work', '', 'other', 'WRITE', 'main', ?, "
                "'2026-10-15T00:00:01Z')", (repo,))
            conn.commit()
        finally:
            conn.close()
        for _ in range(2):
            snapshot = observe()
            verdict = team_recovery.classify(snapshot)
            self.assertTrue(snapshot["checkout_conflict"])
            self.assertEqual(verdict["classification"],
                             team_recovery.REGISTRY_CONFLICT)
            self.assertEqual(verdict["decision"], team_recovery.HOLD)
            self.assertFalse(verdict["auto_resume"])
        self.assertEqual(self._claim_count(db), 2)


    def test_b9_mismatched_released_exclusive_and_unreadable_hold(self):
        db, repo, claim_id, durable, observe = self._b9_open_write()
        for changed in (
            {**durable["claim"], "base_branch": "other"},
            {**durable["claim"], "mode": "READ"},
            {**durable["claim"], "claim_id": claim_id + 1},
        ):
            snap = observe(backing={"claim": changed, "checkpoint": {}})
            self.assertTrue(snap["checkout_conflict"])
            self.assertEqual(team_recovery.classify(snap)["decision"],
                             team_recovery.HOLD)
        self.assertEqual(team_recovery.classify(
            observe(rev="b" * 40))["classification"], team_recovery.GIT_DRIFT)
        self.assertEqual(team_recovery.classify(
            observe(branch="unexpected"))["classification"],
            team_recovery.GIT_DRIFT)
        conn = registry.connect(db)
        try:
            conn.execute(
                "INSERT INTO claims "
                "(run_id, work_key, issue, owner, mode, branch, worktree, claimed_at) "
                "VALUES ('separate', 'lock', '', 'other', 'EXCLUSIVE', 'other', "
                "?, '2026-10-15T00:00:02Z')", (str(self.root / "other"),))
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(observe()["checkout_conflict"])
        self.assertEqual(team_recovery.classify(observe())["classification"],
                         team_recovery.REGISTRY_CONFLICT)
        Path(db).write_bytes(b"not a database")
        unreadable = observe()
        self.assertTrue(unreadable["registry_unreadable"])
        self.assertEqual(team_recovery.classify(unreadable)["decision"],
                         team_recovery.HOLD)

    def test_b9_released_original_does_not_trigger_write_retry(self):
        db, repo, claim_id, durable, observe = self._b9_open_write()
        conn = registry.connect(db)
        try:
            conn.execute(
                "UPDATE claims SET released_at='2026-10-15T00:01:00Z', "
                "outcome='abandon' WHERE claim_id=?", (claim_id,))
            conn.commit()
        finally:
            conn.close()
        snapshot = observe()
        result = team_recovery.classify(snapshot)
        self.assertTrue(snapshot["registry_claim"]["released"])
        self.assertEqual(result["decision"], team_recovery.HOLD)
        self.assertFalse(result["auto_resume"])
        self.assertEqual(self._claim_count(db), 1)

    def test_b9_corrupt_cached_decision_cannot_resume_write(self):
        self.init(self.spec([task("ship", self.paths[0], mode="write")]))
        record = team._required("squad", self.env)
        slot = record["slots"]["ship"]
        identity = team._execution_id("squad", "ship", "work", 0)
        slot.update(status="unknown", error="TASK_STATUS_UNVERIFIED",
                    execution_id=identity, prompt="Test unsafe saved receipt")
        evidence_snapshot = {
            "task_id": "ship", "mode": "WRITE", "phase": "work",
            "status": "unknown", "error": "TASK_STATUS_UNVERIFIED",
            "execution_id": identity, "now": "2026-10-15T00:00:00Z",
        }
        fresh = team_recovery.classify(evidence_snapshot)
        forged = {**fresh, "decision": team_recovery.RESUME_READ,
                  "auto_resume": True,
                  "fingerprint": team_recovery.fingerprint(fresh),
                  "at": "2026-10-15T00:00:00Z"}
        record["recovery"] = {
            "schema": team_recovery.SCHEMA,
            "decisions": {"ship": forged}, "resumed": {},
        }
        with mock.patch("cao.team._recovery_evidence",
                        return_value=evidence_snapshot):
            report = team._apply_recovery(
                "squad", record, driver=None, db="unused", head_reader=None,
                branch_reader=None, occupancy=None, env=self.env,
                clock=None)
        self.assertEqual(report[0]["decision"], team_recovery.HOLD)
        self.assertFalse(report[0]["auto_resume"])
        self.assertEqual(slot["status"], "unknown")
        self.assertEqual(record["recovery"]["resumed"], {})

    # -- R3 automatic safe recovery ---------------------------------------

    def _recovery_setup(self, *, team_id="squad", task_id="inspect",
                        mode="read", error="TEAM_RESULT_UNREADABLE",
                        driver=None):
        """Build a real READ product task with an open claim, then hold the
        team slot at the exact same original identity but UNKNOWN."""

        os.makedirs(self.paths[0], exist_ok=True)
        driver = driver or FakeDriver()
        driver.agent_present = True
        db = product_config.registry_db_path(self.env)
        task = {"id": task_id,
                "role": "analyst" if mode == "read" else "developer",
                "repo": self.paths[0], "prompt": "Analyze source",
                "mode": mode,
                "write_scope": ["README.md"] if mode == "write" else [],
                "depends_on": []}
        self.init(self.spec([task], team_id=team_id))
        ident = team._execution_id(team_id, task_id, "work", 0)
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "opencode/dev"},
            "task": {"agent_kind": "opencode", "run_id": "cao-team-" + team_id},
        })
        product_task.run_task(
            driver=driver, repo=task["repo"], prompt=task["prompt"],
            task_id=ident, mode=mode, write_scope=task["write_scope"],
            config=config, env=self.env, registry_db=db, wait_ms=0)
        record = team._required(team_id, self.env)
        slot = record["slots"][task_id]
        slot.update(status="unknown", error=error, execution_id=ident,
                    prompt=task["prompt"], pinned_head=A, pinned_branch="main",
                    started_at="2026-01-01T00:00:00Z",
                    progress_at="2026-01-01T00:00:00Z")
        team._save(record, self.env)
        return driver, db, ident, config

    def _recovery_tick(self, driver, db, *, execute=None, head=A, branch="main",
                       writers=0, worktrees=None, team_id="squad"):
        return team.tick(
            team_id, env=self.env, config=self.config, driver=driver,
            registry_db=db, execute=execute,
            writer_snapshot=lambda _db: (writers, worktrees or set()),
            head_reader=lambda _repo: head,
            branch_reader=lambda _repo: branch,
            clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))

    def _claim_count(self, db):
        conn = registry.connect_readonly(db)
        try:
            return conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        finally:
            conn.close()

    def _decision(self, team_id="squad", task_id="inspect"):
        return team._required(team_id, self.env)["recovery"]["decisions"][task_id]

    def test_recovery_resumes_the_original_read_identity_without_reprompt(self):
        driver, db, ident, _ = self._recovery_setup()
        calls = []

        def fake(**kwargs):
            calls.append((kwargs["execution_id"], kwargs["prompt"]))
            return {"status": "unknown",
                    "error": {"code": "TEAM_RESULT_UNREADABLE"}}

        result = self._recovery_tick(driver, db, execute=fake)
        # The SAME original execution identity and prompt were reconciled.
        self.assertEqual(calls, [(ident, "Analyze source")])
        decision = self._decision()
        self.assertEqual(decision["classification"], team_recovery.READ_TRANSPORT)
        self.assertEqual(decision["decision"], team_recovery.RESUME_READ)
        self.assertTrue(decision["auto_resume"])
        self.assertEqual(decision["provenance"]["source_head"], A)
        self.assertEqual(decision["provenance"]["claim_id"], 1)
        self.assertEqual(
            team._required("squad", self.env)["recovery"]["resumed"][ident], 1)
        self.assertEqual(result["recovery"][0]["auto_resume"], True)
        # No new claim and no prompt replay ever happened.
        self.assertEqual(self._claim_count(db), 1)
        self.assertEqual(driver.names().count("agent_prompt"), 1)

    def test_recovery_duplicate_tick_holds_after_one_bounded_resume(self):
        driver, db, ident, _ = self._recovery_setup()

        def fake(**kwargs):
            return {"status": "unknown",
                    "error": {"code": "TEAM_RESULT_UNREADABLE"}}

        self._recovery_tick(driver, db, execute=fake)
        second = self._recovery_tick(driver, db, execute=fake)
        record = team._required("squad", self.env)
        self.assertEqual(record["slots"]["inspect"]["status"], "unknown")
        self.assertEqual(record["recovery"]["resumed"][ident], 1)
        self.assertEqual(record["recovery"]["decisions"]["inspect"]["decision"],
                         team_recovery.HOLD)
        self.assertEqual(second["recovery"][0]["decision"], team_recovery.HOLD)
        self.assertEqual(self._claim_count(db), 1)

    def test_recovery_receipt_is_idempotent_across_ticks(self):
        driver, db, ident, _ = self._recovery_setup()
        # A stale head keeps the slot UNKNOWN across ticks without resuming.
        self._recovery_tick(driver, db, head=B)
        first = self._decision()
        self.assertEqual(first["classification"], team_recovery.GIT_DRIFT)
        self.assertEqual(first["decision"], team_recovery.HOLD)
        self._recovery_tick(driver, db, head=B)
        second = self._decision()
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["at"], second["at"])
        self.assertEqual(team._required("squad", self.env)
                         ["slots"]["inspect"]["status"], "unknown")
        self.assertNotIn(ident, team._required("squad", self.env)["recovery"]["resumed"])

    def test_recovery_tool_refusal_blocks_and_changes_no_claim(self):
        driver, db, ident, _ = self._recovery_setup(error="TEAM_TOOL_DENIED")
        before = self._claim_count(db)
        self._recovery_tick(driver, db)
        decision = self._decision()
        self.assertEqual(decision["classification"], team_recovery.SECURITY_FAILURE)
        self.assertEqual(decision["decision"], team_recovery.BLOCK)
        self.assertFalse(decision["auto_resume"])
        self.assertEqual(team._required("squad", self.env)
                         ["slots"]["inspect"]["status"], "unknown")
        self.assertEqual(self._claim_count(db), before)

    def test_recovery_provider_denial_holds_without_claim(self):
        self.init(self.spec([task("a", self.paths[0], mode="read")]))
        ident = team._execution_id("squad", "a", "work", 0)
        record = team._required("squad", self.env)
        record["slots"]["a"].update(
            status="unknown", error="PROVIDER_DENIED", execution_id=ident,
            prompt="Analyze source")
        team._save(record, self.env)
        result = self._recovery_tick(None, product_config.registry_db_path(self.env))
        decision = self._decision("squad", "a")
        self.assertEqual(decision["classification"], team_recovery.INTERACTIVE_GATE)
        self.assertEqual(decision["decision"], team_recovery.HOLD)
        self.assertFalse(decision["auto_resume"])
        self.assertEqual(team._required("squad", self.env)
                         ["slots"]["a"]["status"], "unknown")

    def test_recovery_absent_task_record_holds(self):
        driver, db, ident, _ = self._recovery_setup()
        with mock.patch("cao.product_state.read_task", return_value=None):
            self._recovery_tick(driver, db)
        decision = self._decision()
        self.assertEqual(decision["decision"], team_recovery.HOLD)
        self.assertFalse(decision["auto_resume"])
        self.assertEqual(team._required("squad", self.env)
                         ["slots"]["inspect"]["status"], "unknown")
        self.assertEqual(self._claim_count(db), 1)

    def test_recovery_no_matched_pane_holds(self):
        driver, db, ident, _ = self._recovery_setup()
        driver.agent_present = False
        self._recovery_tick(driver, db)
        decision = self._decision()
        self.assertEqual(decision["classification"], team_recovery.READ_TRANSPORT)
        self.assertEqual(decision["decision"], team_recovery.HOLD)
        self.assertFalse(decision["auto_resume"])

    def test_recovery_stale_branch_holds(self):
        driver, db, ident, _ = self._recovery_setup()
        self._recovery_tick(driver, db, branch="release")
        decision = self._decision()
        self.assertEqual(decision["classification"], team_recovery.GIT_DRIFT)
        self.assertEqual(decision["decision"], team_recovery.HOLD)

    def test_recovery_competing_worker_conflict_holds(self):
        driver, db, ident, _ = self._recovery_setup()
        self._recovery_tick(driver, db, writers=1,
                            worktrees={os.path.realpath(self.paths[0])})
        decision = self._decision()
        self.assertEqual(decision["classification"], team_recovery.REGISTRY_CONFLICT)
        self.assertEqual(decision["decision"], team_recovery.HOLD)
        self.assertFalse(decision["auto_resume"])

    def test_recovery_concurrent_dispatch_lock_refuses_second_tick(self):
        driver, db, ident, _ = self._recovery_setup()
        with team.dispatch_lock(self.env):
            with self.assertRaises(team.TeamError):
                self._recovery_tick(driver, db)

    def test_recovery_source_complete_write_is_never_replayed(self):
        self.init(self.spec([task("ship", self.paths[0], mode="write")]))
        ident = team._execution_id("squad", "ship", "work", 0)
        record = team._required("squad", self.env)
        record["slots"]["ship"].update(
            status="unknown", error="TASK_RELEASE_UNVERIFIED",
            execution_id=ident, prompt="Implement a bounded change")
        team._save(record, self.env)
        self._recovery_tick(None, product_config.registry_db_path(self.env))
        decision = self._decision("squad", "ship")
        self.assertEqual(decision["classification"], team_recovery.WRITE_UNVERIFIED)
        self.assertEqual(decision["decision"], team_recovery.HOLD)
        self.assertFalse(decision["auto_resume"])
        self.assertEqual(team._required("squad", self.env)
                         ["slots"]["ship"]["status"], "unknown")

    def test_recovery_malformed_receipt_is_refused(self):
        driver, db, ident, _ = self._recovery_setup()
        self._recovery_tick(driver, db, head=B)
        path = Path(team.team_path("squad", self.env))
        record = json.loads(path.read_text())
        record["recovery"] = {"schema": "cao.team.recovery.v0", "decisions": {},
                              "resumed": {}}
        path.write_text(json.dumps(record))
        # A receipt of the wrong schema is refused, never reinterpreted.
        with self.assertRaises(team.TeamError):
            team.status("squad", env=self.env)
        with self.assertRaises(team.TeamError):
            self._recovery_tick(driver, db, head=B)

    def test_recovery_no_egress_modules_are_used(self):
        driver, db, ident, _ = self._recovery_setup()
        import socket
        with mock.patch.object(socket, "socket",
                               side_effect=AssertionError("egress")) as net, \
                mock.patch.object(socket, "create_connection",
                                  side_effect=AssertionError("egress")):
            self._recovery_tick(driver, db)
        net.assert_not_called()


if __name__ == "__main__":
    unittest.main()