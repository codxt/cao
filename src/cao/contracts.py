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
    "Placement",
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
    "InvalidStartRequest",
    "SnapshotEntry",
    "Snapshot",
    "TargetInspection",
    "RuntimeEvent",
    "Observation",
    "PromptAck",
    "CloseAck",
    "CapabilityResult",
    "validate_placement",
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


class Placement(str, Enum):
    """Who owns the execution target a start request places work into.

    ``EXISTING`` means the caller owns the target and names it: the runtime
    must place the execution exactly there and never choose a different slot.
    ``PROVISION`` means the caller names no target and the runtime allocates a
    fresh one, which the control plane is then responsible for tracking.
    """

    EXISTING = "existing"
    PROVISION = "provision"


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
    """A request to start one execution in a workspace.

    ``target`` names an addressable execution slot owned by a runtime, and
    ``placement`` says who owns that slot; the two are validated together by
    :func:`validate_placement`. ``EXISTING``
    requires a caller-supplied ``target``; ``PROVISION`` requires it to be
    absent because the runtime allocates one the control plane will track.
    Leaving both unset means unspecified, and a runtime that needs a target
    answers ``UNAVAILABLE`` rather than choosing one. ``agent_kind`` is an
    opaque, runtime-defined type for the execution, kept separate from
    ``argv`` so the trailing command is never silently read as a kind; a
    runtime with no notion of kind leaves it ``None`` and ``argv`` carries the
    whole command.
    """

    workspace_id: WorkspaceId
    argv: tuple[str, ...]
    label: str | None = None
    target: TargetRef | None = None
    placement: Placement | None = None
    agent_kind: str | None = None


class InvalidStartRequest(ValueError):
    """A start request whose placement fields contradict each other.

    This is a programmer error in the request, not a runtime condition, so it
    fails loudly with a named exception instead of returning a partially
    valid request. ``rule`` names the exact rule that was violated.
    """

    def __init__(self, rule: str, detail: str) -> None:
        self.rule = rule
        super().__init__(f"{rule}: {detail}")


def validate_placement(
    placement: Placement | None,
    target: TargetRef | None,
    agent_kind: str | None,
) -> None:
    """Validate a start request's placement fields, refusing contradictions.

    The combinations are exhaustive and explicit; none is silently
    reinterpreted, defaulted, or coerced:

    * ``placement=None`` with ``target=None`` is valid and means unspecified,
      so a runtime that needs a target answers ``UNAVAILABLE`` rather than
      choosing one;
    * ``EXISTING`` requires a non-``None`` ``target`` because the caller owns
      the slot;
    * ``PROVISION`` requires ``target is None`` because the runtime allocates
      the slot and the control plane tracks it, so naming one as well is
      contradictory;
    * a ``target`` without an explicit ``placement`` is refused: the core
      never guesses that it implies ``EXISTING``.

    ``agent_kind`` is shape-checked only: when set it must be a non-empty
    string. Its value is opaque to the core and is never matched against any
    runtime's allowlist, which is the runtime's own business.

    Returns ``None`` for a well-formed request. Otherwise it raises
    :class:`InvalidStartRequest` naming the violated rule.
    """

    if placement is None:
        if target is not None:
            raise InvalidStartRequest(
                "target_without_placement",
                "a target requires an explicit placement",
            )
    elif placement is Placement.EXISTING:
        if target is None:
            raise InvalidStartRequest(
                "existing_requires_target",
                "placement EXISTING requires a target",
            )
    elif placement is Placement.PROVISION:
        if target is not None:
            raise InvalidStartRequest(
                "provision_forbids_target",
                "placement PROVISION requires target to be None",
            )
    else:
        raise InvalidStartRequest(
            "unknown_placement", f"unrecognized placement: {placement!r}"
        )
    if agent_kind is not None and not (
        isinstance(agent_kind, str) and agent_kind
    ):
        raise InvalidStartRequest(
            "agent_kind", "agent_kind must be a non-empty string when set"
        )


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
