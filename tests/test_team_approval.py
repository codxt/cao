"""Hermetic operator approval, provenance, replacement and dispatch tests."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cao import product_config, product_state, registry, state, team, team_approval, team_cli


@unittest.skipUnless(shutil.which("ssh-keygen"), "OpenSSH signature test requires ssh-keygen")
class SignedTeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        }
        self.key = self.root / "signer"
        result = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)],
            capture_output=True, timeout=12, check=False)
        self.assertEqual(result.returncode, 0)
        self.trusted = self.root / "operators"
        self.trusted.write_text("operator " + self.key.with_suffix(".pub").read_text())
        self.trusted.chmod(0o600)
        self.db = str(self.root / "shared.sqlite3")
        self.config_data = {
            "schema": product_config.SCHEMA,
            "registry_db": self.db,
            "provider": {"cli": "opencode", "id": "test", "model": "opencode/default"},
            "task": {"agent_kind": "opencode"},
            "team_approval": {
                "mode": "signed", "signer": "operator",
                "allowed_signers_file": str(self.trusted),
            },
        }
        self.install()
        self.spec = {
            "schema": "cao.team.v1", "team_id": "alpha",
            "initiative": "releases",
            "roles": {"analyst": {"instruction": "Independent source reviewer",
                                  "model": "opencode/review"}},
            "tasks": [{
                "id": "read", "role": "analyst", "repo": str(self.root / "checkout"),
                "mode": "read", "prompt": "Analyze source, return only evidence",
            }],
            "max_parallel": 1, "max_writers": 0,
        }

    def install(self):
        path = Path(product_config.config_path(self.env))
        path.parent.mkdir(parents=True, exist_ok=True)
        product_config.write_config(self.config_data, path)
        self.config = product_config.validate_config(self.config_data)

    def signature(self, spec=None, *, config=None):
        selected = team.validate_spec(spec or self.spec)
        content = team_approval.proposal(selected, config or self.config)
        path = self.root / ("proposal-" + hashlib.sha256(content).hexdigest()[:16] + ".json")
        path.write_bytes(content)
        result = subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(self.key),
             "-n", "cao-team", str(path)],
            capture_output=True, timeout=12, check=False)
        self.assertEqual(result.returncode, 0)
        return path.with_suffix(".json.sig").read_bytes()

    def init(self, spec=None, signature=None):
        return team.initialize(spec or self.spec, env=self.env,
                               config=self.config, signature=signature)

    def test_signed_operator_admission_and_idempotence(self):
        with self.assertRaises(team.TeamError):
            self.init()
        self.assertEqual(team.list_teams(env=self.env), [])
        signature = self.signature()
        first = self.init(signature=signature)
        self.assertEqual(first["status"], "PAUSED")
        self.assertEqual(first, self.init())
        approval = state.read_state(team.team_path("alpha", self.env))["admission"]
        self.assertEqual(approval["signer"], "operator")
        self.assertEqual(approval["schema"], team_approval.SCHEMA)
        team_approval.verify_saved(team.validate_spec(self.spec), self.config, approval)
        self.assertEqual(team.set_desired("alpha", "running", env=self.env)["desired"],
                         "running")

    def test_exact_roster_task_budget_and_routing_are_bound(self):
        signed = self.signature()
        edits = (
            {"roles": {"analyst": {"instruction": "Changed", "model": "opencode/other"}}},
            {"max_parallel": 2},
            {"team_id": "other"},
            {"tasks": [{**self.spec["tasks"][0], "prompt": "different"}]},
        )
        for update in edits:
            with self.subTest(update=update):
                candidate = {**self.spec, **update}
                with self.assertRaises(team.TeamError):
                    self.init(candidate, signed)
        self.config_data["provider"]["model"] = "opencode/changed"
        self.install()
        with self.assertRaises(team.TeamError):
            self.init(signature=signed)
        self.assertEqual(team.list_teams(env=self.env), [])

    def test_rejects_fake_signer_and_unprotected_public_key_list(self):
        wrong = self.root / "wrong"
        result = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(wrong)],
            capture_output=True, timeout=12, check=False)
        self.assertEqual(result.returncode, 0)
        proposal = self.root / "proposal.json"
        proposal.write_bytes(team_approval.proposal(team.validate_spec(self.spec),
                                                     self.config))
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(wrong),
                        "-n", "cao-team", str(proposal)],
                       capture_output=True, timeout=12, check=True)
        with self.assertRaises(team.TeamError):
            self.init(signature=proposal.with_suffix(".json.sig").read_bytes())
        self.trusted.chmod(0o666)
        with self.assertRaises(team.TeamError):
            self.init(signature=self.signature())
        self.assertEqual(team.list_teams(env=self.env), [])

    def test_successor_requires_paused_terminal_predecessor_and_reason(self):
        self.init(signature=self.signature())
        second = {**self.spec, "team_id": "beta",
                  "supersedes": "alpha",
                  "change_reason": "Reviewed original failure, replacement approved"}
        with self.assertRaises(team.TeamError):
            self.init(second, self.signature(second))
        # A prior UNKNOWN task has an immutable execution identity and may
        # be replaced only after the operator pauses it and Registry shows no
        # live claims. Its historical checkpoint remains intact.
        db = registry.connect(self.db)
        db.close()
        path = Path(team.team_path("alpha", self.env))
        old = state.read_state(path)
        slot = old["slots"]["read"]
        slot["status"] = "unknown"
        slot["error"] = "TEAM_RESULT_UNVERIFIED"
        slot["execution_id"] = team._execution_id("alpha", "read", "work", 0, 0)
        slot["prompt"] = "Original bounded request"
        state.write_state(path, old)
        self.assertEqual(team.status("alpha", env=self.env)["status"], "BLOCKED")
        self.assertEqual(self.init(second, self.signature(second))["status"], "PAUSED")
        self.assertEqual(len(team.list_teams(env=self.env)), 2)
        third = {**self.spec, "team_id": "gamma",
                 "supersedes": "alpha",
                 "change_reason": "Explicit older predecessor is not the latest team"}
        with self.assertRaises(team.TeamError):
            self.init(third, self.signature(third))

    def test_existing_legacy_running_work_drains_but_new_dispatch_holds(self):
        self.config_data["team_approval"] = {"mode": "legacy"}
        self.install()
        self.init()
        team.set_desired("alpha", "running", env=self.env)
        calls = []
        runner = lambda **kw: calls.append(kw) or {"status": "running"}
        self.config_data["team_approval"] = {
            "mode": "signed", "signer": "operator",
            "allowed_signers_file": str(self.trusted),
        }
        self.install()
        result = team.tick(
            "alpha", env=self.env, config=self.config, driver=None,
            execute=runner, writer_snapshot=lambda _: (0, set()),
            head_reader=lambda _: "a" * 40)
        self.assertEqual(calls, [])
        self.assertEqual(result["tasks"]["read"]["status"], "pending")
        with self.assertRaises(team.TeamError):
            team.set_desired("alpha", "paused", env=self.env)
            team.set_desired("alpha", "running", env=self.env)

    def test_signed_route_changed_before_dispatch_blocks_effect(self):
        self.init(signature=self.signature())
        team.set_desired("alpha", "running", env=self.env)
        self.config_data["provider"]["model"] = "opencode/changed"
        self.install()
        calls = []
        with self.assertRaises(team.TeamError):
            team.tick("alpha", env=self.env, config=self.config, driver=None,
                      execute=lambda **k: calls.append(k),
                      writer_snapshot=lambda _: (0, set()),
                      head_reader=lambda _: "a" * 40)
        self.assertEqual(calls, [])

    def test_cli_plan_proposal_and_init_refuse_without_signature(self):
        import unittest.mock as mock
        document = self.root / "team.json"
        document.write_text(json.dumps(self.spec))
        with mock.patch.dict(os.environ, self.env):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(team_cli.main(["plan", "--file", str(document)]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["initiative"], "releases")
            self.assertEqual(result["roles"]["analyst"]["model"], "opencode/review")
            self.assertEqual(result["max_writers"], 0)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(team_cli.main(["proposal", "--file", str(document)]), 0)
            signed = output.getvalue().encode("ascii")
            self.assertEqual(signed, team_approval.proposal(team.validate_spec(self.spec),
                                                             self.config))
            with contextlib.redirect_stdout(output := io.StringIO()):
                self.assertEqual(team_cli.main(["init", "--file", str(document)]), 3)
            denied = json.loads(output.getvalue())
            self.assertEqual(denied["code"], "TEAM_APPROVAL_REQUIRED")
            self.assertEqual(denied["status"], "UNAVAILABLE")
            approval = self.root / "external.sig"
            approval.write_bytes(self.signature())
            with contextlib.redirect_stdout(output := io.StringIO()):
                self.assertEqual(team_cli.main([
                    "init", "--file", str(document), "--signature", str(approval)]), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "PAUSED")



if __name__ == "__main__":
    unittest.main()
