"""Focused tests for the one discoverable 'send to CAO' request entrypoint.

They cover deterministic routing, the Intake admission and its idempotency and
conflict behavior, the governed-execution route performing no effect, strict
fail-closed input, and the minimal CLI. Everything is synthetic and hermetic:
no runtime is contacted, no agent is started and no Registry claim is made.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cao import intake, product_config, task_router, team


def run_cli(argv):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = task_router.main(list(argv))
    return code, json.loads(buffer.getvalue())


class RouterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "CAO_REGISTRY_DB": str(self.root / "registry.sqlite3"),
        }
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in env:
            self.addCleanup(os.environ.pop, key, None)
        self.raw = {
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "id": "test",
                         "model": "opencode/default"},
            "task": {"agent_kind": "opencode"},
        }
        self.write_config()
        self.config = product_config.validate_config(self.raw)
        self.roles = {"analyst": {"instruction": "Inspect source",
                                  "model": "opencode/read"}}

    def write_config(self, extra=None):
        payload = dict(self.raw)
        if extra:
            payload.update(extra)
        product_config.write_config(payload)
        self.config = product_config.validate_config(payload)

    def manifest(self, team_id="wave", *, initiative=None, supersedes=None):
        spec = {
            "schema": team.SPEC_SCHEMA, "team_id": team_id,
            "roles": self.roles,
            "tasks": [{"id": "inspect", "role": "analyst",
                       "repo": str(self.root / "repo"), "mode": "read",
                       "prompt": "Inspect the repository"}],
        }
        if initiative is not None:
            spec["initiative"] = initiative
        if supersedes is not None:
            spec["initiative"] = initiative or "project"
            spec["supersedes"] = supersedes
            spec["change_reason"] = ("Operator inspected the original issue and "
                                     "approved a successor")
        return spec


class ClassificationTests(RouterCase):
    def test_declared_shapes_route_deterministically(self):
        self.assertEqual(task_router.classify(self.manifest()),
                         task_router.ROUTE_EXECUTION)
        self.assertEqual(
            task_router.classify({"schema": intake.REQUEST_SCHEMA,
                                  "item_id": "a", "body": "b"}),
            task_router.ROUTE_INTAKE)
        self.assertEqual(task_router.classify({"item_id": "a", "body": "b"}),
                         task_router.ROUTE_INTAKE)
        for unroutable in ({"hello": "world"}, {"schema": "other.v1"},
                           "text", [1, 2], None):
            with self.subTest(document=unroutable):
                self.assertIsNone(task_router.classify(unroutable))


class IntakeRouteTests(RouterCase):
    def test_plain_request_is_admitted_once_and_is_idempotent(self):
        document = {"item_id": "req-1", "body": "please fix the flaky test"}
        first = task_router.route(document)
        self.assertEqual(first["route"], task_router.ROUTE_INTAKE)
        self.assertEqual(first["status"], "ADMITTED")
        self.assertFalse(first["dispatch"])
        self.assertEqual(first["authority"], "none")
        second = task_router.route(document)
        self.assertEqual(second["status"], "DUPLICATE")
        self.assertEqual(first["submission"]["submission_id"],
                         second["submission"]["submission_id"])
        self.assertEqual(len(intake.list_submissions()), 1)

    def test_named_intake_schema_is_admitted(self):
        result = task_router.route({"schema": intake.REQUEST_SCHEMA,
                                    "item_id": "req-2", "body": "x"})
        self.assertEqual((result["route"], result["status"]),
                         (task_router.ROUTE_INTAKE, "ADMITTED"))

    def test_same_item_with_different_content_is_refused_and_original_kept(self):
        task_router.route({"item_id": "req-3", "body": "original"})
        conflict = task_router.route({"item_id": "req-3", "body": "changed"})
        self.assertEqual(conflict["status"], task_router.STATUS_REFUSED)
        self.assertEqual(conflict["error"]["code"], intake.IntakeConflict.code)
        self.assertIsNone(conflict["route"])
        stored = intake.list_submissions()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["item_id"], "req-3")

    def test_malformed_request_is_refused_without_admission(self):
        refused = task_router.route({"item_id": "bad id", "body": "x"})
        self.assertEqual(refused["status"], task_router.STATUS_REFUSED)
        self.assertEqual(refused["error"]["code"], intake.IntakeInvalid.code)
        self.assertEqual(intake.list_submissions(), [])

    def test_dry_run_admits_nothing(self):
        result = task_router.route({"item_id": "dry", "body": "x"}, dry_run=True)
        self.assertEqual(result["status"], task_router.STATUS_ROUTED)
        self.assertTrue(result["dry_run"])
        self.assertFalse(os.path.exists(intake.intake_dir()))
        self.assertEqual(intake.list_submissions(), [])


class ExecutionRouteTests(RouterCase):
    def test_manifest_routes_to_execution_without_any_effect(self):
        result = task_router.route(self.manifest(), source="/tmp/wave.json")
        self.assertEqual(result["status"], task_router.STATUS_ROUTED)
        self.assertEqual(result["route"], task_router.ROUTE_EXECUTION)
        self.assertFalse(result["dispatch"])
        self.assertEqual(result["authority"], "none")
        self.assertEqual(result["team_id"], "wave")
        # No team state, no Intake record and no Registry ledger is created.
        self.assertFalse(os.path.exists(team.team_path("wave")))
        self.assertEqual(team.list_teams(), [])
        self.assertEqual(intake.list_submissions(), [])
        self.assertFalse(os.path.exists(str(self.root / "registry.sqlite3")))

    def test_execution_route_never_initializes_starts_or_ticks(self):
        with mock.patch.object(team, "initialize") as init, \
                mock.patch.object(team, "set_desired") as start, \
                mock.patch.object(team, "tick") as tick, \
                mock.patch.object(team, "resume") as resume:
            result = task_router.route(self.manifest())
        self.assertEqual(result["status"], task_router.STATUS_ROUTED)
        init.assert_not_called()
        start.assert_not_called()
        tick.assert_not_called()
        resume.assert_not_called()

    def test_execution_is_idempotent_and_read_only(self):
        first = task_router.route(self.manifest(), source="m.json")
        second = task_router.route(self.manifest(), source="m.json")
        self.assertEqual(first, second)
        self.assertEqual(team.list_teams(), [])

    def test_absent_configuration_refuses_execution_route(self):
        with mock.patch.object(task_router.product_config, "load_config",
                               return_value=None):
            result = task_router.route(self.manifest())
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["error"]["code"], "PRODUCT_CONFIG_UNAVAILABLE")
        self.assertFalse(result["dispatch"])

    def test_invalid_manifest_is_refused(self):
        result = task_router.route({"schema": team.SPEC_SCHEMA, "team_id": "x"})
        self.assertEqual(result["status"], task_router.STATUS_REFUSED)
        self.assertEqual(result["error"]["code"], team.TeamError.code)

    def test_declared_manifest_is_never_admitted_as_intake(self):
        task_router.route(self.manifest())
        self.assertEqual(intake.list_submissions(), [])

    def test_chat_mode_requires_the_operators_own_confirmation(self):
        self.write_config({"team_approval": {"mode": "chat"}})
        source = str(self.root / "wave.json")
        result = task_router.route(self.manifest(initiative="project"),
                                   source=source)
        self.assertEqual(result["status"], task_router.STATUS_ROUTED)
        self.assertEqual(result["authorization"]["approval_mode"], "chat")
        self.assertTrue(result["authorization"]["independent_authorization_required"])
        self.assertIn("--approved", " ".join(result["next_commands"]))
        self.assertFalse(result["dispatch"])
        self.assertFalse(os.path.exists(team.team_path("wave")))

    def test_legacy_mode_names_the_plain_init_command(self):
        result = task_router.route(self.manifest(), source="m.json")
        self.assertFalse(result["authorization"]["independent_authorization_required"])
        self.assertIn("cao team init --file m.json", result["next_commands"])
        self.assertNotIn("cao team init --file m.json --approved",
                         result["next_commands"])

    def test_unallowlisted_route_is_refused(self):
        self.write_config({"team_model_allowlist": ["opencode-go/elsewhere"]})
        result = task_router.route(self.manifest())
        self.assertEqual(result["status"], task_router.STATUS_REFUSED)
        self.assertEqual(result["error"]["code"], "TEAM_MODEL_NOT_ALLOWED")
        self.assertFalse(os.path.exists(team.team_path("wave")))


class RequestCliTests(RouterCase):
    def test_plain_text_request_admits_then_reports_duplicate(self):
        code, first = run_cli(["--item-id", "cli-1", "--text", "fix it"])
        self.assertEqual(code, task_router.EXIT_OK)
        self.assertEqual(first["schema"], task_router.SCHEMA)
        self.assertEqual(first["route"], task_router.ROUTE_INTAKE)
        self.assertEqual(first["status"], "ADMITTED")
        code, second = run_cli(["--item-id", "cli-1", "--text", "fix it"])
        self.assertEqual(code, task_router.EXIT_OK)
        self.assertEqual(second["status"], "DUPLICATE")

    def test_team_manifest_file_routes_to_execution(self):
        path = self.root / "wave.json"
        path.write_text(json.dumps(self.manifest()), encoding="utf-8")
        code, payload = run_cli(["--file", str(path)])
        self.assertEqual(code, task_router.EXIT_OK)
        self.assertEqual(payload["route"], task_router.ROUTE_EXECUTION)
        self.assertFalse(payload["dispatch"])
        self.assertFalse(os.path.exists(team.team_path("wave")))

    def test_unknown_document_file_is_refused(self):
        path = self.root / "unknown.json"
        path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
        code, payload = run_cli(["--file", str(path)])
        self.assertEqual(code, task_router.EXIT_UNAVAILABLE)
        self.assertEqual(payload["status"], task_router.STATUS_REFUSED)
        self.assertEqual(payload["error"]["code"], "UNKNOWN_REQUEST")

    def test_duplicate_json_keys_are_refused(self):
        path = self.root / "dup.json"
        path.write_text('{"item_id": "a", "item_id": "b", "body": "x"}',
                        encoding="utf-8")
        code, payload = run_cli(["--file", str(path)])
        self.assertEqual(code, task_router.EXIT_UNAVAILABLE)
        self.assertEqual(payload["error"]["code"], "REQUEST_UNREADABLE")
        self.assertEqual(intake.list_submissions(), [])

    def test_absent_file_and_conflicting_flags_are_usage_failures(self):
        code, payload = run_cli(["--file", str(self.root / "missing.json")])
        self.assertEqual(code, task_router.EXIT_UNAVAILABLE)
        self.assertEqual(payload["error"]["code"], "REQUEST_UNREADABLE")
        code, payload = run_cli(["--file", "x.json", "--text", "y"])
        self.assertEqual(code, task_router.EXIT_UNAVAILABLE)
        self.assertEqual(payload["error"]["code"], "USAGE")
        code, payload = run_cli([])
        self.assertEqual(code, task_router.EXIT_UNAVAILABLE)
        self.assertEqual(payload["error"]["code"], "USAGE")

    def test_cli_dry_run_admits_nothing(self):
        code, payload = run_cli(
            ["--item-id", "dry-cli", "--text", "x", "--dry-run"])
        self.assertEqual(code, task_router.EXIT_OK)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(intake.list_submissions(), [])

    def test_json_flag_is_accepted(self):
        code, payload = run_cli(
            ["--json", "--item-id", "cli-json", "--text", "x"])
        self.assertEqual(code, task_router.EXIT_OK)
        self.assertEqual(payload["status"], "ADMITTED")


if __name__ == "__main__":
    unittest.main()
