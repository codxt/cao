"""CLI surface: help, plugin listing, scaffolded UNAVAILABLE lanes."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from cao import cli
from support import FakeRuntimePlugin


def run_cli(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.main(argv)
    return code, buffer.getvalue()


class HelpTests(unittest.TestCase):
    def test_help_exits_zero_and_lists_groups(self) -> None:
        for argv in (["--help"], ["-h"]):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(argv)
            self.assertEqual(ctx.exception.code, 0)
            text = buffer.getvalue()
            for group in ("work", "worker", "run", "coordinator", "relay", "plugins"):
                self.assertIn(group, text)

    def test_version_exits_zero(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_unknown_command_is_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["bogus"])
        self.assertEqual(ctx.exception.code, 2)


class PluginsListTests(unittest.TestCase):
    def test_empty_registry_json(self) -> None:
        code, output = run_cli(["plugins", "list", "--json"])
        self.assertEqual(code, cli.EXIT_OK)
        payload = json.loads(output)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["plugins"], [])

    def test_json_flag_before_group(self) -> None:
        code, output = run_cli(["--json", "plugins", "list"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(output)["schema"], "cao.cli.plugins.v0")

    def test_human_output_when_empty(self) -> None:
        code, output = run_cli(["plugins", "list"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("no runtime plugins registered", output)

    def test_lists_registered_plugin_descriptor(self) -> None:
        from cao.runtime import PluginRegistry

        registry = PluginRegistry()
        registry.register_instance(FakeRuntimePlugin(plugin_id="alpha"))
        with mock.patch.object(
            cli.PluginRegistry, "discover", return_value=registry
        ):
            code, output = run_cli(["plugins", "list", "--json"])
        self.assertEqual(code, cli.EXIT_OK)
        payload = json.loads(output)
        self.assertEqual(len(payload["plugins"]), 1)
        entry = payload["plugins"][0]
        self.assertEqual(entry["id"], "alpha")
        self.assertTrue(entry["available"])
        self.assertIn("snapshot.capture", entry["descriptor"]["capabilities"])


class ScaffoldedLaneTests(unittest.TestCase):
    def test_every_planned_command_reports_unavailable_nonzero(self) -> None:
        for group, commands in cli.PLANNED_GROUPS.items():
            for command in commands:
                with self.subTest(group=group, command=command):
                    code, output = run_cli(["--json", group, command])
                    self.assertEqual(code, cli.EXIT_UNAVAILABLE)
                    payload = json.loads(output)
                    self.assertEqual(payload["status"], "UNAVAILABLE")
                    self.assertEqual(payload["group"], group)
                    self.assertEqual(payload["command"], command)
                    self.assertEqual(
                        payload["error"]["code"], "LANE_NOT_IMPLEMENTED"
                    )
                    self.assertEqual(payload["error"]["verdict"], "UNAVAILABLE")

    def test_unavailable_never_reports_success(self) -> None:
        code, output = run_cli(["--json", "coordinator", "status"])
        self.assertNotEqual(code, cli.EXIT_OK)
        self.assertNotEqual(json.loads(output)["status"], "OK")

    def test_planned_group_without_command_prints_help(self) -> None:
        code, output = run_cli(["work"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("work", output)


if __name__ == "__main__":
    unittest.main()
