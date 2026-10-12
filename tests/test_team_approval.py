"""Chat-confirmed CAO teams; no signer or credential infrastructure."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cao import product_config, registry, state, team, team_approval, team_cli


class ChatTeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {"XDG_STATE_HOME": str(self.root / "state"),
                    "XDG_CONFIG_HOME": str(self.root / "config")}
        self.raw = {"schema": product_config.SCHEMA,
                    "provider": {"cli": "opencode", "id": "test",
                                 "model": "opencode/default"},
                    "team_approval": {"mode": "chat"},
                    "registry_db": str(self.root / "shared.sqlite3")}
        self.configure()
        self.spec = {"schema": team.SPEC_SCHEMA, "team_id": "alpha",
                     "initiative": "project",
                     "roles": {"reviewer": {"instruction": "Inspect source",
                                            "model": "opencode/review"}},
                     "tasks": [{"id": "read", "role": "reviewer",
                                "repo": str(self.root / "repo"),
                                "mode": "read", "prompt": "Review exact source"}],
                     "max_parallel": 1, "max_writers": 0}

    def configure(self):
        p = Path(product_config.config_path(self.env))
        p.parent.mkdir(parents=True, exist_ok=True)
        product_config.write_config(self.raw, p)
        self.config = product_config.validate_config(self.raw)

    def admit(self, spec=None, approved=False):
        return team.initialize(spec or self.spec, env=self.env,
                               config=self.config, approved=approved)

    def test_chat_approval_is_explicit_and_idempotent(self):
        with self.assertRaises(team.TeamAdmissionError):
            self.admit()
        self.assertEqual(team.list_teams(env=self.env), [])
        record = self.admit(approved=True)
        self.assertEqual(record["status"], "PAUSED")
        self.assertEqual(record["approval"]["method"], "operator-declared")
        self.assertEqual(record, self.admit())
        self.assertEqual(len(team.list_teams(env=self.env)), 1)
        self.assertEqual(team.set_desired("alpha", "running", env=self.env)["desired"],
                         "running")

    def test_cli_plan_and_confirmation(self):
        manifest = self.root / "team.json"
        manifest.write_text(json.dumps(self.spec))
        with mock.patch.dict(os.environ, self.env):
            with contextlib.redirect_stdout(output := io.StringIO()):
                self.assertEqual(team_cli.main(["plan", "--file", str(manifest)]), 0)
            plan = json.loads(output.getvalue())
            self.assertEqual(plan["approval_mode"], "chat")
            self.assertEqual(plan["roles"]["reviewer"]["model"], "opencode/review")
            with contextlib.redirect_stdout(output := io.StringIO()):
                self.assertEqual(team_cli.main(["init", "--file", str(manifest)]), 3)
            self.assertEqual(json.loads(output.getvalue())["code"],
                             "TEAM_APPROVAL_REQUIRED")
            with contextlib.redirect_stdout(output := io.StringIO()):
                self.assertEqual(team_cli.main(["init", "--file", str(manifest),
                                                "--approved"]), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "PAUSED")
        self.assertEqual(len(team.list_teams(env=self.env)), 1)

    def tick(self, effects):
        return team.tick("alpha", config=self.config, driver=None, env=self.env,
                         execute=lambda **kwargs: effects.append(kwargs)
                         or dict(status="running"),
                         writer_snapshot=lambda _: (0, set()),
                         head_reader=lambda _: "a" * 40)

    def test_recovered_unclaimed_read_intent_rechecks_chat_approval(self):
        self.admit(approved=True)
        team.set_desired("alpha", "running", env=self.env)
        path = Path(team.team_path("alpha", self.env))
        record = state.read_state(path)
        intent = team._execution_id("alpha", "read", "work", 0)
        record["slots"]["read"].update(
            status="running", execution_id=intent, prompt="Saved prompt intent")
        state.write_state(path, record)
        self.raw["provider"]["id"] = "changed-provider"
        self.configure()
        with mock.patch.object(team, "_execute", return_value={"status": "running"}) as dispatch:
            held = team.tick(
                "alpha", config=self.config, driver=None, env=self.env,
                writer_snapshot=lambda _: (0, set()),
                head_reader=lambda _: "a" * 40)
            dispatch.assert_not_called()
        self.assertEqual(held["tasks"]["read"]["execution_id"], intent)
        self.assertEqual(held["tasks"]["read"]["status"], "running")
        self.assertEqual(held["tasks"]["read"]["error"], "TEAM_ADMISSION_CHANGED")
        self.raw["provider"]["id"] = "test"
        self.configure()
        with mock.patch.object(team, "_execute", return_value={"status": "running"}) as dispatch:
            resumed = team.tick(
                "alpha", config=self.config, driver=None, env=self.env,
                writer_snapshot=lambda _: (0, set()),
                head_reader=lambda _: "a" * 40)
            dispatch.assert_called_once()
        self.assertEqual(resumed["tasks"]["read"]["execution_id"], intent)
        self.assertIsNone(resumed["tasks"]["read"]["error"])

    def test_recovered_unclaimed_write_intent_preserves_own_capacity(self):
        self.spec["tasks"][0].update(mode="write", write_scope=["README.md"])
        self.spec["max_writers"] = 1
        self.admit(approved=True)
        team.set_desired("alpha", "running", env=self.env)
        path = Path(team.team_path("alpha", self.env))
        record = state.read_state(path)
        intent = team._execution_id("alpha", "read", "work", 0)
        record["slots"]["read"].update(
            status="running", execution_id=intent, prompt="Saved write intent")
        state.write_state(path, record)
        self.raw["provider"]["id"] = "changed-provider"
        self.configure()
        with mock.patch.object(team, "_execute", return_value={"status": "running"}) as dispatch:
            held = team.tick(
                "alpha", config=self.config, driver=None, env=self.env,
                writer_snapshot=lambda _: (0, set()),
                head_reader=lambda _: "a" * 40)
            dispatch.assert_not_called()
        self.assertEqual(held["tasks"]["read"]["execution_id"], intent)
        self.assertEqual(held["tasks"]["read"]["error"], "TEAM_ADMISSION_CHANGED")
        self.raw["provider"]["id"] = "test"
        self.configure()
        with mock.patch.object(team, "_execute", return_value={"status": "running"}) as dispatch:
            resumed = team.tick(
                "alpha", config=self.config, driver=None, env=self.env,
                writer_snapshot=lambda _: (0, set()),
                head_reader=lambda _: "a" * 40)
            dispatch.assert_called_once()
        self.assertEqual(resumed["tasks"]["read"]["execution_id"], intent)
        self.assertIsNone(resumed["tasks"]["read"]["error"])

    def test_effective_routes_verified_once_per_dispatch_tick(self):
        self.spec["tasks"].append(
            {"id": "other", "role": "reviewer",
             "repo": str(self.root / "repo2"),
             "mode": "read", "prompt": "Inspect second source"})
        self.spec["max_parallel"] = 2
        self.admit(approved=True)
        team.set_desired("alpha", "running", env=self.env)
        effects = []
        with mock.patch.object(team, "_effective_routes",
                               wraps=team._effective_routes) as resolve:
            result = self.tick(effects)
            resolve.assert_called_once()
        self.assertEqual(len(effects), 2)
        self.assertEqual(result["tasks"]["read"]["status"], "running")
        self.assertEqual(result["tasks"]["other"]["status"], "running")

    def test_unrelated_configuration_change_does_not_invalidate_an_admitted_team(self):
        self.admit(approved=True)
        team.set_desired("alpha", "running", env=self.env)
        self.raw["registry_db"] = str(self.root / "moved.sqlite3")
        self.raw["runtime_id"] = "renamed-runtime"
        self.raw["daemon"] = {"tick_interval": 45}
        self.raw["agent_profiles"] = {"unused": {"agent_kind": "opencode",
                                                 "read_args": ["mini"]}}
        # A role model is what this team dispatches, so the base provider model
        # that no agent of it reads is an unrelated change.
        self.raw["provider"]["model"] = "opencode/changed"
        self.configure()
        effects = []
        self.assertEqual(self.tick(effects)["tasks"]["read"]["status"], "running")
        self.assertEqual(len(effects), 1)

    def test_dispatched_model_selection_change_holds_before_any_agent_effect(self):
        self.spec["roles"]["reviewer"] = {
            "instruction": "Inspect source", "agent_profile": "read"}
        self.raw["agent_profiles"] = {"read": {"agent_kind": "opencode",
                                               "read_args": ["mini"]}}
        self.configure()
        self.admit(approved=True)
        team.set_desired("alpha", "running", env=self.env)
        # The profile this role dispatches is re-pointed at another agent kind.
        self.raw["agent_profiles"]["read"] = {"agent_kind": "other",
                                              "read_args": ["go"]}
        self.configure()
        effects = []
        result = self.tick(effects)
        self.assertEqual(effects, [])
        self.assertEqual(result["tasks"]["read"]["status"], "pending")
        self.assertEqual(result["tasks"]["read"]["error"],
                         "TEAM_ADMISSION_CHANGED")

    def test_dispatched_provider_change_holds_before_any_agent_effect(self):
        self.admit(approved=True)
        team.set_desired("alpha", "running", env=self.env)
        self.raw["provider"]["id"] = "renamed-provider"
        self.configure()
        effects = []
        result = self.tick(effects)
        self.assertEqual(effects, [])
        self.assertEqual(result["tasks"]["read"]["status"], "pending")
        self.assertEqual(result["tasks"]["read"]["error"],
                         "TEAM_ADMISSION_CHANGED")

    def test_inherited_provider_change_holds_before_any_agent_effect(self):
        # A role that declares neither a model nor a profile dispatches with the
        # configured provider, so a provider change still holds before dispatch.
        self.spec["roles"]["reviewer"] = {"instruction": "Inspect source"}
        self.configure()
        self.admit(approved=True)
        team.set_desired("alpha", "running", env=self.env)
        self.raw["provider"]["model"] = "opencode/changed"
        self.configure()
        effects = []
        result = self.tick(effects)
        self.assertEqual(effects, [])
        self.assertEqual(result["tasks"]["read"]["status"], "pending")
        self.assertEqual(result["tasks"]["read"]["error"],
                         "TEAM_ADMISSION_CHANGED")

    def test_running_tasks_are_reconciled_when_new_dispatch_is_refused(self):
        self.admit(approved=True)
        team.set_desired("alpha", "running", env=self.env)
        effects = []
        self.tick(effects)
        self.assertEqual(len(effects), 1)
        # Nothing is pending any more: the dispatched task keeps its exact
        # execution identity and must still be reconciled and released even
        # though the configuration now selects a different provider.
        record = state.read_state(Path(team.team_path("alpha", self.env)))
        record["slots"]["read"]["status"] = "running"
        state.write_state(Path(team.team_path("alpha", self.env)), record)
        self.raw["provider"]["id"] = "renamed-provider"
        self.configure()
        reconciled = []
        result = self.tick(reconciled)
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(result["tasks"]["read"]["status"], "running")

    def test_pending_sibling_is_held_while_a_running_task_reconciles(self):
        # A two-task team where capacity leaves a pending sibling beside the
        # running task: a changed effective route must refuse ONLY new dispatch.
        self.spec["roles"]["worker"] = {
            "instruction": "Analyze further", "model": "opencode/work"}
        self.spec["tasks"].append(
            {"id": "work", "role": "worker", "repo": str(self.root / "repo2"),
             "mode": "read", "prompt": "Analyze the other source"})
        self.spec["max_parallel"] = 1
        self.admit(approved=True)
        team.set_desired("alpha", "running", env=self.env)
        actions = []

        def fake(**kwargs):
            actions.append(kwargs["task"]["id"])
            return dict(status="running")

        result = team.tick("alpha", config=self.config, driver=None, env=self.env,
                           execute=fake, writer_snapshot=lambda _: (0, set()),
                           head_reader=lambda _: "a" * 40)
        self.assertEqual(actions, ["read"])
        self.assertEqual(result["tasks"]["read"]["status"], "running")
        self.assertEqual(result["tasks"]["work"]["status"], "pending")
        self.raw["provider"]["id"] = "renamed-provider"
        self.configure()

        def completing(**kwargs):
            actions.append(kwargs["task"]["id"])
            return dict(status="complete",
                        claim={"released": True, "released_head": "a" * 40},
                        output="final answer")

        result = team.tick("alpha", config=self.config, driver=None, env=self.env,
                           execute=completing, writer_snapshot=lambda _: (0, set()),
                           head_reader=lambda _: "a" * 40)
        # The already-running task keeps its exact identity, is reconciled and
        # released; the refused NEW dispatch only holds the pending sibling.
        self.assertEqual(actions, ["read", "read"])
        self.assertEqual(result["tasks"]["read"]["status"], "complete")
        self.assertEqual(result["tasks"]["work"]["status"], "pending")
        self.assertEqual(result["tasks"]["work"]["error"],
                         "TEAM_ADMISSION_CHANGED")
        # Restoring the effective route is the in-product remedy for the held
        # sibling: it dispatches on the next tick without a successor team.
        self.raw["provider"]["id"] = "test"
        self.configure()
        result = team.tick("alpha", config=self.config, driver=None, env=self.env,
                           execute=fake, writer_snapshot=lambda _: (0, set()),
                           head_reader=lambda _: "a" * 40)
        self.assertEqual(actions, ["read", "read", "work"])
        self.assertEqual(result["tasks"]["work"]["status"], "running")
        self.assertEqual(result["tasks"]["work"]["error"], None)

    def test_absent_configuration_refuses_new_dispatch_of_an_approved_team(self):
        self.admit(approved=True)
        with mock.patch.object(product_config, "load_config", return_value=None):
            with self.assertRaises(team.TeamAdmissionError):
                team.set_desired("alpha", "running", env=self.env)
        self.assertEqual(team.status("alpha", env=self.env)["desired"], "paused")

    def test_legacy_confirmation_record_still_verifies(self):
        self.admit(approved=True)
        team.set_desired("alpha", "running", env=self.env)
        self.tick([])
        # An admission recorded against the raw configuration document by an
        # earlier release keeps verifying instead of freezing the team.
        record = state.read_state(Path(team.team_path("alpha", self.env)))
        record["admission"] = team_approval.confirmation(record["spec"], self.config)
        state.write_state(Path(team.team_path("alpha", self.env)), record)
        effects = []
        self.assertEqual(self.tick(effects)["tasks"]["read"]["status"], "running")
        self.assertEqual(len(effects), 1)

    def test_successor_requires_pause_terminal_and_declared_reason(self):
        self.admit(approved=True)
        later = {**self.spec, "team_id": "beta"}
        with self.assertRaises(team.TeamError):
            self.admit(later, approved=True)
        later["supersedes"] = "alpha"
        later["change_reason"] = "Operator inspected the original issue and approved a successor"
        with self.assertRaises(team.TeamError):
            self.admit(later, approved=True)
        db = registry.connect(self.raw["registry_db"])
        db.close()
        p = Path(team.team_path("alpha", self.env))
        data = state.read_state(p)
        data["slots"]["read"]["status"] = "blocked"
        data["slots"]["read"]["error"] = "TEAM_RESULT_UNVERIFIED"
        state.write_state(p, data)
        self.assertEqual(self.admit(later, approved=True)["status"], "PAUSED")

    def test_never_started_team_is_reconfirmed_on_the_identical_manifest(self):
        self.raw.pop("team_approval")
        self.configure()
        self.admit()
        self.assertIsNone(self.admit().get("approval"))
        self.raw["team_approval"] = {"mode": "chat"}
        self.configure()
        with self.assertRaises(team.TeamAdmissionError):
            team.set_desired("alpha", "running", env=self.env)
        confirmed = self.admit(approved=True)
        self.assertEqual(confirmed["approval"]["method"], "operator-declared")
        self.assertEqual(
            confirmed["approval"]["routes_sha256"],
            team_approval.confirmation(
                self.spec, self.config,
                team._effective_routes(self.config, self.spec))["routes_sha256"])
        self.assertEqual(team.set_desired("alpha", "running", env=self.env)["desired"],
                         "running")
        effects = []
        self.assertEqual(self.tick(effects)["tasks"]["read"]["status"], "running")
        self.assertEqual(len(effects), 1)

    def test_dispatched_or_claimed_team_is_never_reapproved(self):
        self.raw.pop("team_approval")
        self.configure()
        self.admit()
        team.set_desired("alpha", "running", env=self.env)
        effects = []
        self.tick(effects)
        self.assertEqual(len(effects), 1)
        # Enabling chat policy afterwards can no longer re-approve this team,
        # because it already dispatched an execution.
        self.raw["team_approval"] = {"mode": "chat"}
        self.configure()
        with self.assertRaises(team.TeamError):
            self.admit(approved=True)
        # The same refusal holds when a dispatched execution was cleared but the
        # shared Registry still holds the team's claim.
        record = state.read_state(Path(team.team_path("alpha", self.env)))
        execution_id = record["slots"]["read"]["execution_id"]
        record["slots"]["read"] = dict(record["slots"]["read"], status="pending",
                                       execution_id=None, prompt=None, error=None)
        state.write_state(Path(team.team_path("alpha", self.env)), record)
        ledger = registry.connect(self.raw["registry_db"])
        try:
            registry.propose(ledger, "cao-team-alpha", execution_id,
                             str(self.root / "repo"), components=["review"])
            registry.claim(ledger, registry.ClaimSpec(
                run_id="cao-team-alpha", work_key=execution_id,
                owner="cao-product", mode="READ", issue=""), None)
        finally:
            ledger.close()
        with self.assertRaises(team.TeamError):
            self.admit(approved=True)

    def test_legacy_pending_is_held_when_chat_policy_is_enabled(self):
        self.raw.pop("team_approval")
        self.configure()
        self.admit()
        team.set_desired("alpha", "running", env=self.env)
        self.raw["team_approval"] = {"mode": "chat"}
        self.configure()
        effects = []
        result = team.tick("alpha", config=self.config, driver=None, env=self.env,
                           execute=lambda **kwargs: effects.append(kwargs),
                           writer_snapshot=lambda _: (0, set()),
                           head_reader=lambda _: "a" * 40)
        self.assertFalse(effects)
        self.assertEqual(result["tasks"]["read"]["status"], "pending")

    def test_old_key_configuration_refused(self):
        self.raw["team_approval"] = {"mode": "signed"}
        with self.assertRaises(product_config.ProductConfigError):
            product_config.validate_config(self.raw)


if __name__ == "__main__":
    unittest.main()
