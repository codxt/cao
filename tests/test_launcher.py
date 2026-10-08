"""Tests for the gated launcher.

The gate order is the point: policy authorization and capability support are
checked before anything is spawned. The only shipped runtime advertises
discovery/snapshot/inspect and not start, so a real start attempt must end in
explicit ``UNAVAILABLE`` with ``value=None`` and zero spawns.
"""

from __future__ import annotations

import ast
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cao import launcher as launcher_module
from cao.contracts import Capability, ExecutionRef, Placement, StartRequest, TargetRef
from cao.evidence import EvidenceSource, Verdict, build_evidence, fingerprint
from cao.launcher import Launcher, LauncherError
from cao.policy import ActionKind, ActionRequest, Grant, PolicyDecision, PolicyEngine

from support import FakeRuntimePlugin

FIXED = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

# The capabilities the only shipped runtime actually advertises. Start is
# deliberately absent, so this is the shape a real launch attempt meets.
SHIPPED_CAPABILITIES = frozenset(
    {Capability.DISCOVERY, Capability.SNAPSHOT, Capability.INSPECT}
)
ALL_CAPABILITIES = frozenset(Capability)

PLUGIN_ID = "runtime-a"
WORKSPACE_ID = "ws-1"


def request() -> StartRequest:
    return StartRequest(workspace_id=WORKSPACE_ID, argv=("worker", "--once"))


def execution_ref(
    plugin_id: str = PLUGIN_ID, workspace_id: str = WORKSPACE_ID
) -> ExecutionRef:
    return ExecutionRef(
        runtime_id=plugin_id, workspace_id=workspace_id, execution_id="exec-1"
    )


def decision(
    *,
    allowed: bool = True,
    plugin_id: str = PLUGIN_ID,
    workspace_id: str = WORKSPACE_ID,
):
    grants = [Grant(ActionKind.START, plugin_id, workspace_id)] if allowed else []
    engine = PolicyEngine(grants)
    return engine.authorize(ActionRequest(ActionKind.START, plugin_id, workspace_id))


def supported_policy_evidence() -> object:
    return build_evidence(
        EvidenceSource.POLICY,
        Verdict.SUPPORTED,
        capture_time="2026-01-01T00:00:00Z",
        fingerprint_value="sha256:" + "3" * 64,
    )


class RecordingSpawner:
    """An injected spawner double that records calls and returns/raises."""

    def __init__(self, result: object) -> None:
        self._result = result
        self.calls: list[StartRequest] = []

    def __call__(self, start: StartRequest) -> object:
        self.calls.append(start)
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


class HappyPathTests(unittest.TestCase):
    def test_authorized_supported_start_spawns_exactly_once(self) -> None:
        ref = execution_ref()
        spawner = RecordingSpawner(ref)
        result = Launcher(
            plugin_id=PLUGIN_ID,
            capabilities=ALL_CAPABILITIES,
            spawner=spawner,
        ).launch(request(), decision())
        self.assertTrue(result.ok)
        self.assertIs(result.value, ref)
        self.assertIs(result.capability, Capability.START)
        self.assertIs(result.evidence.verdict, Verdict.SUPPORTED)
        self.assertEqual(len(spawner.calls), 1)

    def test_single_attempt_is_bounded(self) -> None:
        spawner = RecordingSpawner(execution_ref())
        Launcher(
            plugin_id=PLUGIN_ID, capabilities=ALL_CAPABILITIES, spawner=spawner
        ).launch(request(), decision())
        # No retry ladder and no queue: exactly one attempt.
        self.assertEqual(len(spawner.calls), 1)


class GateOrderTests(unittest.TestCase):
    def test_policy_refusal_never_spawns(self) -> None:
        spawner = RecordingSpawner(execution_ref())
        result = Launcher(
            plugin_id=PLUGIN_ID,
            capabilities=ALL_CAPABILITIES,
            spawner=spawner,
        ).launch(request(), decision(allowed=False))
        self.assertEqual(spawner.calls, [])
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertIs(result.evidence.verdict, Verdict.UNAVAILABLE)
        self.assertIn("REFUSED", result.evidence.reason or "")

    def test_unsupported_capability_never_spawns(self) -> None:
        spawner = RecordingSpawner(execution_ref())
        result = Launcher(
            plugin_id=PLUGIN_ID,
            capabilities=SHIPPED_CAPABILITIES,
            spawner=spawner,
        ).launch(request(), decision())
        self.assertEqual(spawner.calls, [])
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertIs(result.evidence.verdict, Verdict.UNAVAILABLE)

    def test_refusal_precedes_capability_check(self) -> None:
        spawner = RecordingSpawner(execution_ref())
        result = Launcher(
            plugin_id=PLUGIN_ID,
            capabilities=SHIPPED_CAPABILITIES,
            spawner=spawner,
        ).launch(request(), decision(allowed=False))
        self.assertEqual(spawner.calls, [])
        self.assertIs(result.evidence.source, EvidenceSource.POLICY)
        self.assertIs(result.evidence.verdict, Verdict.UNAVAILABLE)

    def test_real_registered_shape_is_unavailable_with_zero_spawns(self) -> None:
        # The shipped runtime advertises no start capability. Its start method
        # must never be invoked and no process may be spawned.
        plugin = FakeRuntimePlugin(
            plugin_id=PLUGIN_ID, capabilities=SHIPPED_CAPABILITIES
        )
        spawner = RecordingSpawner(execution_ref())
        result = Launcher(
            plugin_id=plugin.plugin_id,
            capabilities=plugin.capabilities(),
            spawner=spawner,
        ).launch(request(), decision())
        self.assertEqual(spawner.calls, [])
        self.assertEqual(plugin.calls, [])
        self.assertIs(result.evidence.verdict, Verdict.UNAVAILABLE)
        self.assertIsNone(result.value)
        self.assertFalse(result.ok)


class ExactAuthorityTests(unittest.TestCase):
    """An allowed decision must authorize THIS exact action and be consistent.

    ``allowed`` alone is not authority: a grant for another action, plugin or
    workspace, or a decision whose evidence contradicts its own flag, must
    refuse before the spawner is reached. The zero-spawn assertion is the
    point: a downstream reference mismatch could otherwise mask a spawn that
    already happened.
    """

    def _refuse_without_spawn(self, decision_obj: PolicyDecision) -> None:
        spawner = RecordingSpawner(execution_ref())
        result = Launcher(
            plugin_id=PLUGIN_ID,
            capabilities=ALL_CAPABILITIES,
            spawner=spawner,
        ).launch(request(), decision_obj)
        self.assertEqual(spawner.calls, [])
        self.assertIsNone(result.value)
        self.assertIn(
            result.evidence.verdict, (Verdict.UNAVAILABLE, Verdict.INVALID)
        )

    def test_wrong_action_grant_never_spawns(self) -> None:
        self._refuse_without_spawn(
            PolicyDecision(
                allowed=True,
                evidence=supported_policy_evidence(),
                matched=Grant(ActionKind.CLOSE, PLUGIN_ID, WORKSPACE_ID),
            )
        )

    def test_wrong_plugin_grant_never_spawns(self) -> None:
        self._refuse_without_spawn(
            PolicyDecision(
                allowed=True,
                evidence=supported_policy_evidence(),
                matched=Grant(ActionKind.START, "runtime-other", WORKSPACE_ID),
            )
        )

    def test_wrong_workspace_grant_never_spawns(self) -> None:
        self._refuse_without_spawn(
            PolicyDecision(
                allowed=True,
                evidence=supported_policy_evidence(),
                matched=Grant(ActionKind.START, PLUGIN_ID, "ws-other"),
            )
        )

    def test_contradictory_decision_never_spawns(self) -> None:
        declined = build_evidence(
            EvidenceSource.POLICY,
            Verdict.REFUSED,
            capture_time="2026-01-01T00:00:00Z",
            fingerprint_value="sha256:" + "3" * 64,
        )
        self._refuse_without_spawn(
            PolicyDecision(allowed=True, evidence=declined, matched=None)
        )

    def test_allowed_with_no_grant_never_spawns(self) -> None:
        self._refuse_without_spawn(
            PolicyDecision(
                allowed=True,
                evidence=supported_policy_evidence(),
                matched=None,
            )
        )

    def test_non_grant_matched_never_spawns(self) -> None:
        self._refuse_without_spawn(
            PolicyDecision(
                allowed=True,
                evidence=supported_policy_evidence(),
                matched="grant",  # type: ignore[arg-type]
            )
        )


class FailClosedTests(unittest.TestCase):
    def _launcher(self, spawner: RecordingSpawner) -> Launcher:
        return Launcher(
            plugin_id=PLUGIN_ID,
            capabilities=ALL_CAPABILITIES,
            spawner=spawner,
        )

    def test_invalid_request_is_invalid_without_spawn(self) -> None:
        spawner = RecordingSpawner(execution_ref())
        result = self._launcher(spawner).launch("start it", decision())  # type: ignore[arg-type]
        self.assertEqual(spawner.calls, [])
        self.assertIs(result.evidence.verdict, Verdict.INVALID)
        self.assertIsNone(result.value)

    def test_invalid_decision_is_invalid_without_spawn(self) -> None:
        spawner = RecordingSpawner(execution_ref())
        result = self._launcher(spawner).launch(request(), None)  # type: ignore[arg-type]
        self.assertEqual(spawner.calls, [])
        self.assertIs(result.evidence.verdict, Verdict.INVALID)
        self.assertIsNone(result.value)

    def test_spawner_returning_none_is_unknown(self) -> None:
        spawner = RecordingSpawner(None)
        result = self._launcher(spawner).launch(request(), decision())
        self.assertEqual(len(spawner.calls), 1)
        self.assertIs(result.evidence.verdict, Verdict.UNKNOWN)
        self.assertIsNone(result.value)

    def test_spawner_raising_is_contained_and_unknown(self) -> None:
        spawner = RecordingSpawner(RuntimeError("boom"))
        result = self._launcher(spawner).launch(request(), decision())
        self.assertEqual(len(spawner.calls), 1)
        self.assertIs(result.evidence.verdict, Verdict.UNKNOWN)
        self.assertIsNone(result.value)

    def test_spawner_wrong_workspace_is_invalid(self) -> None:
        spawner = RecordingSpawner(execution_ref(workspace_id="ws-other"))
        result = self._launcher(spawner).launch(request(), decision())
        self.assertIs(result.evidence.verdict, Verdict.INVALID)
        self.assertIsNone(result.value)

    def test_spawner_wrong_runtime_is_invalid(self) -> None:
        spawner = RecordingSpawner(execution_ref(plugin_id="runtime-b"))
        result = self._launcher(spawner).launch(request(), decision())
        self.assertIs(result.evidence.verdict, Verdict.INVALID)
        self.assertIsNone(result.value)

    def test_spawner_wrong_type_is_invalid(self) -> None:
        spawner = RecordingSpawner(object())
        result = self._launcher(spawner).launch(request(), decision())
        self.assertIs(result.evidence.verdict, Verdict.INVALID)
        self.assertIsNone(result.value)


class PlacementGuardTests(unittest.TestCase):
    """DS-J: bind a start request's placement before the spawner.

    Every case runs with an ALLOWED workspace-scoped START grant and the START
    capability advertised, so the only refusal left is the new
    placement/identity check. The spawn count is asserted directly: a guard
    that ran only after the spawn would still have spawned once.
    """

    def _launch(self, start: StartRequest):
        spawner = RecordingSpawner(execution_ref())
        result = Launcher(
            plugin_id=PLUGIN_ID,
            capabilities=ALL_CAPABILITIES,
            spawner=spawner,
        ).launch(start, decision())
        return result, spawner

    def _assert_refused_before_spawn(self, start: StartRequest) -> None:
        result, spawner = self._launch(start)
        self.assertEqual(spawner.calls, [])
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertIs(result.evidence.verdict, Verdict.INVALID)

    def test_existing_without_target_is_refused_before_spawn(self) -> None:
        self._assert_refused_before_spawn(
            StartRequest(
                workspace_id=WORKSPACE_ID,
                argv=("worker",),
                placement=Placement.EXISTING,
            )
        )

    def test_provision_with_target_is_refused_before_spawn(self) -> None:
        self._assert_refused_before_spawn(
            StartRequest(
                workspace_id=WORKSPACE_ID,
                argv=("worker",),
                placement=Placement.PROVISION,
                target=TargetRef(PLUGIN_ID, WORKSPACE_ID, "target-1"),
            )
        )

    def test_target_in_another_workspace_is_refused_before_spawn(self) -> None:
        self._assert_refused_before_spawn(
            StartRequest(
                workspace_id=WORKSPACE_ID,
                argv=("worker",),
                placement=Placement.EXISTING,
                target=TargetRef(PLUGIN_ID, "ws-other", "target-1"),
            )
        )

    def test_target_for_another_runtime_is_refused_before_spawn(self) -> None:
        self._assert_refused_before_spawn(
            StartRequest(
                workspace_id=WORKSPACE_ID,
                argv=("worker",),
                placement=Placement.EXISTING,
                target=TargetRef("runtime-other", WORKSPACE_ID, "target-1"),
            )
        )

    def test_non_target_reference_is_refused_without_raising(self) -> None:
        # A plain string must be refused as malformed input, not raise an
        # unhandled AttributeError while its identity is read.
        self._assert_refused_before_spawn(
            StartRequest(
                workspace_id=WORKSPACE_ID,
                argv=("worker",),
                placement=Placement.EXISTING,
                target="ws-1:p1",  # type: ignore[arg-type]
            )
        )

    def test_unknown_placement_raw_value_not_leaked_in_reason(self) -> None:
        # A caller-supplied placement is opaque and may be sensitive. The
        # public reason carries only the fixed rule identifier, never the raw
        # value that the exception's detail embeds.
        secret = "synthetic-secret-placement-42"
        spawner = RecordingSpawner(execution_ref())
        result = Launcher(
            plugin_id=PLUGIN_ID,
            capabilities=ALL_CAPABILITIES,
            spawner=spawner,
        ).launch(
            StartRequest(
                workspace_id=WORKSPACE_ID,
                argv=("worker",),
                placement=secret,  # type: ignore[arg-type]
            ),
            decision(),
        )
        self.assertEqual(spawner.calls, [])
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertIs(result.evidence.source, EvidenceSource.RUNTIME)
        self.assertIs(result.evidence.verdict, Verdict.INVALID)
        reason = result.evidence.reason or ""
        self.assertNotIn(secret, reason)
        self.assertIn("unknown_placement", reason)

    def test_refusal_precedes_spawner_under_a_valid_gate(self) -> None:
        # Allowed policy and an advertised capability: only the placement
        # guard can stop the spawn, and the spawn count proves it did so
        # before the spawner was reached.
        result, spawner = self._launch(
            StartRequest(
                workspace_id=WORKSPACE_ID,
                argv=("worker",),
                placement=Placement.PROVISION,
                target=TargetRef(PLUGIN_ID, WORKSPACE_ID, "target-1"),
            )
        )
        self.assertEqual(spawner.calls, [])
        self.assertIs(result.evidence.verdict, Verdict.INVALID)

    def test_unspecified_placement_still_spawns_once(self) -> None:
        result, spawner = self._launch(request())
        self.assertTrue(result.ok)
        self.assertEqual(len(spawner.calls), 1)
        self.assertEqual(result.value.runtime_id, PLUGIN_ID)
        self.assertEqual(result.value.workspace_id, WORKSPACE_ID)

    def test_bound_existing_target_still_spawns_once(self) -> None:
        ref = execution_ref()
        spawner = RecordingSpawner(ref)
        result = Launcher(
            plugin_id=PLUGIN_ID,
            capabilities=ALL_CAPABILITIES,
            spawner=spawner,
        ).launch(
            StartRequest(
                workspace_id=WORKSPACE_ID,
                argv=("worker",),
                placement=Placement.EXISTING,
                target=TargetRef(PLUGIN_ID, WORKSPACE_ID, "target-1"),
            ),
            decision(),
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(spawner.calls), 1)
        self.assertIs(result.value, ref)


class EvidenceTests(unittest.TestCase):
    def test_unavailable_evidence_is_qualified(self) -> None:
        result = Launcher(
            plugin_id=PLUGIN_ID,
            capabilities=SHIPPED_CAPABILITIES,
            spawner=RecordingSpawner(execution_ref()),
            clock=lambda: FIXED,
        ).launch(request(), decision())
        evidence = result.evidence
        self.assertTrue(evidence.reason)
        self.assertEqual(evidence.capture_time, "2026-01-01T00:00:00Z")
        self.assertIs(result.value, None)

    def test_supported_evidence_carries_a_fingerprint(self) -> None:
        ref = execution_ref()
        result = Launcher(
            plugin_id=PLUGIN_ID,
            capabilities=ALL_CAPABILITIES,
            spawner=RecordingSpawner(ref),
            clock=lambda: FIXED,
        ).launch(request(), decision())
        self.assertEqual(result.evidence.fingerprint, fingerprint(ref))
        self.assertEqual(result.evidence.capture_time, "2026-01-01T00:00:00Z")


class ConstructionTests(unittest.TestCase):
    def test_empty_plugin_id_raises(self) -> None:
        with self.assertRaises(LauncherError):
            Launcher(plugin_id="", capabilities=ALL_CAPABILITIES, spawner=lambda r: r)

    def test_non_callable_spawner_raises(self) -> None:
        with self.assertRaises(LauncherError):
            Launcher(plugin_id=PLUGIN_ID, capabilities=ALL_CAPABILITIES, spawner=None)  # type: ignore[arg-type]

    def test_bad_capabilities_raise(self) -> None:
        with self.assertRaises(LauncherError):
            Launcher(
                plugin_id=PLUGIN_ID,
                capabilities=("not-a-capability",),
                spawner=lambda r: r,
            )


class ModuleHygieneTests(unittest.TestCase):
    def _imports(self) -> set[str]:
        tree = ast.parse(Path(launcher_module.__file__).read_text("utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    names.add("cao." + (node.module or ""))
                elif node.module:
                    names.add(node.module)
        return names

    def test_imports_are_stdlib_or_cao_only(self) -> None:
        for module in self._imports():
            top = module.split(".")[0]
            if top == "cao":
                continue
            self.assertIn(top, sys.stdlib_module_names, module)

    def test_does_not_import_concrete_plugins(self) -> None:
        for module in self._imports():
            self.assertFalse(
                module == "plugins"
                or module == "cao.plugins"
                or module.startswith(("plugins.", "cao.plugins.")),
                module,
            )

    def test_does_not_import_subprocess(self) -> None:
        self.assertNotIn("subprocess", self._imports())


if __name__ == "__main__":
    unittest.main()
