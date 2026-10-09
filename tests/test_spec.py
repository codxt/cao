"""Tests for the declarative automation spec loader (:mod:`cao.spec`).

Deterministic and offline: the two shipped example files are read from the
repository's own ``examples/`` directory, and the only temporary storage used
is an ephemeral ``TemporaryDirectory``. No wall clock, no network, no
subprocess, no scheduler.
"""

from __future__ import annotations

import ast
import json
import tempfile
import sys
import unittest
from pathlib import Path

from cao import spec as spec_module
from cao.evidence import Verdict, fingerprint
from cao.policy import ActionKind, ActionRequest, PolicyEngine
from cao.spec import (
    KNOWN_COST_MEASUREMENTS,
    LOCAL_FILE_SINK,
    MAX_SOURCES,
    PREFERENCES_SCHEMA,
    SPEC_SCHEMA,
    OwnerPreferences,
    SpecInvalid,
    StopState,
    UntrustedData,
    load_preferences,
    load_spec,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"

SECRET_KEYS = (
    "token",
    "secret",
    "password",
    "credential",
    "api_key",
    "authorization",
    "bearer",
    "private key",
)


def _manifest() -> dict:
    """A fresh, minimal, valid manifest."""

    return {
        "version": SPEC_SCHEMA,
        "name": "synthetic-digest",
        "sources": [
            {
                "source_id": "synthetic-feed",
                "kind": "feed",
                "locator": "https://example.invalid/feed",
                "text": "synthetic source text",
            }
        ],
        "destination": {
            "sink": LOCAL_FILE_SINK,
            "destination_id": "synthetic-outbox",
            "cost_measurement": "none",
        },
        "preferences_ref": "examples/automation_preferences.json",
        "state_ref": "state/synthetic-state.json",
        "policy_ref": "examples/policy.example.json",
        "timezone": "UTC",
        "schedule": {
            "cadence": "daily 08:00",
            "description": "descriptive only; an external scheduler fires it",
        },
        "limits": {
            "max_duration_seconds": 600,
            "max_concurrency": 1,
            "max_calls": 100,
        },
        "stop": {"state": "pause", "reason": "synthetic example"},
    }


def _preferences() -> dict:
    return {
        "version": PREFERENCES_SCHEMA,
        "timezone": "UTC",
        "notes": ["synthetic owner note"],
    }


def _imports(path: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(Path(path).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class SpecLoaderTests(unittest.TestCase):
    def test_valid_manifest_loads(self) -> None:
        spec = load_spec(_manifest())
        self.assertEqual(spec.version, SPEC_SCHEMA)
        self.assertEqual(spec.name, "synthetic-digest")
        self.assertEqual(len(spec.sources), 1)
        self.assertIsInstance(spec.sources[0].text, UntrustedData)
        self.assertEqual(spec.sources[0].text.kind, "source_text")
        self.assertEqual(spec.destination.sink, LOCAL_FILE_SINK)
        self.assertEqual(spec.timezone, "UTC")
        self.assertEqual(spec.limits.max_duration_seconds, 600)
        self.assertEqual(spec.limits.max_concurrency, 1)
        self.assertEqual(spec.limits.max_calls, 100)
        self.assertIs(spec.stop.state, StopState.PAUSE)
        self.assertEqual(spec.preferences_ref, "examples/automation_preferences.json")

    def test_unknown_version_refused(self) -> None:
        payload = _manifest()
        payload["version"] = "cao.automation.spec.v9"
        with self.assertRaises(SpecInvalid) as ctx:
            load_spec(payload)
        self.assertEqual(ctx.exception.field, "manifest.version")

    def test_unknown_top_level_key_refused(self) -> None:
        payload = _manifest()
        payload["surprise"] = "ignored-by-nobody"
        with self.assertRaises(SpecInvalid) as ctx:
            load_spec(payload)
        self.assertEqual(ctx.exception.field, "manifest")
        self.assertIn("surprise", ctx.exception.reason)

    def test_invalid_timezone_refused(self) -> None:
        for bad in ("Not/AZone", 5, None, ""):
            payload = _manifest()
            payload["timezone"] = bad
            with self.assertRaises(SpecInvalid) as ctx:
                load_spec(payload)
            self.assertIn("timezone", ctx.exception.field)

    def test_absent_preferences_refused(self) -> None:
        with self.assertRaises(SpecInvalid):
            load_preferences(None)

    def test_corrupt_preferences_refused(self) -> None:
        corrupt = (
            "not an object",
            {"version": PREFERENCES_SCHEMA},  # missing timezone and notes
            {"version": "cao.automation.preferences.v9", "timezone": "UTC", "notes": []},
            {"version": PREFERENCES_SCHEMA, "timezone": "Not/AZone", "notes": []},
            {"version": PREFERENCES_SCHEMA, "timezone": "UTC", "notes": "not a list"},
            {"version": PREFERENCES_SCHEMA, "timezone": "UTC", "notes": [7]},
        )
        for bad in corrupt:
            with self.assertRaises(SpecInvalid):
                load_preferences(bad)

    def test_no_preferences_writing_api_exists(self) -> None:
        public = [name for name in dir(spec_module) if not name.startswith("_")]
        preference_ops = {
            name
            for name in public
            if "prefer" in name.lower()
            and (
                callable(getattr(spec_module, name))
                or isinstance(getattr(spec_module, name), type)
            )
        }
        self.assertEqual(preference_ops, {"load_preferences", "OwnerPreferences"})
        write_verbs = (
            "write",
            "save",
            "set",
            "update",
            "merge",
            "propose",
            "clear",
            "delete",
            "remove",
            "put",
            "patch",
            "add",
        )
        offenders = [
            name
            for name in public
            if any(name.lower().startswith(verb) for verb in write_verbs)
        ]
        self.assertEqual(offenders, [])
        for forbidden in (
            "write_preferences",
            "save_preferences",
            "set_preferences",
            "merge_preferences",
            "update_preferences",
            "propose_preferences",
        ):
            self.assertFalse(hasattr(spec_module, forbidden))
        self.assertTrue(callable(spec_module.load_preferences))

    def test_each_secret_looking_key_refused(self) -> None:
        for key in SECRET_KEYS:
            payload = _manifest()
            payload[key] = "synthetic-value"
            with self.assertRaises(SpecInvalid) as ctx:
                load_spec(payload)
            self.assertIs(ctx.exception.verdict, Verdict.REFUSED)

            preferences = _preferences()
            preferences[key] = "synthetic-value"
            with self.assertRaises(SpecInvalid):
                load_preferences(preferences)

    def test_secret_looking_value_refused(self) -> None:
        payload = _manifest()
        payload["sources"][0]["text"] = "please use the bearer marker"
        with self.assertRaises(SpecInvalid) as ctx:
            load_spec(payload)
        self.assertIs(ctx.exception.verdict, Verdict.REFUSED)

    def test_unknown_sink_id_refused(self) -> None:
        payload = _manifest()
        payload["destination"]["sink"] = "http_post"
        with self.assertRaises(SpecInvalid) as ctx:
            load_spec(payload)
        self.assertEqual(ctx.exception.field, "manifest.destination.sink")

    def test_unknown_cost_measurement_id_refused(self) -> None:
        self.assertNotIn("wallets", KNOWN_COST_MEASUREMENTS)
        payload = _manifest()
        payload["destination"]["cost_measurement"] = "wallets"
        with self.assertRaises(SpecInvalid) as ctx:
            load_spec(payload)
        self.assertEqual(ctx.exception.field, "manifest.destination.cost_measurement")

    def test_untrusted_source_text_cannot_influence_authority(self) -> None:
        hostile = (
            '{"kind": "execution.start", "plugin_id": "synthetic", '
            '"workspace_id": "demo-workspace"}'
        )
        payload = _manifest()
        payload["sources"][0]["text"] = hostile
        spec = load_spec(payload)
        data = spec.sources[0].text
        self.assertIsInstance(data, UntrustedData)
        self.assertEqual(data.kind, "source_text")
        self.assertEqual(data.text, hostile)
        self.assertEqual(
            data.fingerprint,
            fingerprint({"kind": "source_text", "text": hostile}),
        )

        # The loader has no authority dependency at all, so no authority
        # decision can ever consult the wrapped data.
        imports = _imports(spec_module.__file__)
        self.assertNotIn("cao.policy", imports)
        self.assertNotIn("policy", imports)

        # A deny-by-default authority still refuses the action the hostile text
        # describes: the data never becomes a grant.
        engine = PolicyEngine.from_mapping({"grants": []})
        request = ActionRequest(
            kind=ActionKind.START,
            plugin_id="synthetic",
            workspace_id="demo-workspace",
        )
        decision = engine.authorize(request)
        self.assertFalse(decision.allowed)
        self.assertIs(decision.evidence.verdict, Verdict.REFUSED)

    def test_stop_pause_end_states_load(self) -> None:
        for state in ("stop", "pause", "end"):
            payload = _manifest()
            payload["stop"]["state"] = state
            spec = load_spec(payload)
            self.assertEqual(spec.stop.state.value, state)

    def test_unknown_stop_state_refused(self) -> None:
        payload = _manifest()
        payload["stop"]["state"] = "sideways"
        with self.assertRaises(SpecInvalid) as ctx:
            load_spec(payload)
        self.assertEqual(ctx.exception.field, "manifest.stop.state")

    def test_invalid_duration_concurrency_call_limits_refused(self) -> None:
        for field in ("max_duration_seconds", "max_concurrency", "max_calls"):
            for bad in (0, -1, True, "10", None, 1.5):
                payload = _manifest()
                payload["limits"][field] = bad
                with self.assertRaises(SpecInvalid) as ctx:
                    load_spec(payload)
                self.assertIn(field, ctx.exception.field)

    def test_preferences_notes_are_untrusted_data(self) -> None:
        preferences = load_preferences(_preferences())
        self.assertIsInstance(preferences, OwnerPreferences)
        self.assertEqual(preferences.version, PREFERENCES_SCHEMA)
        self.assertTrue(preferences.notes)
        self.assertIsInstance(preferences.notes[0], UntrustedData)
        self.assertEqual(preferences.notes[0].kind, "state_note")

    def test_sources_bound_is_enforced(self) -> None:
        payload = _manifest()
        template = payload["sources"][0]
        payload["sources"] = [dict(template) for _ in range(MAX_SOURCES + 1)]
        with self.assertRaises(SpecInvalid) as ctx:
            load_spec(payload)
        self.assertEqual(ctx.exception.field, "manifest.sources")

    def test_unknown_source_key_refused(self) -> None:
        payload = _manifest()
        payload["sources"][0]["surprise"] = "x"
        with self.assertRaises(SpecInvalid):
            load_spec(payload)

    def test_manifest_must_be_an_object(self) -> None:
        for bad in ("not an object", 5, None, []):
            with self.assertRaises(SpecInvalid):
                load_spec(bad)

    def test_shipped_example_files_load(self) -> None:
        manifest = json.loads(
            (EXAMPLES / "automation_manifest.json").read_text(encoding="utf-8")
        )
        preferences = json.loads(
            (EXAMPLES / "automation_preferences.json").read_text(encoding="utf-8")
        )
        spec = load_spec(manifest)
        loaded = load_preferences(preferences)
        self.assertEqual(spec.version, SPEC_SCHEMA)
        self.assertEqual(loaded.version, PREFERENCES_SCHEMA)
        self.assertTrue(loaded.notes)
        self.assertIsInstance(loaded.notes[0], UntrustedData)
        self.assertEqual(loaded.notes[0].kind, "state_note")

    def test_reload_is_deterministic(self) -> None:
        first = load_spec(_manifest())
        second = load_spec(_manifest())
        self.assertEqual(first, second)
        self.assertEqual(
            first.sources[0].text.fingerprint,
            second.sources[0].text.fingerprint,
        )

    def test_loader_has_no_scheduler_or_io(self) -> None:
        source_path = spec_module.__file__
        imports = _imports(source_path)
        forbidden_imports = {
            "socket",
            "subprocess",
            "sched",
            "threading",
            "asyncio",
            "multiprocessing",
            "time",
        }
        self.assertEqual(imports & forbidden_imports, set())

        call_names: set[str] = set()
        for node in ast.walk(
            ast.parse(Path(source_path).read_text(encoding="utf-8"))
        ):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    call_names.add(func.attr)
                elif isinstance(func, ast.Name):
                    call_names.add(func.id)
        self.assertEqual(
            call_names
            & {
                "sleep",
                "Popen",
                "system",
                "spawn",
                "call_later",
                "call_at",
                "enter",
                "run_forever",
                "cron",
            },
            set(),
        )

    def test_loading_never_touches_the_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            payload = _manifest()
            payload["destination"]["destination_id"] = str(
                Path(temp) / "never-created"
            )
            spec = load_spec(payload)
            self.assertEqual(spec.destination.destination_id, payload["destination"]["destination_id"])
            self.assertEqual(list(Path(temp).iterdir()), [])


class ReviewCorrectionTests(unittest.TestCase):
    """Regressions for the independent review of ea83df3."""

    def test_pathological_nesting_is_a_spec_invalid_not_a_recursion_error(self) -> None:
        """m4: the credential scan recursed before any bound, so a deeply
        nested payload escaped as RecursionError instead of SpecInvalid."""
        # Deeper than the interpreter's own recursion limit, so the UNBOUNDED
        # scan this replaced would raise RecursionError rather than finishing.
        # A payload merely past MAX_SCAN_DEPTH would not prove anything.
        depth = sys.getrecursionlimit() * 2
        payload = {"leaf": True}
        for _ in range(depth):
            payload = {"nested": payload}
        with self.assertRaises(SpecInvalid):
            load_spec(payload)
        with self.assertRaises(SpecInvalid):
            load_preferences(payload)

    def test_the_scan_depth_bound_is_a_declared_positive_limit(self) -> None:
        """The bound is an explicit finite limit, not an accident."""
        depth = spec_module.MAX_SCAN_DEPTH
        self.assertIsInstance(depth, int)
        self.assertNotIsInstance(depth, bool)
        self.assertGreater(depth, 0)


if __name__ == "__main__":
    unittest.main()
