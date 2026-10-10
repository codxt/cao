"""Focused tests for the product-facing ``./cao`` command line.

The doctor/readiness logic is covered in ``test_bootstrap``; here the concern is
the CLI surface: structured output, exit codes, the honest ``NOT_STARTED``
service answer, fail-closed behavior on corrupt state, and explicit
``UNAVAILABLE`` for commands this build does not own.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cao import product_cli, product_state

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = product_cli.main(argv)
    return code, buffer.getvalue()


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
        }
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            self.addCleanup(os.environ.pop, key, None)


class HelpTests(CliCase):
    def test_help_exits_zero_and_lists_commands(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as ctx:
                product_cli.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        text = buffer.getvalue()
        for command in ("doctor", "status", "run", "start", "stop", "logs"):
            self.assertIn(command, text)


class DoctorCliTests(CliCase):
    def test_absent_config_is_not_ready(self) -> None:
        code, output = run_cli(["doctor"])
        self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.product.doctor.v0")
        self.assertEqual(payload["status"], "NOT_READY")


class StatusCliTests(CliCase):
    def test_absent_state_does_not_invent_a_service(self) -> None:
        code, output = run_cli(["status"])
        self.assertEqual(code, product_cli.EXIT_OK)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.product.status.v0")
        self.assertEqual(payload["status"], "NOT_STARTED")

    def test_reports_a_persisted_record(self) -> None:
        product_state.write_service(
            {
                "schema": product_state.SERVICE_SCHEMA,
                "desired": "running",
                "observed": "running",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        )
        code, output = run_cli(["status"])
        self.assertEqual(code, product_cli.EXIT_OK)
        self.assertEqual(json.loads(output)["status"], "RUNNING")

    def test_corrupt_state_fails_closed(self) -> None:
        path = product_state.service_record_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text("{ not json", encoding="utf-8")
        code, output = run_cli(["status"])
        self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.product.unavailable.v0")
        self.assertEqual(payload["error"]["code"], "SERVICE_STATE_UNREADABLE")


class UnavailableCommandTests(CliCase):
    def test_owned_elsewhere_commands_are_explicitly_unavailable(self) -> None:
        for command in ("run", "start", "stop", "logs"):
            code, output = run_cli([command])
            self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
            payload = json.loads(output)
            self.assertEqual(payload["schema"], "cao.product.unavailable.v0")
            self.assertEqual(payload["status"], "UNAVAILABLE")
            self.assertEqual(payload["command"], command)
            self.assertEqual(payload["error"]["code"], "NOT_IMPLEMENTED")

    def test_run_accepts_task_arguments_before_reporting_unavailable(self) -> None:
        code, output = run_cli(["run", "--repo", "/tmp/project", "Review this"])
        self.assertEqual(code, product_cli.EXIT_UNAVAILABLE)
        self.assertEqual(json.loads(output)["command"], "run")


class WrapperTests(CliCase):
    def test_root_wrapper_dispatches_to_the_product_cli(self) -> None:
        wrapper = REPO_ROOT / "cao"
        self.assertTrue(os.access(wrapper, os.X_OK))
        result = subprocess.run(
            [str(wrapper), "status"],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["status"], "NOT_STARTED")


if __name__ == "__main__":
    unittest.main()
