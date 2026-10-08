"""Frozen runtime contract: discovery, snapshot, inspect, observe, start,
prompt, close, explicit selection, and no silent fallback."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace

from cao.contracts import (
    Capability,
    InvalidStartRequest,
    Placement,
    StartRequest,
    UnsupportedCapability,
    validate_placement,
)
from cao.evidence import EvidenceSource, Verdict
from cao.runtime import (
    PluginError,
    PluginNotFound,
    PluginRegistry,
    RuntimeClient,
)
from support import (
    FIXED_CAPTURE_TIME,
    FakeEntryPoint,
    FakeRuntimePlugin,
    execution_ref,
    session_ref,
    target_ref,
)


class DiscoveryTests(unittest.TestCase):
    def test_describe_reports_declared_capabilities(self) -> None:
        plugin = FakeRuntimePlugin(capabilities=frozenset({Capability.SNAPSHOT}))
        descriptor = plugin.describe()
        self.assertEqual(descriptor.id, "fake")
        self.assertEqual(descriptor.name, "Fake Runtime")
        self.assertEqual(descriptor.version, "1.2.3")
        self.assertEqual(descriptor.capabilities, frozenset({Capability.SNAPSHOT}))

    def test_client_discover_is_supported(self) -> None:
        client = RuntimeClient(FakeRuntimePlugin())
        result = client.discover()
        self.assertTrue(result.ok)
        self.assertEqual(result.evidence.source, EvidenceSource.PLUGIN)
        self.assertEqual(result.evidence.verdict, Verdict.SUPPORTED)
        self.assertIsNotNone(result.evidence.capture_time)
        self.assertIsNotNone(result.evidence.fingerprint)
        self.assertEqual(result.value.id, "fake")


class SnapshotTests(unittest.TestCase):
    def test_snapshot_evidence_preserves_capture_metadata(self) -> None:
        plugin = FakeRuntimePlugin()
        snapshot = plugin.snapshot("ws-1")
        result = RuntimeClient(plugin).snapshot("ws-1")
        self.assertTrue(result.ok)
        self.assertEqual(result.evidence.capture_time, FIXED_CAPTURE_TIME)
        self.assertEqual(result.evidence.fingerprint, snapshot.fingerprint)
        self.assertEqual(result.evidence.coverage, 0.75)
        self.assertEqual(result.value.workspace_id, "ws-1")

    def test_snapshot_unavailable_when_not_declared(self) -> None:
        plugin = FakeRuntimePlugin(capabilities=frozenset({Capability.DISCOVERY}))
        result = RuntimeClient(plugin).snapshot("ws-1")
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertEqual(result.evidence.verdict, Verdict.UNAVAILABLE)
        self.assertIn(Capability.SNAPSHOT.value, result.evidence.reason or "")
        self.assertEqual(plugin.calls, [])

    def test_snapshot_rejects_invalid_capture_time(self) -> None:
        for bad in (
            "not-a-time",
            "2026-01-01T00:00:00",  # offsetless naive timestamp
            "2026-01-01 00:00:00Z",  # space, not RFC 3339 'T'
            "2026-13-40T00:00:00Z",  # impossible calendar values
            "",
        ):
            with self.subTest(captured_at=bad):

                class BadCaptureTime(FakeRuntimePlugin):
                    def snapshot(self, workspace_id: str):  # type: ignore[override]
                        return replace(
                            super().snapshot(workspace_id), captured_at=bad
                        )

                result = RuntimeClient(BadCaptureTime()).snapshot("ws-1")
                self.assertFalse(result.ok)
                self.assertIsNone(result.value)
                self.assertEqual(result.evidence.verdict, Verdict.INVALID)

    def test_snapshot_preserves_valid_capture_time_forms(self) -> None:
        for good in (
            "2026-01-01T00:00:00Z",
            "2026-01-01T05:30:00+05:30",
            "2026-01-01T00:00:00.123456Z",
        ):
            with self.subTest(captured_at=good):

                class GoodCaptureTime(FakeRuntimePlugin):
                    def snapshot(self, workspace_id: str):  # type: ignore[override]
                        return replace(
                            super().snapshot(workspace_id), captured_at=good
                        )

                result = RuntimeClient(GoodCaptureTime()).snapshot("ws-1")
                self.assertTrue(result.ok)
                self.assertEqual(result.evidence.capture_time, good)
                self.assertEqual(result.value.captured_at, good)


class PointCapabilityTests(unittest.TestCase):
    def test_inspect_observe_start_prompt_close(self) -> None:
        client = RuntimeClient(FakeRuntimePlugin())
        target = target_ref()
        inspect = client.inspect(target)
        self.assertTrue(inspect.ok)
        self.assertEqual(inspect.value.target_id, "target-1")

        observe = client.observe(execution_ref())
        self.assertTrue(observe.ok)
        self.assertEqual(observe.value.execution_id, "exec-1")

        start = client.start(StartRequest(workspace_id="ws-1", argv=("run",)))
        self.assertTrue(start.ok)
        self.assertEqual(start.value.execution_id, "exec-1")

        prompt = client.prompt(session_ref(), "hello")
        self.assertTrue(prompt.ok)
        self.assertTrue(prompt.value.accepted)

        close = client.close(session_ref())
        self.assertTrue(close.ok)
        self.assertTrue(close.value.closed)

    def test_close_is_optional_and_never_faked(self) -> None:
        capabilities = frozenset(
            {
                Capability.DISCOVERY,
                Capability.START,
                Capability.PROMPT,
                Capability.OBSERVE,
            }
        )
        plugin = FakeRuntimePlugin(capabilities=capabilities)
        result = RuntimeClient(plugin).close(session_ref())
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertEqual(result.evidence.verdict, Verdict.UNAVAILABLE)
        # The unsupported method was not called.
        self.assertEqual([call for call, _ in plugin.calls], [])

    def test_plugin_returning_none_is_unknown_not_success(self) -> None:
        plugin = FakeRuntimePlugin(return_none_for=Capability.START)
        result = RuntimeClient(plugin).start(
            StartRequest(workspace_id="ws-1", argv=("run",))
        )
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertEqual(result.evidence.verdict, Verdict.UNKNOWN)
        self.assertIn("returned no value", result.evidence.reason or "")

    def test_unsupported_capability_evidence_shape(self) -> None:
        plugin = FakeRuntimePlugin(capabilities=frozenset({Capability.DISCOVERY}))
        result = RuntimeClient(plugin).observe(execution_ref())
        evidence = result.evidence
        self.assertEqual(evidence.source, EvidenceSource.PLUGIN)
        self.assertEqual(evidence.verdict, Verdict.UNAVAILABLE)
        self.assertIsNotNone(evidence.capture_time)
        self.assertIsNone(evidence.fingerprint)
        self.assertIsNone(evidence.coverage)
        self.assertIsNotNone(evidence.reason)


class PluginSelectionTests(unittest.TestCase):
    def test_resolve_unknown_plugin_fails_loudly(self) -> None:
        registry = PluginRegistry()
        with self.assertRaises(PluginNotFound) as ctx:
            registry.resolve("missing")
        self.assertEqual(ctx.exception.code, "PLUGIN_NOT_FOUND")

    def test_register_and_resolve_explicitly(self) -> None:
        registry = PluginRegistry()
        plugin = FakeRuntimePlugin(plugin_id="alpha")
        registry.register_instance(plugin)
        self.assertIn("alpha", registry)
        self.assertIs(registry.resolve("alpha"), plugin)
        self.assertEqual(registry.ids(), ("alpha",))

    def test_duplicate_registration_refused(self) -> None:
        registry = PluginRegistry()
        registry.register_instance(FakeRuntimePlugin(plugin_id="alpha"))
        with self.assertRaises(PluginError):
            registry.register_instance(FakeRuntimePlugin(plugin_id="alpha"))
        registry.register_instance(
            FakeRuntimePlugin(plugin_id="alpha"), replace=True
        )

    def test_entries_reports_load_failure_honestly(self) -> None:
        registry = PluginRegistry()

        def broken() -> FakeRuntimePlugin:
            raise RuntimeError("boom")

        registry.register("broken", broken)
        entries = registry.entries()
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0].available)
        self.assertIn("boom", entries[0].reason or "")
        self.assertIsNone(entries[0].descriptor)


class EntryPointDiscoveryTests(unittest.TestCase):
    def test_discovers_registered_entry_points(self) -> None:
        entry = FakeEntryPoint("discovered", lambda: FakeRuntimePlugin("discovered"))
        registry = PluginRegistry.discover(entry_points=[entry])
        self.assertEqual(registry.ids(), ("discovered",))
        self.assertEqual(registry.resolve("discovered").plugin_id, "discovered")

    def test_entry_point_loading_a_class_is_instantiated(self) -> None:
        entry = FakeEntryPoint("fake", lambda: FakeRuntimePlugin)
        registry = PluginRegistry.discover(entry_points=[entry])
        plugin = registry.resolve("fake")
        self.assertIsInstance(plugin, FakeRuntimePlugin)
        self.assertEqual(plugin.plugin_id, "fake")

    def test_default_discovery_is_empty_without_installed_plugins(self) -> None:
        # No plugin packages are installed in the F0 tree; an empty registry is
        # the honest result, not an error and not a fallback runtime.
        registry = PluginRegistry.discover(entry_points=[])
        self.assertEqual(registry.ids(), ())

    def test_broken_entry_point_surfaces_as_unavailable(self) -> None:
        def broken() -> FakeRuntimePlugin:
            raise RuntimeError("cannot import")

        entry = FakeEntryPoint("broken", broken)
        registry = PluginRegistry.discover(entry_points=[entry])
        entries = registry.entries()
        self.assertFalse(entries[0].available)
        self.assertIn("cannot import", entries[0].reason or "")


class DuplicateDiscoveryTests(unittest.TestCase):
    def _registry(self, order: tuple[str, ...]):
        entry_points = [
            FakeEntryPoint("duplicate", lambda name=name: FakeRuntimePlugin(name))
            for name in order
        ]
        return PluginRegistry.discover(entry_points=entry_points)

    def test_both_collision_orders_refuse_selection(self) -> None:
        for order in (("first", "second"), ("second", "first")):
            with self.subTest(order=order):
                registry = self._registry(order)
                self.assertEqual(registry.ids(), ("duplicate",))
                # Neither candidate is retained as an available winner.
                with self.assertRaises(PluginError):
                    registry.resolve("duplicate")

    def test_duplicate_entry_is_reported_unavailable(self) -> None:
        entries = self._registry(("first", "second")).entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].id, "duplicate")
        self.assertFalse(entries[0].available)
        self.assertIn("ambiguous", entries[0].reason or "")

    def test_entry_point_identity_mismatch_refuses_selection(self) -> None:
        entry = FakeEntryPoint("alias", lambda: FakeRuntimePlugin("real"))
        registry = PluginRegistry.discover(entry_points=[entry])
        with self.assertRaises(PluginError):
            registry.resolve("alias")
        report = registry.entries()[0]
        self.assertFalse(report.available)
        self.assertIn("identity mismatch", report.reason or "")

    def test_non_plugin_factory_refuses_selection(self) -> None:
        registry = PluginRegistry()
        registry.register("notaplugin", lambda: object())
        with self.assertRaises(PluginError):
            registry.resolve("notaplugin")


class RuntimeBoundaryTests(unittest.TestCase):
    def test_cross_runtime_reference_never_calls_plugin(self) -> None:
        cases = (
            ("inspect", lambda client: client.inspect(target_ref("other"))),
            ("observe", lambda client: client.observe(execution_ref("other"))),
            ("prompt", lambda client: client.prompt(session_ref("other"), "hi")),
            ("close", lambda client: client.close(session_ref("other"))),
        )
        for name, call in cases:
            with self.subTest(capability=name):
                plugin = FakeRuntimePlugin()
                result = call(RuntimeClient(plugin))
                self.assertFalse(result.ok)
                self.assertIsNone(result.value)
                self.assertEqual(result.evidence.verdict, Verdict.INVALID)
                self.assertEqual(plugin.calls, [])

    def test_wrong_reference_type_refused_without_call(self) -> None:
        plugin = FakeRuntimePlugin()
        result = RuntimeClient(plugin).prompt(object(), "hi")  # type: ignore[arg-type]
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertEqual(result.evidence.verdict, Verdict.INVALID)
        self.assertEqual(plugin.calls, [])

    def test_start_requires_a_start_request(self) -> None:
        plugin = FakeRuntimePlugin()
        result = RuntimeClient(plugin).start(object())  # type: ignore[arg-type]
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertEqual(result.evidence.verdict, Verdict.INVALID)
        self.assertEqual(plugin.calls, [])

    def test_start_returning_wrong_type_is_nonpositive(self) -> None:
        class WrongStart(FakeRuntimePlugin):
            def start(self, request: StartRequest) -> object:
                self.calls.append(("start", request))
                return False

        result = RuntimeClient(WrongStart()).start(
            StartRequest(workspace_id="ws-1", argv=("run",))
        )
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertEqual(result.evidence.verdict, Verdict.INVALID)

    def test_returned_identity_mismatch_is_nonpositive(self) -> None:
        class WrongInspection(FakeRuntimePlugin):
            def inspect(self, target):  # type: ignore[override]
                return replace(super().inspect(target), workspace_id="elsewhere")

        result = RuntimeClient(WrongInspection()).inspect(target_ref())
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertEqual(result.evidence.verdict, Verdict.INVALID)

    def test_malformed_snapshot_metadata_is_nonpositive(self) -> None:
        class MalformedSnapshot(FakeRuntimePlugin):
            def snapshot(self, workspace_id: str):  # type: ignore[override]
                return replace(
                    super().snapshot(workspace_id),
                    runtime_id="wrong",
                    workspace_id="wrong",
                    captured_at="",
                    fingerprint="",
                    coverage=None,
                )

        result = RuntimeClient(MalformedSnapshot()).snapshot("ws-1")
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertEqual(result.evidence.verdict, Verdict.INVALID)

    def test_nonfinite_snapshot_coverage_is_nonpositive(self) -> None:
        class NonFiniteCoverage(FakeRuntimePlugin):
            def snapshot(self, workspace_id: str):  # type: ignore[override]
                return replace(
                    super().snapshot(workspace_id), coverage=float("nan")
                )

        result = RuntimeClient(NonFiniteCoverage()).snapshot("ws-1")
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)


class DiscoveryGuardTests(unittest.TestCase):
    def test_undeclared_discovery_is_unavailable_without_describe(self) -> None:
        class NoDiscovery(FakeRuntimePlugin):
            def describe(self):
                self.calls.append(("describe", None))
                return super().describe()

        plugin = NoDiscovery(capabilities=frozenset())
        result = RuntimeClient(plugin).discover()
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertEqual(result.evidence.verdict, Verdict.UNAVAILABLE)
        self.assertEqual(plugin.calls, [])

    def test_declared_discovery_still_works(self) -> None:
        result = RuntimeClient(FakeRuntimePlugin()).discover()
        self.assertTrue(result.ok)
        self.assertEqual(result.value.id, "fake")

    def test_discovery_returning_none_is_nonpositive(self) -> None:
        class NoneDiscovery(FakeRuntimePlugin):
            def describe(self):  # type: ignore[override]
                return None

        result = RuntimeClient(NoneDiscovery()).discover()
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertEqual(result.evidence.verdict, Verdict.UNKNOWN)

    def test_discovery_returning_wrong_type_is_nonpositive(self) -> None:
        class WrongDiscovery(FakeRuntimePlugin):
            def describe(self):  # type: ignore[override]
                return "not a descriptor"

        result = RuntimeClient(WrongDiscovery()).discover()
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertEqual(result.evidence.verdict, Verdict.INVALID)


class BasePluginTests(unittest.TestCase):
    def test_unimplemented_capability_method_raises(self) -> None:
        plugin = FakeRuntimePlugin(capabilities=frozenset({Capability.DISCOVERY}))
        with self.assertRaises(UnsupportedCapability):
            # Bypass the client to prove the base class itself refuses.
            super(FakeRuntimePlugin, plugin).snapshot("ws-1")


class StartRequestPlacementTests(unittest.TestCase):
    """The additive ``Placement`` contract on ``StartRequest`` (H1-H8)."""

    def test_existing_constructions_still_work_with_new_fields_none(self) -> None:
        positional = StartRequest("ws-1", ("run",))
        labelled = StartRequest("ws-1", ("run",), "label")
        keyword = StartRequest(workspace_id="ws-1", argv=("run",))
        for request in (positional, labelled, keyword):
            with self.subTest(request=request):
                self.assertIsNone(request.target)
                self.assertIsNone(request.placement)
                self.assertIsNone(request.agent_kind)
        self.assertEqual(labelled.label, "label")
        self.assertEqual(keyword.workspace_id, "ws-1")
        self.assertEqual(keyword.argv, ("run",))

    def test_field_order_and_defaults_are_unchanged(self) -> None:
        fields = StartRequest.__dataclass_fields__
        self.assertEqual(
            tuple(fields),
            ("workspace_id", "argv", "label", "target", "placement", "agent_kind"),
        )
        for name in ("label", "target", "placement", "agent_kind"):
            self.assertIs(fields[name].default, None)

    def test_start_request_is_frozen(self) -> None:
        request = StartRequest("ws-1", ("run",))
        with self.assertRaises(FrozenInstanceError):
            request.label = "changed"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            request.placement = Placement.PROVISION  # type: ignore[misc]

    def test_placement_is_str_enum_with_exactly_two_members(self) -> None:
        self.assertTrue(issubclass(Placement, str))
        self.assertEqual(
            {member.name: member.value for member in Placement},
            {"EXISTING": "existing", "PROVISION": "provision"},
        )
        self.assertIsInstance(Placement.EXISTING, str)
        self.assertEqual(Placement.EXISTING, "existing")

    def test_unspecified_placement_is_valid(self) -> None:
        self.assertIsNone(validate_placement(None, None, None))

    def test_existing_with_target_validates(self) -> None:
        self.assertIsNone(
            validate_placement(Placement.EXISTING, target_ref(), None)
        )

    def test_existing_without_target_raises_named_rule(self) -> None:
        with self.assertRaises(InvalidStartRequest) as ctx:
            validate_placement(Placement.EXISTING, None, None)
        self.assertEqual(ctx.exception.rule, "existing_requires_target")

    def test_provision_without_target_validates(self) -> None:
        self.assertIsNone(validate_placement(Placement.PROVISION, None, None))

    def test_provision_with_target_raises_named_rule(self) -> None:
        with self.assertRaises(InvalidStartRequest) as ctx:
            validate_placement(Placement.PROVISION, target_ref(), None)
        self.assertEqual(ctx.exception.rule, "provision_forbids_target")

    def test_target_without_placement_is_refused(self) -> None:
        with self.assertRaises(InvalidStartRequest) as ctx:
            validate_placement(None, target_ref(), None)
        self.assertEqual(ctx.exception.rule, "target_without_placement")

    def test_agent_kind_none_and_opaque_string_validate(self) -> None:
        self.assertIsNone(validate_placement(None, None, None))
        self.assertIsNone(validate_placement(None, None, "agent"))
        # Shape only: the value is opaque and never matched to an allowlist.
        self.assertIsNone(validate_placement(None, None, "totally-opaque-kind"))

    def test_empty_agent_kind_raises_named_rule(self) -> None:
        with self.assertRaises(InvalidStartRequest) as ctx:
            validate_placement(None, None, "")
        self.assertEqual(ctx.exception.rule, "agent_kind")

    def test_non_string_agent_kind_raises_named_rule(self) -> None:
        for bad in (123, b"agent", ["agent"], object()):
            with self.subTest(agent_kind=bad):
                with self.assertRaises(InvalidStartRequest) as ctx:
                    validate_placement(None, None, bad)  # type: ignore[arg-type]
                self.assertEqual(ctx.exception.rule, "agent_kind")

    def test_unknown_placement_value_is_refused(self) -> None:
        with self.assertRaises(InvalidStartRequest) as ctx:
            validate_placement("existing", None, None)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.rule, "unknown_placement")

    def test_every_combination_validates_or_raises_named_error(self) -> None:
        placements = (None, Placement.EXISTING, Placement.PROVISION)
        targets = (None, target_ref())
        agent_kinds = (None, "agent", "", 123)

        def expected_valid(placement, target, agent_kind) -> bool:
            if agent_kind is not None and not (
                isinstance(agent_kind, str) and agent_kind
            ):
                return False
            if placement is None:
                return target is None
            if placement is Placement.EXISTING:
                return target is not None
            return target is None  # Placement.PROVISION

        for placement in placements:
            for target in targets:
                for agent_kind in agent_kinds:
                    with self.subTest(
                        placement=placement,
                        target=target,
                        agent_kind=agent_kind,
                    ):
                        if expected_valid(placement, target, agent_kind):
                            self.assertIsNone(
                                validate_placement(placement, target, agent_kind)
                            )
                        else:
                            # Named and specific, never a bare ValueError.
                            with self.assertRaises(InvalidStartRequest):
                                validate_placement(placement, target, agent_kind)

    def test_contract_documentation_states_the_semantics(self) -> None:
        for doc in (
            Placement.__doc__,
            StartRequest.__doc__,
            validate_placement.__doc__,
        ):
            self.assertTrue(doc and doc.strip())
        self.assertIn("EXISTING", StartRequest.__doc__ or "")
        self.assertIn("PROVISION", StartRequest.__doc__ or "")
        self.assertIn("agent_kind", validate_placement.__doc__ or "")


if __name__ == "__main__":
    unittest.main()
