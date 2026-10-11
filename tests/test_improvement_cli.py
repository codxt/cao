"""The improvement CLI must be usable without agents, providers or permissions."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from cao import improvement as m
from cao import improvement_cli

ROOT = Path(__file__).resolve().parents[1]
CASE = json.loads((ROOT / "examples/improvement_case.json").read_text())
AFTER = json.loads((ROOT / "examples/improvement_after.json").read_text())


class CLIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case_file = self.save("case.json", CASE)
        self.after_file = self.save("after.json", AFTER)

    def save(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def run_cmd(self, *args):
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["XDG_STATE_HOME"] = str(self.root / "no-state-should-be-created")
        process = subprocess.run(
            [str(ROOT / "cao"), "improve", *[str(x) for x in args]],
            cwd=self.root, capture_output=True, text=True, env=env,
            timeout=12, check=False)
        return process.returncode, json.loads(process.stdout) if process.stdout.strip().startswith("{") else process.stdout, process.stderr

    def test_assess_works_without_config_or_agent(self):
        code, result, error = self.run_cmd("assess", "--file", self.case_file)
        self.assertEqual(code, 0, error)
        self.assertEqual(result["status"], "INVESTIGATE")
        self.assertEqual(result["issues"][0]["status"], "CORROBORATED")
        self.assertFalse((self.root / "no-state-should-be-created").exists())

    def test_context_projection_deep_and_shallow(self):
        code, result, error = self.run_cmd("context", "--file", self.case_file,
                                           "--resource", "db", "--depth", "0")
        self.assertEqual(code, 0, error)
        self.assertEqual([x["id"] for x in result["nodes"]], ["db"])
        self.assertEqual(result["edges"], [])
        code, result, error = self.run_cmd("context", "--file", self.case_file,
                                           "--resource", "api")
        self.assertEqual(code, 0, error)
        self.assertEqual(len(result["nodes"]), 3)

    def test_propose_draft_exact_scope_never_claim(self):
        cause = {
            "schema": m.CAUSE_SCHEMA, "case_id": CASE["case_id"],
            "issue": "deployment-failure", "repo": "example/platform",
            "hypothesis": "Inspect the bounded implementation mismatch.",
            "write_scope": ["src/api.py"], "evidence_ids": ["factory-observation"],
        }
        file = self.save("cause.json", cause)
        code, result, error = self.run_cmd("propose", "--file", self.case_file,
                                           "--cause", file)
        self.assertEqual(code, 0, error)
        self.assertEqual(result["status"], "PROPOSAL_ONLY")
        self.assertTrue(result["no_execution_authority"])
        self.assertEqual(result["suggested_write_scope"], ["src/api.py"])
        self.assertFalse((self.root / "no-state-should-be-created").exists())

    def test_verify_before_after_references(self):
        code, result, error = self.run_cmd("verify", "--file", self.case_file,
                                           "--after", self.after_file)
        self.assertEqual(code, 0, error)
        self.assertEqual(result["status"], "SYNTHETIC_ONLY")
        self.assertFalse(result["source_attested"])
        self.assertTrue(result["simulated_sources"])
        self.assertFalse(result["accepted_as_canonical_operating_proof"])
        self.assertEqual(result["elapsed_seconds"]["detection_to_deployment"], 2700)

    def test_learning_is_proposal_only(self):
        one, two = copy.deepcopy(CASE), copy.deepcopy(CASE)
        one["data_class"] = two["data_class"] = "source_attested"
        a, b = copy.deepcopy(AFTER), copy.deepcopy(AFTER)
        one["case_id"] = a["case_id"] = "first"
        two["case_id"] = b["case_id"] = "second"
        for stage in ("review", "ci", "deployment"):
            b["intervention"][stage]["head"] = "c"*40
        b["intervention"]["commit_sha"] = "c"*40
        for item in b["measurements"]:
            item["measurement"]["release_sha"] = "c"*40
        proof_objects = ([sig["proof"] for sig in two["signals"]]
                         + [o["before"]["proof"] for o in two["objectives"]]
                         + [x["measurement"]["proof"] for x in b["measurements"]])
        for proof in proof_objects:
            proof["fingerprint"] = "sha256:" + hashlib.sha256(
                ("second|" + proof["fingerprint"]).encode()).hexdigest()
            proof["ref"] = proof["ref"] + ":second"
        two = json.loads(json.dumps(two).replace("2026-01-01T", "2026-01-02T"))
        b = json.loads(json.dumps(b).replace("2026-01-01T", "2026-01-02T"))
        a["case_fingerprint"] = m.evidence.fingerprint(one)
        b["case_fingerprint"] = m.evidence.fingerprint(two)
        def accepted(case, after, name, at):
            report = m.verify(case, after)
            return {
                "case": case, "after": after,
                "acceptance": {
                    "decision": "accepted",
                    "case_fingerprint": report["case_fingerprint"],
                    "after_fingerprint": report["after_fingerprint"],
                    "proof": {
                        "source": "operator",
                        "ref": "urn:operator:acceptance:" + name,
                        "fingerprint": "sha256:" + hashlib.sha256(name.encode()).hexdigest(),
                        "observed_at": at, "verdict": "SUPPORTED",
                    },
                },
            }
        f = self.save("history.json", {"schema": m.HISTORY_SCHEMA,
                                      "entries": [
                                          accepted(one, a, "first", "2026-01-01T10:10:00Z"),
                                          accepted(two, b, "second", "2026-01-02T10:10:00Z"),
                                      ]})
        code, result, error = self.run_cmd("learn", "--history", f)
        self.assertEqual(code, 0, error)
        self.assertEqual(result["status"], "PROPOSALS_ONLY")
        self.assertEqual(len(result["suggestions"]), 1)
        self.assertEqual(result["rules_modified"], 0)

    def test_json_duplicate_key_rejected_without_traceback(self):
        path = self.root / "duplicate.json"
        path.write_text('{"schema":"x","schema":"y"}', encoding="utf-8")
        code, result, error = self.run_cmd("assess", "--file", path)
        self.assertEqual(code, 3)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertNotIn("Traceback", error)

    def test_invalid_and_nonfinite_json_rejected(self):
        for raw in ['{bad', '{"x":NaN}', '[true]']:
            path = self.root / "invalid.json"
            path.write_text(raw, encoding="utf-8")
            code, result, error = self.run_cmd("assess", "--file", path)
            self.assertEqual(code, 3, raw)
            self.assertEqual(result["status"], "UNAVAILABLE")
            self.assertNotIn("Traceback", error)

    def test_symlink_and_missing_input_rejected(self):
        symlink = self.root / "indirect.json"
        symlink.symlink_to(self.case_file)
        for target in (symlink, self.root / "missing.json"):
            code, result, error = self.run_cmd("assess", "--file", target)
            self.assertEqual(code, 3, error)
            self.assertEqual(result["status"], "UNAVAILABLE")

    def test_oversized_input_rejected(self):
        oversized = self.root / "oversized.json"
        oversized.write_bytes(b"a" * (improvement_cli.MAX_INPUT_BYTES + 1))
        code, result, error = self.run_cmd("assess", "--file", oversized)
        self.assertEqual(code, 3)
        self.assertEqual(result["status"], "UNAVAILABLE")

    def test_path_escape_in_proposal_has_no_side_effect(self):
        cause = {
            "schema": m.CAUSE_SCHEMA, "case_id": CASE["case_id"],
            "issue": "deployment-failure", "repo": "example/platform",
            "hypothesis": "Try traversal.",
            "write_scope": ["src/../../secret"], "evidence_ids": ["factory-observation"],
        }
        code, result, error = self.run_cmd("propose", "--file", self.case_file,
                                           "--cause", self.save("cause.json", cause))
        self.assertEqual(code, 3)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertFalse((self.root / "no-state-should-be-created").exists())

    def test_unhashable_objective_issue_returns_bounded_json_error(self):
        invalid = copy.deepcopy(CASE)
        invalid["objectives"][0]["issue"] = []
        code, payload, error = self.run_cmd(
            "assess", "--file", self.save("invalid-issue.json", invalid))
        self.assertEqual(code, 3)
        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertNotIn("Traceback", error)

    def test_unknown_proof_hash_match_is_never_positive_qualification(self):
        case = copy.deepcopy(CASE)
        case["signals"][0]["finding"] = "unknown"
        case["signals"][0]["proof"]["verdict"] = "UNKNOWN"
        case["signals"][0].pop("threshold")
        artifact = self.root / "held-artifact.bin"
        artifact.write_bytes(b"readable but source-status-unknown")
        proof = case["signals"][0]["proof"]
        proof["fingerprint"] = "sha256:" + hashlib.sha256(
            artifact.read_bytes()).hexdigest()
        code, result, error = self.run_cmd(
            "check-proof", "--file", self.save("unknown-case.json", case),
            "--ref", proof["ref"], "--artifact", artifact)
        self.assertEqual(code, 3, error)
        self.assertEqual(result["status"], "UNQUALIFIED")
        self.assertEqual(result["source_verdict"], "UNKNOWN")

    def test_same_cli_work_under_minus_s(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        process = subprocess.run(
            ["python3", "-B", "-S", "-m", "cao.improvement_cli", "assess",
             "--file", str(self.case_file)],
            cwd=self.root, capture_output=True, text=True, env=env,
            check=False, timeout=12)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["status"], "INVESTIGATE")

    def test_artifact_hash_check_matches_real_bytes_not_claimed_label(self):
        case = copy.deepcopy(CASE)
        artifact = self.root / "captured.json"
        artifact.write_bytes(b'{"event":"source-backed"}\n')
        observed = case["signals"][0]["proof"]
        observed["fingerprint"] = "sha256:" + hashlib.sha256(
            artifact.read_bytes()).hexdigest()
        path = self.save("case-hashed.json", case)
        code, report, error = self.run_cmd(
            "check-proof", "--file", path,
            "--ref", observed["ref"], "--artifact", artifact)
        self.assertEqual(code, 0, error)
        self.assertEqual(report["status"], "MATCH")
        self.assertFalse(report["external_provenance_authenticated"])
        artifact.write_bytes(b'{"event":"changed after observation"}\n')
        code, report, error = self.run_cmd(
            "check-proof", "--file", path,
            "--ref", observed["ref"], "--artifact", artifact)
        self.assertEqual(code, 3, error)
        self.assertEqual(report["status"], "MISMATCH")

    def test_proof_checker_cannot_read_unlisted_or_symlink_artifact(self):
        artifact = self.root / "evidence.json"
        artifact.write_bytes(b'{"valid":true}\n')
        link = self.root / "evidence-link.json"
        link.symlink_to(artifact)
        actual_ref = CASE["signals"][0]["proof"]["ref"]
        for ref, path in [(actual_ref, link), ("urn:factory:foreign", artifact)]:
            code, report, error = self.run_cmd(
                "check-proof", "--file", self.case_file,
                "--ref", ref, "--artifact", path)
            self.assertEqual(code, 3, error)
            self.assertEqual(report["status"], "UNAVAILABLE")

    def test_stage_proof_requires_after_document(self):
        ref = AFTER["intervention"]["ci"]["proof"]["ref"]
        f = self.root / "something.bin"
        f.write_bytes(b"unrelated")
        code, report, error = self.run_cmd(
            "check-proof", "--file", self.case_file,
            "--ref", ref, "--artifact", f)
        self.assertEqual(code, 3)
        self.assertEqual(report["status"], "UNAVAILABLE")
        code, report, error = self.run_cmd(
            "check-proof", "--file", self.case_file, "--after", self.after_file,
            "--ref", ref, "--artifact", f)
        self.assertEqual(code, 3)
        self.assertEqual(report["status"], "MISMATCH")

    def test_synthetic_proof_input_rejects_oversized_artifact(self):
        huge = self.root / "huge-blob.bin"
        with huge.open("wb") as output:
            output.truncate(improvement_cli.MAX_ARTIFACT_BYTES + 1)
        code, report, error = self.run_cmd(
            "check-proof", "--file", self.case_file,
            "--ref", CASE["signals"][0]["proof"]["ref"], "--artifact", huge)
        self.assertEqual(code, 3)
        self.assertEqual(report["status"], "UNAVAILABLE")

    def test_cli_help_documents_improvement_surface(self):
        process = subprocess.run(
            [str(ROOT / "cao"), "--help"], cwd=self.root,
            capture_output=True, text=True, check=False, timeout=12)
        self.assertEqual(process.returncode, 0)
        self.assertIn("improve", process.stdout)


if __name__ == "__main__":
    unittest.main()
