"""Shared test helpers for CAO.

Nothing here is part of the shipped package. The synthetic runtime is a test
fixture only; CAO ships no runtime plugin.
"""

from __future__ import annotations

import json
from pathlib import Path

from cao.contracts import (
    Capability,
    CloseAck,
    ExecutionRef,
    Observation,
    PromptAck,
    SessionRef,
    Snapshot,
    SnapshotEntry,
    StartRequest,
    TargetInspection,
    TargetRef,
)
from cao.evidence import fingerprint
from cao.runtime import RuntimePlugin

FIXED_CAPTURE_TIME = "2026-01-01T00:00:00Z"
ALL_CAPABILITIES = frozenset(Capability)


class FakeRuntimePlugin(RuntimePlugin):
    """A synthetic in-memory runtime used to exercise the contract.

    Capabilities are configurable so tests can prove that an unsupported
    capability yields explicit ``UNAVAILABLE`` evidence instead of a call or a
    fabricated value.
    """

    runtime_name = "Fake Runtime"
    runtime_version = "1.2.3"

    def __init__(
        self,
        plugin_id: str = "fake",
        capabilities: frozenset[Capability] | None = None,
        *,
        return_none_for: Capability | None = None,
    ) -> None:
        self._plugin_id = plugin_id
        self._capabilities = (
            frozenset(capabilities) if capabilities is not None else ALL_CAPABILITIES
        )
        self._return_none_for = return_none_for
        self.calls: list[tuple[str, object]] = []

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    def capabilities(self) -> frozenset[Capability]:
        return self._capabilities

    def snapshot(self, workspace_id: str) -> Snapshot:
        self.calls.append(("snapshot", workspace_id))
        if self._return_none_for is Capability.SNAPSHOT:
            return None  # type: ignore[return-value]
        entries = (
            SnapshotEntry(
                target_id="target-1",
                kind="session",
                state="running",
                execution_id="exec-1",
                session_id="sess-1",
            ),
        )
        payload = [
            {
                "target_id": entry.target_id,
                "kind": entry.kind,
                "state": entry.state,
                "execution_id": entry.execution_id,
                "session_id": entry.session_id,
            }
            for entry in entries
        ]
        return Snapshot(
            runtime_id=self.plugin_id,
            workspace_id=workspace_id,
            captured_at=FIXED_CAPTURE_TIME,
            fingerprint=fingerprint(payload),
            coverage=0.75,
            entries=entries,
        )

    def inspect(self, target: TargetRef) -> TargetInspection:
        self.calls.append(("inspect", target))
        if self._return_none_for is Capability.INSPECT:
            return None  # type: ignore[return-value]
        return TargetInspection(
            runtime_id=self.plugin_id,
            workspace_id=target.workspace_id,
            target_id=target.target_id,
            state="running",
            execution_id="exec-1",
            session_id="sess-1",
        )

    def observe(self, execution: ExecutionRef) -> Observation:
        self.calls.append(("observe", execution))
        if self._return_none_for is Capability.OBSERVE:
            return None  # type: ignore[return-value]
        return Observation(
            runtime_id=self.plugin_id,
            workspace_id=execution.workspace_id,
            execution_id=execution.execution_id,
            events=(),
        )

    def start(self, request: StartRequest) -> ExecutionRef:
        self.calls.append(("start", request))
        if self._return_none_for is Capability.START:
            return None  # type: ignore[return-value]
        return ExecutionRef(
            runtime_id=self.plugin_id,
            workspace_id=request.workspace_id,
            execution_id="exec-1",
        )

    def prompt(self, session: SessionRef, text: str) -> PromptAck:
        self.calls.append(("prompt", (session, text)))
        if self._return_none_for is Capability.PROMPT:
            return None  # type: ignore[return-value]
        return PromptAck(session_id=session.session_id, accepted=True)

    def close(self, session: SessionRef) -> CloseAck:
        self.calls.append(("close", session))
        if self._return_none_for is Capability.CLOSE:
            return None  # type: ignore[return-value]
        return CloseAck(session_id=session.session_id, closed=True)


class FakeEntryPoint:
    """Minimal stand-in for importlib.metadata.EntryPoint."""

    def __init__(self, name: str, factory) -> None:
        self.name = name
        self._factory = factory

    def load(self):
        return self._factory()


def execution_ref(plugin_id: str = "fake") -> ExecutionRef:
    return ExecutionRef(
        runtime_id=plugin_id, workspace_id="ws-1", execution_id="exec-1"
    )


def session_ref(plugin_id: str = "fake") -> SessionRef:
    return SessionRef(
        runtime_id=plugin_id,
        workspace_id="ws-1",
        execution_id="exec-1",
        session_id="sess-1",
    )


def target_ref(plugin_id: str = "fake") -> TargetRef:
    return TargetRef(runtime_id=plugin_id, workspace_id="ws-1", target_id="target-1")


def write_text(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def write_json(path: Path, payload) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
