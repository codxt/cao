"""Runtime-neutral contract types for CAO.

These types are runtime agnostic. CAO never names a concrete runtime here;
runtimes integrate through plugins (see :mod:`cao.runtime`). Identities are
generic: a *runtime* hosts a *workspace*; a workspace may hold many
*executions* (one launched unit of work); an execution may hold one or more
*sessions* (a promptable conversation with the execution).

Nothing in this module performs I/O, reads policy, or touches persistent
state. It is the shared vocabulary the rest of CAO speaks.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

from .evidence import Evidence

__all__ = [
    "Capability",
    "RuntimeId",
    "WorkspaceId",
    "ExecutionId",
    "SessionId",
    "UnsupportedCapability",
    "RuntimeDescriptor",
    "WorkspaceRef",
    "ExecutionRef",
    "SessionRef",
    "TargetRef",
    "StartRequest",
    "SnapshotEntry",
    "Snapshot",
    "TargetInspection",
    "RuntimeEvent",
    "Observation",
    "PromptAck",
    "CloseAck",
    "CapabilityResult",
]

# Generic identities. These are intentionally plain strings: CAO does not own
# the meaning of an id, only its role.
RuntimeId = str
WorkspaceId = str
ExecutionId = str
SessionId = str


class Capability(str, Enum):
    """The generic runtime capabilities the frozen contract exposes."""

    DISCOVERY = "capability.discovery"
    SNAPSHOT = "snapshot.capture"
    INSPECT = "target.inspect"
    OBSERVE = "output.observe"
    START = "execution.start"
    PROMPT = "session.prompt"
    CLOSE = "session.close"


class UnsupportedCapability(RuntimeError):
    """Raised by a plugin when a capability method is not implemented."""

    def __init__(self, capability: Capability) -> None:
        self.capability = capability
        super().__init__(f"capability not supported: {capability.value}")


@dataclass(frozen=True)
class RuntimeDescriptor:
    """A runtime's self-description, produced by capability discovery."""

    id: RuntimeId
    name: str
    version: str
    capabilities: frozenset[Capability]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "capabilities": sorted(cap.value for cap in self.capabilities),
        }


@dataclass(frozen=True)
class WorkspaceRef:
    runtime_id: RuntimeId
    workspace_id: WorkspaceId


@dataclass(frozen=True)
class ExecutionRef:
    runtime_id: RuntimeId
    workspace_id: WorkspaceId
    execution_id: ExecutionId


@dataclass(frozen=True)
class SessionRef:
    runtime_id: RuntimeId
    workspace_id: WorkspaceId
    execution_id: ExecutionId
    session_id: SessionId


@dataclass(frozen=True)
class TargetRef:
    runtime_id: RuntimeId
    workspace_id: WorkspaceId
    target_id: str


@dataclass(frozen=True)
class StartRequest:
    """A request to start one execution in a workspace."""

    workspace_id: WorkspaceId
    argv: tuple[str, ...]
    label: str | None = None


@dataclass(frozen=True)
class SnapshotEntry:
    """One observed item in a workspace snapshot."""

    target_id: str
    kind: str
    state: str
    execution_id: ExecutionId | None = None
    session_id: SessionId | None = None
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Snapshot:
    """Exactly one capture of a workspace's observable state.

    ``fingerprint`` and ``coverage`` are produced by the capturing plugin so
    the resulting evidence reflects what was actually seen, not what CAO
    assumed.
    """

    runtime_id: RuntimeId
    workspace_id: WorkspaceId
    captured_at: str
    fingerprint: str
    coverage: float
    entries: tuple[SnapshotEntry, ...] = ()


@dataclass(frozen=True)
class TargetInspection:
    """A point-in-time inspection of one target (process, pane, session...)."""

    runtime_id: RuntimeId
    workspace_id: WorkspaceId
    target_id: str
    state: str
    execution_id: ExecutionId | None = None
    session_id: SessionId | None = None
    detail: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RuntimeEvent:
    """One unit of runtime output or an event, in observation order."""

    sequence: int
    kind: str  # e.g. "output" or "event"
    timestamp: str
    content: str
    channel: str | None = None


@dataclass(frozen=True)
class Observation:
    """Observed output/events for one execution."""

    runtime_id: RuntimeId
    workspace_id: WorkspaceId
    execution_id: ExecutionId
    events: tuple[RuntimeEvent, ...] = ()
    cursor: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class PromptAck:
    session_id: SessionId
    accepted: bool
    detail: str | None = None


@dataclass(frozen=True)
class CloseAck:
    session_id: SessionId
    closed: bool
    detail: str | None = None


T = TypeVar("T")


@dataclass(frozen=True)
class CapabilityResult(Generic[T]):
    """A capability outcome paired with the evidence that qualifies it.

    ``value`` is populated only when evidence is ``SUPPORTED``. When a
    capability is unavailable the caller still receives an explicit
    ``UNAVAILABLE`` evidence value and ``value is None`` — there is no silent
    fallback and no fabricated result.
    """

    capability: Capability
    evidence: Evidence
    value: T | None = None

    @property
    def ok(self) -> bool:
        return self.evidence.supported and self.value is not None
