"""Chat-confirmed CAO teams; no signer or credential infrastructure."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cao import product_config, registry, state, team, team_cli


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

    def test_routing_change_holds_before_any_agent_effect(self):
        self.admit(approved=True)
        team.set_desired("alpha", "running", env=self.env)
        self.raw["provider"]["model"] = "opencode/changed"
        self.configure()
        effects = []
        with self.assertRaises(team.TeamAdmissionError):
            team.tick("alpha", config=self.config, driver=None, env=self.env,
                      execute=lambda **kwargs: effects.append(kwargs),
                      writer_snapshot=lambda _: (0, set()),
                      head_reader=lambda _: "a" * 40)
        self.assertEqual(effects, [])

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
