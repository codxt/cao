"""A synthetic, in-memory runtime plugin for demonstration and manual testing.

This is **not** a real runtime and ships with no product. It exists only to
show the plugin contract in `docs/runtime-plugin.md` against concrete values.

Run the self-check with:

    PYTHONPATH=src python3 -B examples/synthetic_runtime.py
"""

from __future__ import annotations

import sys
from dataclasses import asdict

from cao.contracts import (
    Capability,
    CloseAck,
    ExecutionRef,
    Observation,
    PromptAck,
    RuntimeEvent,
    SessionRef,
    Snapshot,
    SnapshotEntry,
    StartRequest,
    TargetInspection,
    TargetRef,
)
from cao.evidence import fingerprint, utc_now
from cao.runtime import RuntimeClient, RuntimePlugin

PLUGIN_ID = "synthetic"


class SyntheticRuntime(RuntimePlugin):
    """A deterministic in-memory runtime with one workspace and one session."""

    runtime_name = "Synthetic Runtime"
    runtime_version = "0.1"

    def __init__(self, workspace_id: str = "demo-workspace") -> None:
        self._workspace_id = workspace_id
        self._executions: dict[str, str] = {}
        self._sessions: dict[str, str] = {}

    @property
    def plugin_id(self) -> str:
        return PLUGIN_ID

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.DISCOVERY,
                Capability.SNAPSHOT,
                Capability.INSPECT,
                Capability.OBSERVE,
                Capability.START,
                Capability.PROMPT,
                Capability.CLOSE,
            }
        )

    def snapshot(self, workspace_id: str) -> Snapshot:
        entries = tuple(
            SnapshotEntry(
                target_id=execution_id,
                kind="execution",
                state=state,
                execution_id=execution_id,
                session_id=self._sessions.get(execution_id),
            )
            for execution_id, state in sorted(self._executions.items())
        )
        payload = [asdict(entry) for entry in entries]
        return Snapshot(
            runtime_id=PLUGIN_ID,
            workspace_id=workspace_id,
            captured_at=utc_now(),
            fingerprint=fingerprint(payload),
            coverage=1.0 if entries else 0.0,
            entries=entries,
        )

    def inspect(self, target: TargetRef) -> TargetInspection:
        state = self._executions.get(target.target_id, "absent")
        return TargetInspection(
            runtime_id=PLUGIN_ID,
            workspace_id=target.workspace_id,
            target_id=target.target_id,
            state=state,
            execution_id=target.target_id,
            session_id=self._sessions.get(target.target_id),
        )

    def observe(self, execution: ExecutionRef) -> Observation:
        events = (
            RuntimeEvent(
                sequence=0,
                kind="output",
                timestamp=utc_now(),
                content="synthetic output",
                channel="stdout",
            ),
        )
        return Observation(
            runtime_id=PLUGIN_ID,
            workspace_id=execution.workspace_id,
            execution_id=execution.execution_id,
            events=events,
        )

    def start(self, request: StartRequest) -> ExecutionRef:
        execution_id = f"exec-{len(self._executions) + 1}"
        self._executions[execution_id] = "running"
        return ExecutionRef(
            runtime_id=PLUGIN_ID,
            workspace_id=request.workspace_id,
            execution_id=execution_id,
        )

    def prompt(self, session: SessionRef, text: str) -> PromptAck:
        if session.session_id not in self._sessions:
            self._sessions[session.session_id] = session.execution_id
        return PromptAck(session_id=session.session_id, accepted=True)

    def close(self, session: SessionRef) -> CloseAck:
        self._sessions.pop(session.session_id, None)
        return CloseAck(session_id=session.session_id, closed=True)


def _self_check() -> None:
    client = RuntimeClient(SyntheticRuntime())
    assert client.discover().ok

    started = client.start(StartRequest(workspace_id="demo-workspace", argv=("demo",)))
    assert started.ok, started.evidence.reason
    execution = started.value

    snapshot = client.snapshot("demo-workspace")
    assert snapshot.ok and snapshot.value.entries
    assert snapshot.evidence.coverage == 1.0

    session = SessionRef(
        runtime_id=PLUGIN_ID,
        workspace_id=execution.workspace_id,
        execution_id=execution.execution_id,
        session_id="sess-1",
    )
    assert client.prompt(session, "hello").ok
    assert client.close(session).ok

    # An unavailable capability is explicit, never faked.
    limited = RuntimeClient(LimitedRuntime())
    unavailable = limited.close(session)
    assert not unavailable.ok and unavailable.value is None
    assert unavailable.evidence.verdict.value == "UNAVAILABLE"

    print("synthetic runtime self-check: OK")


class LimitedRuntime(SyntheticRuntime):
    """A runtime that deliberately offers fewer capabilities."""

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.DISCOVERY, Capability.SNAPSHOT})


if __name__ == "__main__":
    _self_check()
    sys.exit(0)
