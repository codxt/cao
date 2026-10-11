"""Portable Team Execution agent-profile tests; no provider or host needed."""
import tempfile
import unittest
from pathlib import Path

from cao import agent_profiles, product_config, team


class ProfilesTests(unittest.TestCase):
    def setUp(self):
        self.profiles = {
            "claude_review": {"agent_kind": "claude", "read_args":
                ["--model", "sonnet", "--effort", "high", "--permission-mode", "plan"]},
            "codex_dev": {"agent_kind": "codex", "read_args":
                ["--model", "gpt-example", "--sandbox", "read-only"],
                "write_args": ["--model", "gpt-example", "--sandbox", "workspace-write"]},
            "gemini": {"agent_kind": "gemini", "read_args": []},
            "opencode": {"agent_kind": "opencode", "read_args": []},
        }
        self.config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "id": "local", "model": "opencode/default"},
            "task": {"agent_kind": "opencode"},
            "agent_profiles": self.profiles,
        })

    def test_provider_agnostic_profiles_and_modes(self):
        for name, kind in (("claude_review", "claude"), ("codex_dev", "codex"),
                           ("gemini", "gemini"), ("opencode", "opencode")):
            spec = {"schema": "cao.team.v1", "team_id": "portable",
                    "roles": {"review": {"instruction": "Read", "agent_profile": name}},
                    "tasks": [{"id": "r", "role": "review",
                               "repo": "/tmp/profile-test", "prompt": "Read",
                               "mode": "read"}]}
            team.validate_spec(spec)
            config = team._role_config(self.config, spec, spec["tasks"][0], "work")
            defaults = product_config.task_defaults(config)
            self.assertEqual(defaults.agent_kind, kind)
            self.assertEqual(defaults.agent_args, tuple(self.profiles[name]["read_args"]))
        self.assertEqual(agent_profiles.resolve_profile(self.profiles, "codex_dev", "write")[1],
                         tuple(self.profiles["codex_dev"]["write_args"]))

    def test_profile_missing_mode_does_not_fallback_to_write(self):
        with self.assertRaises(agent_profiles.AgentProfileError):
            agent_profiles.resolve_profile(self.profiles, "claude_review", "write")
        with self.assertRaises(agent_profiles.AgentProfileError):
            agent_profiles.resolve_profile(self.profiles, "no_such_profile", "read")

    def test_invalid_profiles_fail_closed(self):
        for invalid in (
            {"a": {"agent_kind": "claude", "read_args": ["--dangerously-skip-permissions"]}},
            {"a": {"agent_kind": "codex", "write_args": ["--sandbox", "danger-full-access"]}},
            {"a": {"agent_kind": "claude", "read_args": ["\n"]}},
            {"a": {"agent_kind": "bad agent", "read_args": []}},
            {"a": {"agent_kind": "claude", "model": "secret", "read_args": []}},
            {"a": {"agent_kind": "claude"}},
        ):
            with self.subTest(profile=invalid), self.assertRaises(product_config.ProductConfigError):
                product_config.validate_config({
                    "schema": product_config.SCHEMA, "agent_profiles": invalid})

    def test_existing_default_stays_opencode(self):
        spec = {"schema": "cao.team.v1", "team_id": "original",
                "roles": {"read": {"instruction": "Read"}},
                "tasks": [{"id": "r", "role": "read", "repo": "/tmp/profile-test",
                           "mode": "read", "prompt": "Read"}]}
        selected = team._role_config(self.config, spec, spec["tasks"][0], "work")
        defaults = product_config.task_defaults(selected)
        self.assertEqual(defaults.agent_kind, "opencode")
        self.assertEqual(defaults.agent_args,
                         ("mini", "--standalone", "--model", "opencode/default"))

    def test_role_cannot_mix_inline_model_and_profile(self):
        spec = {"schema": "cao.team.v1", "team_id": "original",
                "roles": {"read": {"instruction": "Read",
                                   "agent_profile": "codex_dev",
                                   "model": "opencode/other"}},
                "tasks": [{"id": "r", "role": "read", "repo": "/tmp/profile-test",
                           "mode": "read", "prompt": "Read"}]}
        with self.assertRaises(team.TeamError):
            team.validate_spec(spec)


if __name__ == "__main__":
    unittest.main()
