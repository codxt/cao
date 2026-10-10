"""Real source-launcher CLI trials with isolated state and no agent/process."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class TeamCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.launcher = Path(__file__).resolve().parents[1] / "cao"
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.root / "state"),
                        XDG_CONFIG_HOME=str(self.root / "config"),
                        PYTHONDONTWRITEBYTECODE="1")
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({
            "schema": "cao.team.v1", "team_id": "pilot",
            "roles": {"analyst": {"instruction": "Inspect source without changes"}},
            "tasks": [{
                "id": "inspect", "role": "analyst", "repo": str(self.root / "repo"),
                "prompt": "Inspect the repository", "mode": "read",
            }],
        }), encoding="utf-8")

    def run_cao(self, *args):
        p = subprocess.run([str(self.launcher), "team", *args], env=self.env,
                           cwd=str(self.root), capture_output=True, text=True,
                           timeout=12)
        return p.returncode, json.loads(p.stdout)

    def test_validate_init_start_list_pause_without_runtime(self):
        code, p = self.run_cao("validate", "--file", str(self.manifest))
        self.assertEqual((code, p["status"], p["roles"]), (0, "VALID", 1))
        code, p = self.run_cao("init", "--file", str(self.manifest))
        self.assertEqual((code, p["status"]), (0, "PAUSED"))
        code, p = self.run_cao("start", "--id", "pilot")
        self.assertEqual((code, p["desired"]), (0, "running"))
        code, p = self.run_cao("status", "--id", "pilot")
        self.assertEqual((code, p["tasks"]["inspect"]["status"]), (0, "pending"))
        code, p = self.run_cao("list")
        self.assertEqual((code, len(p["teams"])), (0, 1))
        code, p = self.run_cao("pause", "--id", "pilot")
        self.assertEqual((code, p["desired"]), (0, "paused"))

    def test_resume_only_unknown_task_is_refused_for_pending(self):
        self.run_cao("init", "--file", str(self.manifest))
        code, p = self.run_cao("resume", "--id", "pilot", "--task", "inspect")
        self.assertEqual((code, p["status"]), (3, "UNAVAILABLE"))

    def test_tick_without_product_provider_refuses_no_agent(self):
        self.run_cao("init", "--file", str(self.manifest))
        self.run_cao("start", "--id", "pilot")
        code, p = self.run_cao("tick", "--id", "pilot")
        self.assertEqual((code, p["status"]), (3, "UNAVAILABLE"))
        self.assertEqual(self.run_cao("status", "--id", "pilot")[1]
                         ["tasks"]["inspect"]["status"], "pending")

    def test_invalid_document_denied_and_does_not_create_state(self):
        self.manifest.write_text('{"schema":"fake", "team_id":"pilot"}')
        code, p = self.run_cao("init", "--file", str(self.manifest))
        self.assertEqual((code, p["status"]), (3, "UNAVAILABLE"))
        self.assertFalse((self.root / "state" / "cao" / "teams" / "pilot.json").exists())

    def test_missing_team_and_ambiguous_spec_refused(self):
        code, p = self.run_cao("status", "--id", "absent")
        self.assertEqual((code, p["status"]), (3, "UNAVAILABLE"))
        self.run_cao("init", "--file", str(self.manifest))
        d = json.loads(self.manifest.read_text())
        d["tasks"][0]["prompt"] = "Changed scope"
        self.manifest.write_text(json.dumps(d))
        code, p = self.run_cao("init", "--file", str(self.manifest))
        self.assertEqual((code, p["status"]), (3, "UNAVAILABLE"))


if __name__ == "__main__":
    unittest.main()
