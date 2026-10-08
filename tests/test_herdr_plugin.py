"""Focused tests for the first-class Herdr runtime plugin.

Everything here runs against a fake transport and synthetic protocol-22
responses. No live Herdr server is contacted and no runtime state is mutated.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from cao.contracts import (
    Capability,
    ExecutionRef,
    SessionRef,
    Snapshot,
    StartRequest,
    TargetRef,
    UnsupportedCapability,
)
from cao.evidence import EvidenceSource, Verdict
from cao.plugins.herdr import (
    HerdrError,
    HerdrNotFoundError,
    HerdrProtocolError,
    HerdrRuntimePlugin,
    HerdrTransportError,
    _subprocess_runner,
    create_plugin,
)
from cao.runtime import RuntimeClient

FIXED_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIXED_TIME = "2026-01-01T00:00:00Z"
EXPECTED_CAPABILITIES = frozenset(
    {
        Capability.DISCOVERY,
        Capability.SNAPSHOT,
        Capability.INSPECT,
        Capability.PROMPT,
    }
)
UNIMPLEMENTED_CAPABILITIES = (
    Capability.OBSERVE,
    Capability.START,
    Capability.CLOSE,
)


# -- synthetic protocol-22 builders ---------------------------------------


def make_workspace(
    workspace_id: str = "ws-1",
    *,
    pane_count: int = 1,
    tab_count: int = 1,
    agent_status: str = "working",
) -> dict:
    return {
        "workspace_id": workspace_id,
        "number": 1,
        "label": "demo",
        "focused": True,
        "pane_count": pane_count,
        "tab_count": tab_count,
        "active_tab_id": f"{workspace_id}:t1",
        "agent_status": agent_status,
    }


def make_pane(
    pane_id: str = "ws-1:p1",
    *,
    workspace_id: str = "ws-1",
    tab_id: str = "ws-1:t1",
    terminal_id: str = "term-1",
    agent_status: str = "working",
    agent: str | None = "example",
    session: tuple[str, str] | None = ("id", "sess-1"),
    cwd: str = "/repo",
) -> dict:
    pane = {
        "pane_id": pane_id,
        "terminal_id": terminal_id,
        "workspace_id": workspace_id,
        "tab_id": tab_id,
        "focused": True,
        "agent_status": agent_status,
        "cwd": cwd,
        "revision": 3,
    }
    if agent is not None:
        pane["agent"] = agent
    if session is not None:
        pane["agent_session"] = {
            "source": "herdr:example",
            "agent": agent or "example",
            "kind": session[0],
            "value": session[1],
        }
    return pane


def snapshot_response(
    *,
    protocol: object = 22,
    version: str = "9.9.9",
    workspaces: list | None = None,
    panes: list | None = None,
    snapshot_overrides: dict | None = None,
) -> str:
    snapshot: dict = {
        "version": version,
        "protocol": protocol,
        "workspaces": [make_workspace()] if workspaces is None else workspaces,
        "tabs": [],
        "panes": [make_pane()] if panes is None else panes,
        "layouts": [],
        "agents": [],
    }
    if snapshot_overrides:
        snapshot.update(snapshot_overrides)
    return _envelope("session_snapshot", "snapshot", snapshot)


def pane_response(pane: dict) -> str:
    return _envelope("pane_info", "pane", pane)


def agent_prompted_response(
    *,
    pane_id: str = "ws-1:p1",
    workspace_id: str = "ws-1",
    terminal_id: str = "term-1",
    agent_status: str = "working",
    session: tuple[str, str] | None = ("id", "sess-1"),
) -> str:
    """A protocol-22 ``agent_prompted`` result carrying an ``AgentInfo``."""

    agent: dict = {
        "pane_id": pane_id,
        "workspace_id": workspace_id,
        "tab_id": "ws-1:t1",
        "terminal_id": terminal_id,
        "agent_status": agent_status,
        "focused": True,
        "revision": 4,
    }
    if session is not None:
        agent["agent_session"] = {
            "source": "herdr:example",
            "agent": "example",
            "kind": session[0],
            "value": session[1],
        }
    return _envelope("agent_prompted", "agent", agent)


def _envelope(result_type: str, key: str, value: object) -> str:
    return json.dumps(
        {
            "id": "cli:test",
            "result": {"type": result_type, key: value},
        }
    )


class FakeRunner:
    """Records CLI calls and returns a fixed response or raises it."""

    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv):
        self.calls.append(tuple(argv))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def make_plugin(response: object) -> tuple[HerdrRuntimePlugin, FakeRunner]:
    runner = FakeRunner(response)
    plugin = HerdrRuntimePlugin(runner=runner, clock=lambda: FIXED_NOW)
    return plugin, runner


class ScriptedRunner:
    """Returns one queued response per call and records the argv.

    An unexpected extra call is a hard failure, so a test that owes exactly one
    side-effecting call can rely on the queue length to notice a stray one.
    """

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv):
        self.calls.append(tuple(argv))
        if not self.responses:
            raise AssertionError(f"unexpected extra Herdr call: {tuple(argv)}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def make_scripted_plugin(
    *responses: object,
) -> tuple[HerdrRuntimePlugin, ScriptedRunner]:
    runner = ScriptedRunner(*responses)
    plugin = HerdrRuntimePlugin(runner=runner, clock=lambda: FIXED_NOW)
    return plugin, runner


def session_ref(
    *,
    workspace_id: str = "ws-1",
    execution_id: str = "term-1",
    session_id: str = "sess-1",
    runtime_id: str = "herdr",
) -> SessionRef:
    return SessionRef(runtime_id, workspace_id, execution_id, session_id)


# -- discovery / capabilities ----------------------------------------------


class DiscoveryTests(unittest.TestCase):
    def test_plugin_identity(self) -> None:
        self.assertEqual(HerdrRuntimePlugin().plugin_id, "herdr")
        self.assertEqual(create_plugin().plugin_id, "herdr")

    def test_describe_reports_supported_capabilities(self) -> None:
        descriptor = HerdrRuntimePlugin().describe()
        self.assertEqual(descriptor.id, "herdr")
        self.assertEqual(descriptor.name, "Herdr")
        self.assertEqual(descriptor.capabilities, EXPECTED_CAPABILITIES)

    def test_client_discovery_is_supported(self) -> None:
        plugin = HerdrRuntimePlugin()
        result = RuntimeClient(plugin).discover()
        self.assertTrue(result.ok)
        self.assertEqual(result.evidence.source, EvidenceSource.PLUGIN)
        self.assertEqual(result.value.capabilities, EXPECTED_CAPABILITIES)

    def test_advertised_capabilities_match_the_implemented_set(self) -> None:
        # K5: the advertised set is exactly what is implemented. Adding a
        # capability without implementing it (or dropping one) fails here.
        advertised = HerdrRuntimePlugin().capabilities()
        self.assertEqual(advertised, EXPECTED_CAPABILITIES)
        self.assertIn(Capability.PROMPT, advertised)
        for capability in UNIMPLEMENTED_CAPABILITIES:
            self.assertNotIn(capability, advertised)


# -- snapshot: supported path ----------------------------------------------


class SnapshotSupportedTests(unittest.TestCase):
    def test_snapshot_is_one_call_with_exact_identity(self) -> None:
        plugin, runner = make_plugin(snapshot_response())
        result = RuntimeClient(plugin).snapshot("ws-1")

        self.assertTrue(result.ok, result.evidence.reason)
        self.assertEqual(runner.calls, [("api", "snapshot")])
        snapshot = result.value
        self.assertEqual(snapshot.runtime_id, "herdr")
        self.assertEqual(snapshot.workspace_id, "ws-1")
        self.assertEqual(snapshot.captured_at, FIXED_TIME)
        self.assertTrue(snapshot.fingerprint.startswith("sha256:"))
        self.assertEqual(snapshot.coverage, 1.0)

    def test_snapshot_entry_preserves_distinct_identities(self) -> None:
        plugin, _ = make_plugin(snapshot_response())
        snapshot = RuntimeClient(plugin).snapshot("ws-1").value

        self.assertEqual(len(snapshot.entries), 1)
        entry = snapshot.entries[0]
        self.assertEqual(entry.target_id, "ws-1:p1")  # layout pane
        self.assertEqual(entry.execution_id, "term-1")  # stable terminal
        self.assertEqual(entry.session_id, "sess-1")  # opaque session
        self.assertEqual(entry.kind, "pane")
        self.assertEqual(entry.state, "working")
        self.assertNotEqual(entry.session_id, entry.target_id)
        self.assertNotEqual(entry.execution_id, entry.target_id)

    def test_snapshot_evidence_carries_plugin_capture_metadata(self) -> None:
        plugin, _ = make_plugin(snapshot_response())
        result = RuntimeClient(plugin).snapshot("ws-1")
        self.assertEqual(result.evidence.capture_time, FIXED_TIME)
        self.assertEqual(result.evidence.fingerprint, result.value.fingerprint)
        self.assertEqual(result.evidence.coverage, 1.0)
        self.assertEqual(result.evidence.verdict, Verdict.SUPPORTED)

    def test_capture_time_is_timezone_aware_rfc3339(self) -> None:
        plugin, _ = make_plugin(snapshot_response())
        captured_at = RuntimeClient(plugin).snapshot("ws-1").value.captured_at
        self.assertTrue(captured_at.endswith("Z"))
        self.assertEqual(datetime.fromisoformat(captured_at.replace("Z", "+00:00")),
                         FIXED_NOW)

    def test_snapshot_filters_to_requested_workspace(self) -> None:
        response = snapshot_response(
            workspaces=[make_workspace("ws-1", pane_count=1), make_workspace("ws-2")],
            panes=[
                make_pane("ws-1:p1", workspace_id="ws-1"),
                make_pane("ws-2:p1", workspace_id="ws-2", session=None),
            ],
        )
        plugin, _ = make_plugin(response)
        snapshot = RuntimeClient(plugin).snapshot("ws-1").value
        self.assertEqual([e.target_id for e in snapshot.entries], ["ws-1:p1"])

    def test_snapshot_reports_partial_coverage_honestly(self) -> None:
        # The workspace claims two panes but the snapshot only shows one.
        response = snapshot_response(
            workspaces=[make_workspace("ws-1", pane_count=2)],
            panes=[make_pane("ws-1:p1")],
        )
        plugin, _ = make_plugin(response)
        snapshot = RuntimeClient(plugin).snapshot("ws-1").value
        self.assertEqual(snapshot.coverage, 0.5)

    def test_empty_workspace_is_fully_covered(self) -> None:
        response = snapshot_response(
            workspaces=[make_workspace("ws-1", pane_count=0, tab_count=0)],
            panes=[],
        )
        plugin, _ = make_plugin(response)
        snapshot = RuntimeClient(plugin).snapshot("ws-1").value
        self.assertEqual(snapshot.entries, ())
        self.assertEqual(snapshot.coverage, 1.0)

    def test_path_session_value_is_not_surfaced_as_identity(self) -> None:
        response = snapshot_response(
            panes=[make_pane(session=("path", "/some/filesystem/path"))]
        )
        plugin, _ = make_plugin(response)
        entry = RuntimeClient(plugin).snapshot("ws-1").value.entries[0]
        self.assertIsNone(entry.session_id)

    def test_pane_without_agent_keeps_terminal_as_execution(self) -> None:
        response = snapshot_response(
            panes=[make_pane(agent=None, agent_status="unknown", session=None)]
        )
        plugin, _ = make_plugin(response)
        entry = RuntimeClient(plugin).snapshot("ws-1").value.entries[0]
        self.assertEqual(entry.state, "unknown")
        self.assertEqual(entry.execution_id, "term-1")
        self.assertIsNone(entry.session_id)


# -- snapshot: malformed and error paths -----------------------------------


class SnapshotMalformedTests(unittest.TestCase):
    def _snapshot(self, response: object):
        plugin, _ = make_plugin(response)
        return RuntimeClient(plugin).snapshot("ws-1")

    def test_non_json_response_raises_protocol_error(self) -> None:
        with self.assertRaises(HerdrProtocolError):
            self._snapshot("not json at all")

    def test_non_object_response_raises_protocol_error(self) -> None:
        with self.assertRaises(HerdrProtocolError):
            self._snapshot("[1, 2, 3]")

    def test_missing_result_raises_protocol_error(self) -> None:
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(json.dumps({"id": "cli:test"}))

    def test_wrong_result_type_raises_protocol_error(self) -> None:
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(_envelope("pong", "value", {}))

    def test_missing_snapshot_object_raises_protocol_error(self) -> None:
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(_envelope("session_snapshot", "other", {}))

    def test_non_object_snapshot_raises_protocol_error(self) -> None:
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(_envelope("session_snapshot", "snapshot", []))

    def test_missing_protocol_raises_protocol_error(self) -> None:
        response = snapshot_response(snapshot_overrides={"protocol": None})
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(response)

    def test_unsupported_protocol_raises_protocol_error(self) -> None:
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(snapshot_response(protocol=23))

    def test_boolean_protocol_raises_protocol_error(self) -> None:
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(snapshot_response(protocol=True))

    def test_missing_workspaces_list_raises_protocol_error(self) -> None:
        response = snapshot_response(snapshot_overrides={"workspaces": None})
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(response)

    def test_missing_panes_list_raises_protocol_error(self) -> None:
        response = snapshot_response(snapshot_overrides={"panes": None})
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(response)

    def test_non_integer_pane_count_raises_protocol_error(self) -> None:
        response = snapshot_response(
            workspaces=[make_workspace("ws-1", pane_count="one")]
        )
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(response)

    def test_pane_missing_id_raises_protocol_error(self) -> None:
        pane = make_pane()
        del pane["pane_id"]
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(snapshot_response(panes=[pane]))

    def test_pane_missing_state_raises_protocol_error(self) -> None:
        pane = make_pane()
        del pane["agent_status"]
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(snapshot_response(panes=[pane]))

    def test_non_object_pane_row_raises_protocol_error(self) -> None:
        # A malformed row must never be silently discarded.
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(snapshot_response(panes=[make_pane(), "not-an-object"]))

    def test_pane_row_missing_workspace_id_raises_protocol_error(self) -> None:
        pane = make_pane()
        del pane["workspace_id"]
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(snapshot_response(panes=[pane]))

    def test_duplicate_pane_target_raises_protocol_error(self) -> None:
        response = snapshot_response(
            workspaces=[make_workspace("ws-1", pane_count=2)],
            panes=[make_pane("ws-1:p1"), make_pane("ws-1:p1")],
        )
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(response)

    def test_observed_count_exceeding_declared_raises_protocol_error(self) -> None:
        # One declared pane but two observed targets is contradictory; it must
        # not be rounded down to coverage 1.0.
        response = snapshot_response(
            workspaces=[make_workspace("ws-1", pane_count=1)],
            panes=[make_pane("ws-1:p1"), make_pane("ws-1:p2")],
        )
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(response)

    def test_unknown_workspace_raises_not_found(self) -> None:
        response = snapshot_response(workspaces=[make_workspace("ws-other")], panes=[])
        with self.assertRaises(HerdrNotFoundError):
            self._snapshot(response)

    def test_error_envelope_not_found_maps_to_not_found(self) -> None:
        response = json.dumps(
            {"id": "cli:test", "error": {"code": "workspace_not_found", "message": "x"}}
        )
        with self.assertRaises(HerdrNotFoundError):
            self._snapshot(response)

    def test_error_envelope_other_code_maps_to_protocol_error(self) -> None:
        response = json.dumps(
            {"id": "cli:test", "error": {"code": "bad_request", "message": "x"}}
        )
        with self.assertRaises(HerdrProtocolError):
            self._snapshot(response)

    def test_runner_transport_failure_propagates(self) -> None:
        plugin, _ = make_plugin(HerdrTransportError("boom"))
        with self.assertRaises(HerdrTransportError):
            RuntimeClient(plugin).snapshot("ws-1")


# -- snapshot: fingerprint qualification -----------------------------------


class SnapshotFingerprintTests(unittest.TestCase):
    def _fingerprint(self, **kwargs) -> str:
        plugin, _ = make_plugin(snapshot_response(**kwargs))
        return RuntimeClient(plugin).snapshot("ws-1").value.fingerprint

    def test_row_order_does_not_change_fingerprint(self) -> None:
        workspaces = [make_workspace("ws-1", pane_count=2)]
        first = self._fingerprint(
            workspaces=workspaces,
            panes=[
                make_pane("ws-1:p1", session=None),
                make_pane("ws-1:p2", agent_status="done", session=None),
            ],
        )
        second = self._fingerprint(
            workspaces=workspaces,
            panes=[
                make_pane("ws-1:p2", agent_status="done", session=None),
                make_pane("ws-1:p1", session=None),
            ],
        )
        self.assertEqual(first, second)

    def test_coverage_change_changes_fingerprint(self) -> None:
        # Same visible rows, different qualification: a workspace declaring a
        # third (unseen) pane produces coverage 2/3 instead of 1.0.
        panes = [make_pane("ws-1:p1", session=None), make_pane("ws-1:p2", session=None)]
        full = self._fingerprint(
            workspaces=[make_workspace("ws-1", pane_count=2)], panes=panes
        )
        partial = self._fingerprint(
            workspaces=[make_workspace("ws-1", pane_count=3)], panes=panes
        )
        self.assertNotEqual(full, partial)

    def test_workspace_identity_change_changes_fingerprint(self) -> None:
        # Same pane shape, different workspace identity: qualification differs.
        first = self._fingerprint(
            workspaces=[make_workspace("ws-1", pane_count=1)],
            panes=[make_pane("ws-1:p1", session=None)],
        )
        plugin, _ = make_plugin(
            snapshot_response(
                workspaces=[make_workspace("ws-2", pane_count=1)],
                panes=[make_pane("ws-2:p1", session=None)],
            )
        )
        second = RuntimeClient(plugin).snapshot("ws-2").value.fingerprint
        self.assertNotEqual(first, second)

    def test_entry_state_change_changes_fingerprint(self) -> None:
        working = self._fingerprint(
            workspaces=[make_workspace("ws-1", pane_count=1)],
            panes=[make_pane("ws-1:p1", agent_status="working", session=None)],
        )
        idle = self._fingerprint(
            workspaces=[make_workspace("ws-1", pane_count=1)],
            panes=[make_pane("ws-1:p1", agent_status="idle", session=None)],
        )
        self.assertNotEqual(working, idle)


# -- inspect: supported path -----------------------------------------------


class InspectTests(unittest.TestCase):
    def test_inspect_is_one_call_with_exact_identity(self) -> None:
        plugin, runner = make_plugin(pane_response(make_pane()))
        target = TargetRef("herdr", "ws-1", "ws-1:p1")
        result = RuntimeClient(plugin).inspect(target)

        self.assertTrue(result.ok, result.evidence.reason)
        self.assertEqual(runner.calls, [("pane", "get", "ws-1:p1")])
        inspection = result.value
        self.assertEqual(inspection.runtime_id, "herdr")
        self.assertEqual(inspection.workspace_id, "ws-1")
        self.assertEqual(inspection.target_id, "ws-1:p1")
        self.assertEqual(inspection.state, "working")
        self.assertEqual(inspection.execution_id, "term-1")
        self.assertEqual(inspection.session_id, "sess-1")
        self.assertNotEqual(inspection.session_id, inspection.target_id)

    def test_inspect_rejects_workspace_mismatch(self) -> None:
        plugin, _ = make_plugin(pane_response(make_pane(workspace_id="ws-other")))
        target = TargetRef("herdr", "ws-1", "ws-1:p1")
        with self.assertRaises(HerdrNotFoundError):
            RuntimeClient(plugin).inspect(target)

    def test_inspect_rejects_wrong_returned_pane_id(self) -> None:
        # Herdr must not be trusted to echo the requested pane; a different
        # returned identity is not a positive inspection of the request.
        plugin, _ = make_plugin(pane_response(make_pane(pane_id="ws-1:p2")))
        target = TargetRef("herdr", "ws-1", "ws-1:p1")
        with self.assertRaises(HerdrProtocolError):
            RuntimeClient(plugin).inspect(target)

    def test_inspect_rejects_missing_returned_pane_id(self) -> None:
        pane = make_pane()
        del pane["pane_id"]
        plugin, _ = make_plugin(pane_response(pane))
        target = TargetRef("herdr", "ws-1", "ws-1:p1")
        with self.assertRaises(HerdrProtocolError):
            RuntimeClient(plugin).inspect(target)

    def test_inspect_wrong_result_type_raises_protocol_error(self) -> None:
        plugin, _ = make_plugin(_envelope("session_snapshot", "snapshot", {}))
        target = TargetRef("herdr", "ws-1", "ws-1:p1")
        with self.assertRaises(HerdrProtocolError):
            RuntimeClient(plugin).inspect(target)

    def test_inspect_missing_pane_object_raises_protocol_error(self) -> None:
        plugin, _ = make_plugin(_envelope("pane_info", "other", {}))
        target = TargetRef("herdr", "ws-1", "ws-1:p1")
        with self.assertRaises(HerdrProtocolError):
            RuntimeClient(plugin).inspect(target)

    def test_inspect_runner_failure_propagates(self) -> None:
        plugin, _ = make_plugin(HerdrTransportError("boom"))
        target = TargetRef("herdr", "ws-1", "ws-1:p1")
        with self.assertRaises(HerdrTransportError):
            RuntimeClient(plugin).inspect(target)


# -- prompt: supported path and identity binding ---------------------------


class _WrongWorkspaceSnapshotPlugin(HerdrRuntimePlugin):
    """A plugin whose snapshot is bound to a different workspace.

    The real plugin always stamps the requested workspace; this deliberately
    violates that invariant so the prompt workspace guard can be exercised.
    """

    def snapshot(self, workspace_id: str) -> Snapshot:
        snapshot = super().snapshot(workspace_id)
        return Snapshot(
            runtime_id=snapshot.runtime_id,
            workspace_id="ws-elsewhere",
            captured_at=snapshot.captured_at,
            fingerprint=snapshot.fingerprint,
            coverage=snapshot.coverage,
            entries=snapshot.entries,
        )


class PromptTests(unittest.TestCase):
    PROMPT_TEXT = "SECRET-PROMPT-TEXT"

    def test_prompt_resolves_session_to_pane_and_issues_one_prompt_call(self) -> None:
        plugin, runner = make_scripted_plugin(
            snapshot_response(), agent_prompted_response()
        )
        result = RuntimeClient(plugin).prompt(session_ref(), "hello")

        self.assertTrue(result.ok, result.evidence.reason)
        self.assertEqual(result.value.session_id, "sess-1")
        self.assertTrue(result.value.accepted)
        # K4: the snapshot read plus exactly one side-effecting agent prompt.
        self.assertEqual(
            runner.calls,
            [("api", "snapshot"), ("agent", "prompt", "ws-1:p1", "hello")],
        )

    def test_prompt_rejects_returned_session_mismatch(self) -> None:
        # The send already happened, so this cannot undo it, but the ack must
        # not claim a success when the pane's agent session changed between the
        # snapshot and the prompt. The returned value is never copied through.
        plugin, runner = make_scripted_plugin(
            snapshot_response(),
            agent_prompted_response(session=("id", "sess-returned-elsewhere")),
        )
        with self.assertRaises(HerdrProtocolError):
            plugin.prompt(session_ref(session_id="sess-1"), "hello")
        # Honest count: the send was issued exactly once.
        self.assertEqual(
            runner.calls,
            [("api", "snapshot"), ("agent", "prompt", "ws-1:p1", "hello")],
        )

    def test_prompt_without_matching_session_fails_closed(self) -> None:
        plugin, runner = make_scripted_plugin(
            snapshot_response(), agent_prompted_response()
        )
        with self.assertRaises(HerdrNotFoundError):
            plugin.prompt(session_ref(session_id="sess-absent"), "hello")
        # No match resolves before the side-effecting call: zero prompt calls.
        self.assertEqual(runner.calls, [("api", "snapshot")])

    def test_prompt_ambiguous_session_match_fails_closed(self) -> None:
        # Two distinct panes record the same session id: refusing to guess.
        response = snapshot_response(
            workspaces=[make_workspace("ws-1", pane_count=2)],
            panes=[make_pane("ws-1:p1"), make_pane("ws-1:p2")],
        )
        plugin, runner = make_scripted_plugin(response, agent_prompted_response())
        with self.assertRaises(HerdrProtocolError):
            plugin.prompt(session_ref(), "hello")
        self.assertEqual(runner.calls, [("api", "snapshot")])

    def test_prompt_snapshot_workspace_mismatch_fails_closed(self) -> None:
        runner = ScriptedRunner(snapshot_response())
        plugin = _WrongWorkspaceSnapshotPlugin(runner=runner, clock=lambda: FIXED_NOW)
        with self.assertRaises(HerdrNotFoundError):
            plugin.prompt(session_ref(workspace_id="ws-1"), "hello")
        self.assertEqual(runner.calls, [("api", "snapshot")])

    def test_prompt_partial_snapshot_fails_closed_without_a_prompt_call(self) -> None:
        # A workspace declaring two panes but listing one is a half-view: the
        # session happens to match, but the plugin must refuse to write.
        response = snapshot_response(
            workspaces=[make_workspace("ws-1", pane_count=2)],
            panes=[make_pane("ws-1:p1")],
        )
        plugin, runner = make_scripted_plugin(response, agent_prompted_response())
        with self.assertRaises(HerdrProtocolError):
            plugin.prompt(session_ref(), "hello")
        self.assertEqual(runner.calls, [("api", "snapshot")])

    def test_prompt_partial_snapshot_hiding_duplicate_session_fails_closed(
        self,
    ) -> None:
        # The exact defect: the visible pane carries the requested session, but
        # an unlisted pane could carry it too, so uniqueness cannot be proven.
        response = snapshot_response(
            workspaces=[make_workspace("ws-1", pane_count=2)],
            panes=[make_pane("ws-1:p1", session=("id", "sess-1"))],
        )
        plugin, runner = make_scripted_plugin(response, agent_prompted_response())
        with self.assertRaises(HerdrProtocolError):
            plugin.prompt(session_ref(), "hello")
        self.assertEqual(runner.calls, [("api", "snapshot")])

    def test_prompt_complete_multi_pane_snapshot_succeeds(self) -> None:
        # The fix must not refuse a complete view: two named panes, two declared,
        # coverage 1.0, and only the matching session is prompted.
        response = snapshot_response(
            workspaces=[make_workspace("ws-1", pane_count=2)],
            panes=[
                make_pane("ws-1:p1", session=("id", "sess-1")),
                make_pane("ws-1:p2", session=("id", "sess-2")),
            ],
        )
        plugin, runner = make_scripted_plugin(response, agent_prompted_response())
        ack = plugin.prompt(session_ref(session_id="sess-1"), "hello")
        self.assertTrue(ack.accepted)
        self.assertEqual(ack.session_id, "sess-1")
        self.assertEqual(
            runner.calls,
            [("api", "snapshot"), ("agent", "prompt", "ws-1:p1", "hello")],
        )

    def test_prompt_refuses_text_starting_with_hyphen(self) -> None:
        # A leading hyphen may be read as an option by the bare-argument CLI, so
        # it is refused before any call is made.
        plugin, runner = make_scripted_plugin(
            snapshot_response(), agent_prompted_response()
        )
        with self.assertRaises(ValueError):
            plugin.prompt(session_ref(), "--wait")
        self.assertEqual(runner.calls, [])

    def test_prompt_rejects_wrong_returned_pane_id(self) -> None:
        plugin, runner = make_scripted_plugin(
            snapshot_response(), agent_prompted_response(pane_id="ws-1:p2")
        )
        with self.assertRaises(HerdrProtocolError) as caught:
            plugin.prompt(session_ref(), self.PROMPT_TEXT)
        self.assertNotIn(self.PROMPT_TEXT, str(caught.exception))
        self.assertEqual(
            runner.calls,
            [("api", "snapshot"), ("agent", "prompt", "ws-1:p1", self.PROMPT_TEXT)],
        )

    def test_prompt_rejects_returned_workspace_mismatch(self) -> None:
        plugin, runner = make_scripted_plugin(
            snapshot_response(), agent_prompted_response(workspace_id="ws-other")
        )
        with self.assertRaises(HerdrNotFoundError):
            plugin.prompt(session_ref(workspace_id="ws-1"), "hello")
        self.assertEqual(len(runner.calls), 2)

    def test_prompt_malformed_response_fails_closed(self) -> None:
        # K6: an unreadable or unbound response never becomes an acceptance.
        malformed = {
            "non-json": "not json at all",
            "wrong result type": _envelope("pane_info", "pane", make_pane()),
            "missing agent object": _envelope("agent_prompted", "other", {}),
            "non-object agent": _envelope("agent_prompted", "agent", []),
            "agent without pane id": _envelope(
                "agent_prompted", "agent", {"workspace_id": "ws-1"}
            ),
        }
        for label, response in malformed.items():
            with self.subTest(label=label):
                plugin, runner = make_scripted_plugin(
                    snapshot_response(), response
                )
                with self.assertRaises(HerdrProtocolError):
                    plugin.prompt(session_ref(), "hello")
                self.assertEqual(len(runner.calls), 2)

    def test_prompt_error_envelope_is_sanitized(self) -> None:
        secret = "/opt/example-host/secret"
        recognized = json.dumps(
            {"id": "cli:test", "error": {"code": "pane_not_found", "message": secret}}
        )
        plugin, runner = make_scripted_plugin(snapshot_response(), recognized)
        with self.assertRaises(HerdrNotFoundError) as caught:
            plugin.prompt(session_ref(), "hello")
        self.assertEqual(str(caught.exception), "Herdr reports 'pane_not_found'")
        self.assertNotIn(secret, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(len(runner.calls), 2)

        unrecognized = json.dumps(
            {
                "id": "cli:test",
                "error": {"code": secret + "_not_found", "message": secret},
            }
        )
        plugin, runner = make_scripted_plugin(snapshot_response(), unrecognized)
        with self.assertRaises(HerdrProtocolError) as caught:
            plugin.prompt(session_ref(), "hello")
        self.assertEqual(
            str(caught.exception), "Herdr returned an unrecognized error response"
        )
        self.assertNotIn(secret, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_prompt_missing_executable_is_sanitized_and_unchained(self) -> None:
        # The second call goes through the real default transport, whose
        # executable is absent: no process runs, and the OS error text (which
        # carries the path) is never surfaced or chained.
        secret = "/opt/example-host/secret/herdr"
        snapshot_runner = FakeRunner(snapshot_response())

        def runner(argv):
            if argv[:1] == ("api",):
                return snapshot_runner(argv)
            return _subprocess_runner(secret, 5.0)(argv)

        plugin = HerdrRuntimePlugin(runner=runner, clock=lambda: FIXED_NOW)
        with self.assertRaises(HerdrTransportError) as caught:
            plugin.prompt(session_ref(), self.PROMPT_TEXT)
        message = str(caught.exception)
        self.assertNotIn(secret, message)
        self.assertNotIn("example-host", message)
        self.assertNotIn(self.PROMPT_TEXT, message)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_prompt_timeout_is_sanitized_and_unchained(self) -> None:
        snapshot_runner = FakeRunner(snapshot_response())

        def runner(argv):
            if argv[:1] == ("api",):
                return snapshot_runner(argv)
            return _subprocess_runner("herdr", 0.2)(argv)

        plugin = HerdrRuntimePlugin(runner=runner, clock=lambda: FIXED_NOW)
        secret_cmd = [
            "/opt/example-host/secret/herdr",
            "agent",
            "prompt",
            "ws-1:p1",
            self.PROMPT_TEXT,
        ]
        with mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired(secret_cmd, 0.2)
        ):
            with self.assertRaises(HerdrTransportError) as caught:
                plugin.prompt(session_ref(), self.PROMPT_TEXT)
        message = str(caught.exception)
        self.assertIn("timed out", message)
        self.assertNotIn("/opt/example-host", message)
        self.assertNotIn(self.PROMPT_TEXT, message)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_prompt_never_reaches_real_subprocess(self) -> None:
        # K7: with the injected runner, the real transport must not run.
        plugin, runner = make_scripted_plugin(
            snapshot_response(), agent_prompted_response()
        )
        with mock.patch(
            "subprocess.run", side_effect=AssertionError("real subprocess reached")
        ):
            ack = plugin.prompt(session_ref(), "hello")
        self.assertTrue(ack.accepted)
        self.assertEqual(
            runner.calls,
            [("api", "snapshot"), ("agent", "prompt", "ws-1:p1", "hello")],
        )


# -- explicit unsupported / unavailable capabilities -----------------------


class UnsupportedCapabilityTests(unittest.TestCase):
    def assert_unavailable(self, result, plugin_runner: FakeRunner) -> None:
        self.assertFalse(result.ok)
        self.assertIsNone(result.value)
        self.assertEqual(result.evidence.verdict, Verdict.UNAVAILABLE)
        self.assertEqual(result.evidence.source, EvidenceSource.PLUGIN)
        self.assertEqual(plugin_runner.calls, [])

    def setUp(self) -> None:
        self.plugin, self.runner = make_plugin(snapshot_response())
        self.client = RuntimeClient(self.plugin)

    def test_observe_is_unavailable_without_a_call(self) -> None:
        execution = ExecutionRef("herdr", "ws-1", "term-1")
        self.assert_unavailable(self.client.observe(execution), self.runner)

    def test_start_is_unavailable_without_a_call(self) -> None:
        request = StartRequest(workspace_id="ws-1", argv=("example",))
        self.assert_unavailable(self.client.start(request), self.runner)

    def test_close_is_unavailable_without_a_call(self) -> None:
        session = session_ref()
        self.assert_unavailable(self.client.close(session), self.runner)

    def test_plugin_methods_raise_unsupported_capability(self) -> None:
        plugin = HerdrRuntimePlugin()
        execution = ExecutionRef("herdr", "ws-1", "term-1")
        session = session_ref()
        with self.assertRaises(UnsupportedCapability):
            plugin.observe(execution)
        with self.assertRaises(UnsupportedCapability):
            plugin.start(StartRequest(workspace_id="ws-1", argv=("example",)))
        with self.assertRaises(UnsupportedCapability):
            plugin.close(session)
        # prompt is implemented and must no longer raise UnsupportedCapability.
        self.assertIn(Capability.PROMPT, plugin.capabilities())


class ClientReferenceGuardTests(unittest.TestCase):
    def test_inspect_wrong_runtime_reference_is_invalid_without_call(self) -> None:
        plugin, runner = make_plugin(pane_response(make_pane()))
        target = TargetRef("some-other-runtime", "ws-1", "ws-1:p1")
        result = RuntimeClient(plugin).inspect(target)
        self.assertFalse(result.ok)
        self.assertEqual(result.evidence.verdict, Verdict.INVALID)
        self.assertEqual(runner.calls, [])

    def test_observe_wrong_runtime_reference_is_invalid_without_call(self) -> None:
        plugin, runner = make_plugin(snapshot_response())
        execution = ExecutionRef("some-other-runtime", "ws-1", "term-1")
        result = RuntimeClient(plugin).observe(execution)
        self.assertFalse(result.ok)
        self.assertEqual(result.evidence.verdict, Verdict.INVALID)
        self.assertEqual(runner.calls, [])


# -- transport -------------------------------------------------------------


class TransportTests(unittest.TestCase):
    def test_default_executable_is_missing_binary_fails_closed(self) -> None:
        # A non-existent executable name is an attempted read-only invocation,
        # never a runtime mutation, and must fail closed.
        plugin = HerdrRuntimePlugin(executable="cao-herdr-not-installed-xyz")
        with self.assertRaises(HerdrTransportError):
            RuntimeClient(plugin).snapshot("ws-1")

    def test_constructor_rejects_non_callable_runner(self) -> None:
        with self.assertRaises(TypeError):
            HerdrRuntimePlugin(runner="not-callable")  # type: ignore[arg-type]


class TransportSanitizationTests(unittest.TestCase):
    """Public transport failures must never surface host paths or argv."""

    def _assert_sanitized(self, error: HerdrError, secrets: tuple[str, ...]):
        message = str(error)
        for secret in secrets:
            self.assertNotIn(secret, message)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_missing_executable_message_hides_path(self) -> None:
        secret = os.path.join(tempfile.gettempdir(), "cao-herdr-secret-bin", "herdr")
        with self.assertRaises(HerdrTransportError) as caught:
            _subprocess_runner(secret, 5.0)(("api", "snapshot"))
        self._assert_sanitized(caught.exception, (secret, "cao-herdr-secret-bin"))

    def test_permission_failure_message_hides_path(self) -> None:
        handle = tempfile.NamedTemporaryFile(delete=False, suffix="-herdr")
        handle.close()
        os.chmod(handle.name, 0o600)
        try:
            with self.assertRaises(HerdrTransportError) as caught:
                _subprocess_runner(handle.name, 5.0)(("api", "snapshot"))
            self._assert_sanitized(caught.exception, (handle.name, "herdr"))
        finally:
            os.unlink(handle.name)

    def test_timeout_message_hides_argv(self) -> None:
        with self.assertRaises(HerdrTransportError) as caught:
            _subprocess_runner(sys.executable, 0.2)(
                ("-c", "import time; time.sleep(5)")
            )
        self._assert_sanitized(caught.exception, ("time.sleep", "sleep(5)"))
        self.assertIn("timed out", str(caught.exception))

    def test_error_envelope_message_is_not_surfaced(self) -> None:
        response = json.dumps(
            {
                "id": "cli:test",
                "error": {
                    "code": "workspace_not_found",
                    "message": "/opt/example-host/secret/workspace",
                },
            }
        )
        plugin, _ = make_plugin(response)
        with self.assertRaises(HerdrNotFoundError) as caught:
            RuntimeClient(plugin).snapshot("ws-1")
        self._assert_sanitized(caught.exception, ("/opt/example-host", "secret"))
        self.assertEqual(str(caught.exception), "Herdr reports 'workspace_not_found'")

    def test_stderr_error_envelope_is_not_surfaced(self) -> None:
        # The default runner maps a failed process; only the code is reflected.
        stderr = json.dumps(
            {
                "id": "cli:test",
                "error": {
                    "code": "pane_not_found",
                    "message": "/opt/example-host/secret/pane",
                },
            }
        )
        process = type("P", (), {"returncode": 1, "stdout": "", "stderr": stderr})
        with mock.patch("subprocess.run", return_value=process):
            with self.assertRaises(HerdrNotFoundError) as caught:
                _subprocess_runner("herdr", 5.0)(("pane", "get", "ws-1:p1"))
        self._assert_sanitized(
            caught.exception, ("/opt/example-host", "secret", "ws-1:p1")
        )
        self.assertEqual(str(caught.exception), "Herdr reports 'pane_not_found'")


class UntrustedFieldSanitizationTests(unittest.TestCase):
    """Untrusted error codes/result types must not reach public exceptions.

    A path-shaped or otherwise hostile value, arriving in stdout or stderr,
    must produce a fixed public message and leave no ``__cause__`` /
    ``__context__`` that could still expose it.
    """

    SECRET = "/opt/example-host/secret"

    def _assert_clean(self, error: HerdrError, secrets: tuple[str, ...]) -> None:
        message = str(error)
        for secret in secrets:
            self.assertNotIn(secret, message)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def _stdout_error(self, code: object) -> str:
        return json.dumps(
            {"id": "cli:test", "error": {"code": code, "message": self.SECRET}}
        )

    def _run_stderr(self, stderr: str) -> HerdrTransportError:
        process = type("P", (), {"returncode": 1, "stdout": "", "stderr": stderr})
        with mock.patch("subprocess.run", return_value=process):
            with self.assertRaises(HerdrTransportError) as caught:
                _subprocess_runner("herdr", 5.0)(("pane", "get", "ws-1:p1"))
        return caught.exception

    def test_path_shaped_not_found_code_in_stdout_is_not_echoed(self) -> None:
        response = self._stdout_error(self.SECRET + "_not_found")
        plugin, _ = make_plugin(response)
        with self.assertRaises(HerdrProtocolError) as caught:
            RuntimeClient(plugin).snapshot("ws-1")
        self._assert_clean(caught.exception, (self.SECRET, "secret_not_found"))
        self.assertEqual(
            str(caught.exception), "Herdr returned an unrecognized error response"
        )

    def test_path_shaped_not_found_code_in_stderr_is_not_echoed(self) -> None:
        error = self._run_stderr(self._stdout_error(self.SECRET + "_not_found"))
        self._assert_clean(error, (self.SECRET, "secret_not_found", "ws-1:p1"))
        self.assertEqual(str(error), "Herdr command failed with exit code 1")

    def test_non_string_code_in_stdout_is_not_echoed(self) -> None:
        response = self._stdout_error([self.SECRET])
        plugin, _ = make_plugin(response)
        with self.assertRaises(HerdrProtocolError) as caught:
            RuntimeClient(plugin).snapshot("ws-1")
        self._assert_clean(caught.exception, (self.SECRET,))
        self.assertEqual(
            str(caught.exception), "Herdr returned an unrecognized error response"
        )

    def test_non_string_code_in_stderr_is_not_echoed(self) -> None:
        error = self._run_stderr(self._stdout_error(12345))
        self._assert_clean(error, (self.SECRET, "12345"))
        self.assertEqual(str(error), "Herdr command failed with exit code 1")

    def test_null_code_is_treated_as_unrecognized(self) -> None:
        response = self._stdout_error(None)
        plugin, _ = make_plugin(response)
        with self.assertRaises(HerdrProtocolError) as caught:
            RuntimeClient(plugin).snapshot("ws-1")
        self._assert_clean(caught.exception, (self.SECRET,))
        self.assertEqual(
            str(caught.exception), "Herdr returned an unrecognized error response"
        )

    def test_hostile_result_type_is_omitted_from_error(self) -> None:
        response = json.dumps(
            {"id": "cli:test", "result": {"type": self.SECRET, "value": {}}}
        )
        plugin, _ = make_plugin(response)
        with self.assertRaises(HerdrProtocolError) as caught:
            RuntimeClient(plugin).snapshot("ws-1")
        self._assert_clean(caught.exception, (self.SECRET,))
        self.assertEqual(
            str(caught.exception), "expected a 'session_snapshot' result"
        )

    def test_hostile_result_type_for_inspect_is_omitted(self) -> None:
        response = json.dumps(
            {"id": "cli:test", "result": {"type": self.SECRET, "value": {}}}
        )
        plugin, _ = make_plugin(response)
        with self.assertRaises(HerdrProtocolError) as caught:
            RuntimeClient(plugin).inspect(TargetRef("herdr", "ws-1", "ws-1:p1"))
        self._assert_clean(caught.exception, (self.SECRET,))
        self.assertEqual(str(caught.exception), "expected a 'pane_info' result")

    def test_malformed_json_secret_is_not_retained(self) -> None:
        response = '{"result": "' + self.SECRET + '", "broken":'
        plugin, _ = make_plugin(response)
        with self.assertRaises(HerdrProtocolError) as caught:
            RuntimeClient(plugin).snapshot("ws-1")
        self._assert_clean(caught.exception, (self.SECRET,))
        self.assertEqual(
            str(caught.exception), "Herdr returned output that is not JSON"
        )


# -- plugin import boundary (the repo suite's top-level glob does not reach here)


class PluginImportBoundaryTests(unittest.TestCase):
    """The plugin lives outside the repo suite's ``src/cao/*.py`` glob.

    The privacy denylist still applies (it uses a recursive glob), but these
    checks close the rest of the boundary locally: standard library or cao
    only, and no concrete sibling plugin import.
    """

    PLUGIN_PATH = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "cao"
        / "plugins"
        / "herdr"
        / "__init__.py"
    )
    CAO_ROOT = Path(__file__).resolve().parents[1] / "src" / "cao"

    def _imported_modules(self) -> set[str]:
        tree = ast.parse(self.PLUGIN_PATH.read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        return modules

    def test_plugin_imports_only_stdlib_or_cao(self) -> None:
        extra: list[str] = []
        for module in self._imported_modules():
            top = module.split(".")[0]
            if top == "cao":
                continue
            if (self.CAO_ROOT / f"{top}.py").exists():
                # A relative import resolved to a sibling cao module.
                continue
            if top not in sys.stdlib_module_names:
                extra.append(module)
        self.assertEqual(extra, [])

    def test_plugin_imports_no_concrete_sibling_plugin(self) -> None:
        offenders = [
            module
            for module in self._imported_modules()
            if module in {"plugins", "cao.plugins", "observers", "cao.observers"}
            or module.startswith(("plugins.", "cao.plugins."))
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
