"""CLI surface: plugin listing, read-only lane commands, fail-closed behavior.

Every lane command is exercised against a synthetic state fixture. The tests
assert real output from the accepted modules: they never mock a module into
returning fabricated success.
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cao import cli, coordinator, registry, relay
from support import FakeRuntimePlugin


def run_cli(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.main(argv)
    return code, buffer.getvalue()


def make_registry_ledger(path: Path, *, with_claim: bool = True) -> Path:
    """Build a synthetic registry SQLite ledger with obviously fake ids."""

    conn = registry.connect(str(path))
    try:
        if with_claim:
            registry.propose(
                conn,
                "run-a",
                "work-1",
                repo="/tmp/synthetic-repo",
                paths=["src/alpha"],
                objective="synthetic work one",
            )
            registry.propose(
                conn,
                "run-a",
                "work-2",
                repo="/tmp/synthetic-repo",
                paths=["src/beta"],
                objective="synthetic work two",
            )
            registry.claim(
                conn,
                registry.ClaimSpec(
                    run_id="run-a", work_key="work-1", owner="writer-a", mode="READ"
                ),
            )
    finally:
        conn.close()
    return path


def make_relay_state(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "cao.relay.v0",
                "delivered": ["synthetic-event-0001"],
                "writers": {"synthetic-writer-a": "sha256:00"},
            }
        ),
        encoding="utf-8",
    )
    return path


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

    def test_group_without_command_prints_help(self) -> None:
        code, output = run_cli(["work"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("work", output)


class PluginsListTests(unittest.TestCase):
    def test_empty_registry_json(self) -> None:
        from cao.runtime import PluginRegistry

        # Hermetic: never depend on whether this checkout happens to be
        # installed, which would expose an entry point via ambient metadata.
        with mock.patch.object(
            cli.PluginRegistry, "discover", return_value=PluginRegistry()
        ):
            code, output = run_cli(["plugins", "list", "--json"])
        self.assertEqual(code, cli.EXIT_OK)
        payload = json.loads(output)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["plugins"], [])
        self.assertEqual(payload["schema"], "cao.cli.plugins.v0")

    def test_json_flag_before_group(self) -> None:
        code, output = run_cli(["--json", "plugins", "list"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(output)["schema"], "cao.cli.plugins.v0")

    def test_human_output_when_empty(self) -> None:
        from cao.runtime import PluginRegistry

        with mock.patch.object(
            cli.PluginRegistry, "discover", return_value=PluginRegistry()
        ):
            code, output = run_cli(["plugins", "list"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("no runtime plugins registered", output)

    def test_lists_registered_plugin_descriptor(self) -> None:
        from cao.runtime import PluginRegistry

        registry_instance = PluginRegistry()
        registry_instance.register_instance(FakeRuntimePlugin(plugin_id="alpha"))
        with mock.patch.object(
            cli.PluginRegistry, "discover", return_value=registry_instance
        ):
            code, output = run_cli(["plugins", "list", "--json"])
        self.assertEqual(code, cli.EXIT_OK)
        payload = json.loads(output)
        self.assertEqual(len(payload["plugins"]), 1)
        entry = payload["plugins"][0]
        self.assertEqual(entry["id"], "alpha")
        self.assertTrue(entry["available"])
        self.assertIn("snapshot.capture", entry["descriptor"]["capabilities"])


class LaneFixtureTests(unittest.TestCase):
    """Shared synthetic state for the read-only lane commands."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.ledger = self.dir / "registry.sqlite3"
        self.state = self.dir / "relay.json"

    def build(self, *, with_claim: bool = True) -> None:
        make_registry_ledger(self.ledger, with_claim=with_claim)
        make_relay_state(self.state)


class WorkListTests(LaneFixtureTests):
    def test_reports_registry_works(self) -> None:
        self.build()
        code, output = run_cli(["work", "list", "--ledger", str(self.ledger)])
        self.assertEqual(code, cli.EXIT_OK)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.cli.work.v0")
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            {work["work_key"] for work in payload["works"]}, {"work-1", "work-2"}
        )

    def test_run_filter_is_applied_by_the_registry(self) -> None:
        self.build()
        code, output = run_cli(
            ["work", "list", "--ledger", str(self.ledger), "--run", "run-a"]
        )
        self.assertEqual(code, cli.EXIT_OK)
        payload = json.loads(output)
        self.assertEqual(payload["run_id"], "run-a")
        self.assertEqual(payload["count"], 2)
        empty, empty_output = run_cli(
            ["work", "list", "--ledger", str(self.ledger), "--run", "run-missing"]
        )
        self.assertEqual(empty, cli.EXIT_OK)
        self.assertEqual(json.loads(empty_output)["count"], 0)

    def test_delegates_to_registry(self) -> None:
        self.build()
        with mock.patch.object(registry, "list_works", wraps=registry.list_works) as spy:
            code, _ = run_cli(["work", "list", "--ledger", str(self.ledger)])
        self.assertEqual(code, cli.EXIT_OK)
        spy.assert_called_once()

    def test_missing_ledger_is_unavailable(self) -> None:
        missing = self.dir / "absent.sqlite3"
        code, output = run_cli(["work", "list", "--ledger", str(missing)])
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.cli.unavailable.v0")
        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertEqual(payload["group"], "work")
        self.assertEqual(payload["error"]["code"], "REGISTRY_UNAVAILABLE")
        self.assertNotIn("works", payload)
        self.assertFalse(missing.exists())


class WorkerListTests(LaneFixtureTests):
    def test_reports_open_claims(self) -> None:
        self.build()
        code, output = run_cli(["worker", "list", "--ledger", str(self.ledger)])
        self.assertEqual(code, cli.EXIT_OK)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.cli.worker.v0")
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["count"], 1)
        claim = payload["workers"][0]
        self.assertEqual(claim["owner"], "writer-a")
        self.assertEqual(claim["mode"], "READ")
        self.assertEqual(claim["work_key"], "work-1")

    def test_missing_ledger_is_unavailable(self) -> None:
        missing = self.dir / "absent.sqlite3"
        code, output = run_cli(["worker", "list", "--ledger", str(missing)])
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.cli.unavailable.v0")
        self.assertEqual(payload["group"], "worker")
        self.assertFalse(missing.exists())


class RunListTests(LaneFixtureTests):
    def test_groups_works_by_run(self) -> None:
        self.build()
        code, output = run_cli(["run", "list", "--ledger", str(self.ledger)])
        self.assertEqual(code, cli.EXIT_OK)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.cli.run.v0")
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["count"], 1)
        run_entry = payload["runs"][0]
        self.assertEqual(run_entry["run_id"], "run-a")
        self.assertEqual(run_entry["work_count"], 2)
        self.assertEqual(run_entry["work_keys"], ["work-1", "work-2"])

    def test_missing_ledger_is_unavailable(self) -> None:
        missing = self.dir / "absent.sqlite3"
        code, output = run_cli(["run", "list", "--ledger", str(missing)])
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.cli.unavailable.v0")
        self.assertEqual(payload["group"], "run")
        self.assertFalse(missing.exists())


class CoordinatorStatusTests(LaneFixtureTests):
    def test_unobserved_claims_report_unknown_nonzero(self) -> None:
        self.build()
        code, output = run_cli(["coordinator", "status", "--ledger", str(self.ledger)])
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.cli.coordinator.v0")
        self.assertEqual(payload["status"], "UNKNOWN")
        self.assertEqual(payload["counts"]["UNKNOWN"], 1)
        subject = payload["subjects"][0]
        self.assertEqual(subject["verdict"], "UNKNOWN")
        self.assertEqual(subject["intended_state"], "READ")
        self.assertIsNone(subject["observed_state"])
        self.assertEqual(subject["evidence"]["verdict"], "UNAVAILABLE")
        self.assertEqual(subject["evidence"]["source"], "cli")
        self.assertIn("no runtime observation source", subject["evidence"]["reason"])

    def test_empty_ledger_is_ok(self) -> None:
        self.build(with_claim=False)
        code, output = run_cli(["coordinator", "status", "--ledger", str(self.ledger)])
        self.assertEqual(code, cli.EXIT_OK)
        payload = json.loads(output)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["counts"]["UNKNOWN"], 0)
        self.assertEqual(payload["subjects"], [])

    def test_delegates_to_coordinator(self) -> None:
        self.build()
        with mock.patch.object(
            coordinator, "reconcile", wraps=coordinator.reconcile
        ) as spy:
            code, _ = run_cli(
                ["coordinator", "status", "--ledger", str(self.ledger)]
            )
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        spy.assert_called_once()

    def test_missing_ledger_is_unavailable(self) -> None:
        missing = self.dir / "absent.sqlite3"
        code, output = run_cli(
            ["coordinator", "status", "--ledger", str(missing)]
        )
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.cli.unavailable.v0")
        self.assertEqual(payload["group"], "coordinator")
        self.assertFalse(missing.exists())


class RelayStatusTests(LaneFixtureTests):
    def test_reports_persisted_dedup_state(self) -> None:
        make_relay_state(self.state)
        code, output = run_cli(["relay", "status", "--state", str(self.state)])
        self.assertEqual(code, cli.EXIT_OK)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.cli.relay.v0")
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["delivered_count"], 1)
        self.assertEqual(payload["delivered"], ["synthetic-event-0001"])
        self.assertEqual(payload["writers"], {"synthetic-writer-a": "sha256:00"})

    def test_delegates_to_relay(self) -> None:
        make_relay_state(self.state)
        with mock.patch.object(relay, "load_ledger", wraps=relay.load_ledger) as spy:
            code, _ = run_cli(["relay", "status", "--state", str(self.state)])
        self.assertEqual(code, cli.EXIT_OK)
        spy.assert_called_once()

    def test_missing_state_is_unavailable(self) -> None:
        missing = self.dir / "absent.json"
        code, output = run_cli(["relay", "status", "--state", str(missing)])
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "cao.cli.unavailable.v0")
        self.assertEqual(payload["group"], "relay")
        self.assertEqual(payload["error"]["code"], "RELAY_STATE_NOT_FOUND")
        self.assertFalse(missing.exists())

    def test_malformed_state_is_unavailable(self) -> None:
        self.state.write_text(json.dumps({"schema": "wrong"}), encoding="utf-8")
        code, output = run_cli(["relay", "status", "--state", str(self.state)])
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        self.assertEqual(json.loads(output)["error"]["code"], "RELAY_STATE_INVALID")

    def test_non_object_state_is_unavailable(self) -> None:
        self.state.write_text("[]", encoding="utf-8")
        code, output = run_cli(["relay", "status", "--state", str(self.state)])
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        self.assertEqual(
            json.loads(output)["error"]["code"], "RELAY_STATE_UNREADABLE"
        )


class QueryFailureTests(LaneFixtureTests):
    """A valid ledger whose query fails must fail closed, not traceback.

    The ledger is a real, valid registry; only the query raises, which is
    exactly the corrupted-page/query-error case. The exception is simulated
    in-memory so the regression is deterministic and never swallows the error
    into a success payload.
    """

    _CASES = (
        ("work", "list", "list_works"),
        ("run", "list", "list_works"),
        ("worker", "list", "list_active_claims"),
        ("coordinator", "status", "list_active_claims"),
    )

    def test_query_failure_is_structured_unavailable(self) -> None:
        self.build()

        def boom(*args, **kwargs):
            raise sqlite3.DatabaseError("simulated corrupt page")

        for group, command, function_name in self._CASES:
            with self.subTest(group=group, command=command):
                with mock.patch.object(registry, function_name, side_effect=boom):
                    buffer = io.StringIO()
                    with contextlib.redirect_stdout(buffer):
                        code = cli.main(
                            [group, command, "--ledger", str(self.ledger)]
                        )
                output = buffer.getvalue()
                self.assertEqual(code, cli.EXIT_UNAVAILABLE)
                payload = json.loads(output)
                self.assertEqual(payload["schema"], "cao.cli.unavailable.v0")
                self.assertEqual(payload["status"], "UNAVAILABLE")
                self.assertEqual(payload["group"], group)
                self.assertEqual(payload["command"], command)
                self.assertEqual(payload["error"]["code"], "REGISTRY_UNAVAILABLE")
                self.assertIn("simulated corrupt page", payload["error"]["message"])


class AutomationCommandTests(LaneFixtureTests):
    """The automation group reports what it can prove and refuses the rest."""

    def setUp(self) -> None:
        super().setUp()
        self.repo_root = Path(__file__).resolve().parents[1]
        self.manifest = self.repo_root / "examples" / "automation_manifest.json"
        self.preferences = self.repo_root / "examples" / "automation_preferences.json"

    def _manifest_argv(self, document: dict, name: str = "manifest.json") -> list[str]:
        path = self.dir / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return ["automation", "validate", "--config", str(path)]

    def test_validate_accepts_the_shipped_synthetic_manifest(self) -> None:
        code, output = run_cli(
            [
                "automation",
                "validate",
                "--config",
                str(self.manifest),
                "--preferences",
                str(self.preferences),
            ]
        )
        payload = json.loads(output)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(payload["schema"], "cao.cli.automation.v0")
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["version"], "cao.automation.spec.v0")
        self.assertEqual(payload["preferences"], "valid")
        self.assertEqual(payload["destination"]["sink"], "local_file")

    def test_validate_reports_the_schedule_as_descriptive_only(self) -> None:
        code, output = run_cli(["automation", "validate", "--config", str(self.manifest)])
        payload = json.loads(output)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("external scheduler", payload["schedule"]["executed_by"])

    def test_validate_refuses_an_unknown_version(self) -> None:
        document = json.loads(self.manifest.read_text(encoding="utf-8"))
        document["version"] = "cao.automation.spec.v99"
        code, output = run_cli(self._manifest_argv(document))
        payload = json.loads(output)
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        self.assertEqual(payload["status"], "INVALID")
        self.assertEqual(payload["error"]["field"], "manifest.version")

    def test_validate_refuses_an_unknown_sink(self) -> None:
        document = json.loads(self.manifest.read_text(encoding="utf-8"))
        document["destination"]["sink"] = "http_post"
        code, output = run_cli(self._manifest_argv(document))
        payload = json.loads(output)
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        self.assertEqual(payload["error"]["verdict"], "REFUSED")

    def test_validate_refuses_corrupt_owner_preferences(self) -> None:
        prefs = self.dir / "prefs.json"
        prefs.write_text(
            json.dumps({"version": "cao.automation.preferences.v0"}), encoding="utf-8"
        )
        code, output = run_cli(
            [
                "automation",
                "validate",
                "--config",
                str(self.manifest),
                "--preferences",
                str(prefs),
            ]
        )
        payload = json.loads(output)
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        self.assertEqual(payload["status"], "INVALID")
        # Absent preferences are refused, never defaulted to an empty set.
        self.assertEqual(payload["error"]["field"], "preferences.timezone")

    def test_validate_fails_closed_on_an_unreadable_document(self) -> None:
        code, output = run_cli(
            ["automation", "validate", "--config", str(self.dir / "absent.json")]
        )
        payload = json.loads(output)
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        self.assertEqual(payload["schema"], "cao.cli.unavailable.v0")
        self.assertEqual(payload["error"]["code"], "DOCUMENT_UNREADABLE")

    def test_validate_fails_closed_on_a_non_object_document(self) -> None:
        path = self.dir / "list.json"
        path.write_text("[]", encoding="utf-8")
        code, output = run_cli(["automation", "validate", "--config", str(path)])
        payload = json.loads(output)
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        self.assertEqual(payload["error"]["code"], "DOCUMENT_SCHEMA")

    def test_run_reports_the_missing_capability_and_exits_nonzero(self) -> None:
        code, output = run_cli(
            ["automation", "run", "--config", str(self.manifest), "--dry-run"]
        )
        payload = json.loads(output)
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        self.assertEqual(payload["schema"], "cao.cli.unavailable.v0")
        self.assertEqual(payload["group"], "automation")
        self.assertEqual(payload["command"], "run")
        self.assertEqual(payload["error"]["code"], "NO_SOURCE_ADAPTER")
        self.assertEqual(payload["error"]["verdict"], "UNAVAILABLE")

    def test_status_reports_absent_state_as_unavailable_not_empty(self) -> None:
        code, output = run_cli(
            ["automation", "status", "--bookmarks", str(self.dir / "absent.json")]
        )
        payload = json.loads(output)
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertEqual(payload["state"]["bookmarks"], "absent")

    def test_status_fails_closed_on_corrupt_persisted_state(self) -> None:
        path = self.dir / "bookmarks.json"
        path.write_text("{not json", encoding="utf-8")
        code, output = run_cli(["automation", "status", "--bookmarks", str(path)])
        payload = json.loads(output)
        self.assertEqual(code, cli.EXIT_UNAVAILABLE)
        self.assertEqual(payload["state"]["bookmarks"]["status"], "INVALID")

    def test_status_reads_each_state_through_its_own_loader(self) -> None:
        from cao.brief import BriefHistory, ObservedItem
        from cao.delivery import DeliveryLedger
        from cao.sources import Bookmarks

        paths = {}
        for name, state in (
            ("bookmarks", Bookmarks().as_state()),
            ("delivery", DeliveryLedger().as_state()),
            (
                "history",
                BriefHistory([ObservedItem("s", "i", "sha256:x")]).as_state(),
            ),
        ):
            path = self.dir / f"{name}.json"
            path.write_text(json.dumps(state), encoding="utf-8")
            paths[name] = str(path)

        code, output = run_cli(
            [
                "automation",
                "status",
                "--bookmarks",
                paths["bookmarks"],
                "--delivery",
                paths["delivery"],
                "--history",
                paths["history"],
            ]
        )
        payload = json.loads(output)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["state"]["delivery"]["receipt_count"], 0)
        self.assertEqual(payload["state"]["brief_history"]["observed_count"], 1)

    def test_automation_commands_write_nothing(self) -> None:
        self.build()
        before = {
            path.name: path.read_bytes()
            for path in sorted(self.dir.iterdir())
            if path.is_file()
        }
        for argv in (
            ["automation", "validate", "--config", str(self.manifest)],
            ["automation", "status"],
            ["automation", "run", "--config", str(self.manifest), "--dry-run"],
        ):
            with self.subTest(argv=argv):
                code, output = run_cli(argv)
                self.assertIn(code, (cli.EXIT_OK, cli.EXIT_UNAVAILABLE))
                json.loads(output)
        after = {
            path.name: path.read_bytes()
            for path in sorted(self.dir.iterdir())
            if path.is_file()
        }
        self.assertEqual(after, before)


class ReadOnlyTests(LaneFixtureTests):
    def test_every_command_leaves_state_byte_identical(self) -> None:
        self.build()
        ledger_before = self.ledger.read_bytes()
        state_before = self.state.read_bytes()
        commands = [
            ["work", "list", "--ledger", str(self.ledger)],
            ["worker", "list", "--ledger", str(self.ledger)],
            ["run", "list", "--ledger", str(self.ledger)],
            ["coordinator", "status", "--ledger", str(self.ledger)],
            ["relay", "status", "--state", str(self.state)],
        ]
        for argv in commands:
            with self.subTest(argv=argv):
                code, output = run_cli(argv)
                self.assertIn(code, (cli.EXIT_OK, cli.EXIT_UNAVAILABLE))
                json.loads(output)
        self.assertEqual(self.ledger.read_bytes(), ledger_before)
        self.assertEqual(self.state.read_bytes(), state_before)
        leftovers = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(leftovers, ["registry.sqlite3", "relay.json"])


if __name__ == "__main__":
    unittest.main()
