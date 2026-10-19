"""CLI contract tests for the standalone disposition command."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from cao import disposition_cli, disposition_control

REPO_ROOT = Path(__file__).resolve().parents[1]


class DispositionCliTests(unittest.TestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = disposition_cli.main(argv)
        return code, out.getvalue()

    def expect_usage_exit(self, argv) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                disposition_cli.main(argv)
        self.assertEqual(raised.exception.code, 2)

    def test_usage_requires_a_verb_and_team_id(self) -> None:
        self.expect_usage_exit([])
        self.expect_usage_exit(["plan"])

    def test_no_caller_supplied_authority_or_path_flags(self) -> None:
        for argv in (
            ["authorize", "--team-id", "squad", "--authority", "true"],
            ["apply", "--team-id", "squad", "--grant", "/srv/grant.json"],
            ["plan", "--team-id", "squad", "--run-id", "other"],
            ["verify", "--team-id", "squad", "--uid", "0"],
        ):
            with self.subTest(argv=argv):
                self.expect_usage_exit(argv)

    def test_success_is_zero_and_json(self) -> None:
        payload = {"schema": "cao.disposition.plan.v1", "status": "PLAN"}
        with mock.patch.object(disposition_control, "plan", return_value=payload):
            code, text = self.run_cli(["plan", "--team-id", "squad"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["status"], "PLAN")

    def test_unavailable_is_three(self) -> None:
        error = disposition_control.DispositionUnavailable("no grant")
        with mock.patch.object(disposition_control, "authorize", side_effect=error):
            code, text = self.run_cli(["authorize", "--team-id", "squad"])
        self.assertEqual(code, disposition_cli.EXIT_UNAVAILABLE)
        body = json.loads(text)
        self.assertEqual(body["status"], "UNAVAILABLE")
        self.assertEqual(body["error"]["code"], "DISPOSITION_UNAVAILABLE")

    def test_refused_is_two(self) -> None:
        error = disposition_control.DispositionRefused("stale grant")
        with mock.patch.object(disposition_control, "apply", side_effect=error):
            code, text = self.run_cli(["apply", "--team-id", "squad"])
        self.assertEqual(code, disposition_cli.EXIT_USAGE)
        body = json.loads(text)
        self.assertEqual(body["status"], "REFUSED")
        self.assertEqual(body["error"]["code"], "DISPOSITION_REFUSED")

    def test_verify_success_can_report_not_ready(self) -> None:
        payload = {"schema": "cao.disposition.verify.v1", "status": "VERIFY",
                   "ready_for_separate_writer_dispatch": False}
        with mock.patch.object(disposition_control, "verify",
                               return_value=payload):
            code, text = self.run_cli(["verify", "--team-id", "squad"])
        self.assertEqual(code, 0)
        self.assertFalse(
            json.loads(text)["ready_for_separate_writer_dispatch"])

    def test_module_entry_point_is_supported(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get(
            "PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-B", "-S", "-m", "cao.disposition_cli", "--help"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertIn(b"cao-disposition", proc.stdout)


if __name__ == "__main__":
    unittest.main()
