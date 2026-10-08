"""Authorization checks for the exact requested action.

Policy answers one question: is this precise action authorized? It matches a
request against a set of grants field by field. A grant to start one runtime
in one workspace does not authorize closing it, prompting another workspace,
or acting on a different plugin.

The engine is deny-by-default. With no matching grant the decision is
``REFUSED``. When the policy source itself cannot be read or understood, the
engine fails closed: every decision is ``UNKNOWN`` and denied, never allowed.

Policy is an authority concern, separate from runtime execution, state I/O,
Work Registry ownership, and reconciliation. This module depends only on the
generic evidence primitives and the shared state reader.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .evidence import (
    Evidence,
    EvidenceSource,
    Verdict,
    build_evidence,
    fingerprint,
)
from .state import StateError, read_state

__all__ = [
    "ActionKind",
    "ActionRequest",
    "Grant",
    "PolicyDecision",
    "PolicyEngine",
]

# The exact fields a grant may carry. Anything else is a misspelling and must
# make the whole policy unreadable rather than silently narrowing authority.
_GRANT_KEYS = frozenset(
    {"kind", "plugin_id", "workspace_id", "execution_id", "session_id"}
)


class ActionKind(str, Enum):
    """The exact action being authorized.

    Values mirror the generic runtime capabilities so a grant reads the same
    as the capability it covers, while keeping policy independent of the
    runtime contract module.
    """

    DISCOVERY = "capability.discovery"
    SNAPSHOT = "snapshot.capture"
    INSPECT = "target.inspect"
    OBSERVE = "output.observe"
    START = "execution.start"
    PROMPT = "session.prompt"
    CLOSE = "session.close"


@dataclass(frozen=True)
class ActionRequest:
    """One precise action awaiting authorization."""

    kind: ActionKind
    plugin_id: str
    workspace_id: str
    execution_id: str | None = None
    session_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "plugin_id": self.plugin_id,
            "workspace_id": self.workspace_id,
            "execution_id": self.execution_id,
            "session_id": self.session_id,
        }


@dataclass(frozen=True)
class Grant:
    """An authorization for a precise action.

    ``execution_id`` and ``session_id`` are optional narrowings: when set they
    must match exactly; when unset the grant covers any execution/session in
    the named workspace for the named action.
    """

    kind: ActionKind
    plugin_id: str
    workspace_id: str
    execution_id: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class PolicyDecision:
    """The result of an authorization check."""

    allowed: bool
    evidence: Evidence
    matched: Grant | None = None


def _grant_matches(grant: Grant, request: ActionRequest) -> bool:
    if grant.kind is not request.kind:
        return False
    if grant.plugin_id != request.plugin_id:
        return False
    if grant.workspace_id != request.workspace_id:
        return False
    if grant.execution_id is not None and grant.execution_id != request.execution_id:
        return False
    if grant.session_id is not None and grant.session_id != request.session_id:
        return False
    return True


class PolicyEngine:
    """A deny-by-default authorization engine."""

    def __init__(
        self,
        grants: Iterable[Grant] = (),
        *,
        unreadable_reason: str | None = None,
    ) -> None:
        self._grants = tuple(grants)
        self._unreadable_reason = unreadable_reason

    @property
    def grants(self) -> tuple[Grant, ...]:
        return self._grants

    @property
    def readable(self) -> bool:
        return self._unreadable_reason is None

    def authorize(self, request: ActionRequest) -> PolicyDecision:
        """Authorize ``request`` against the loaded grants, exactly."""

        request_fingerprint = fingerprint(request.as_dict())
        if self._unreadable_reason is not None:
            return PolicyDecision(
                allowed=False,
                evidence=build_evidence(
                    EvidenceSource.POLICY,
                    verdict=Verdict.UNKNOWN,
                    reason=f"policy is unreadable: {self._unreadable_reason}",
                    fingerprint_value=request_fingerprint,
                ),
                matched=None,
            )
        for grant in self._grants:
            if _grant_matches(grant, request):
                return PolicyDecision(
                    allowed=True,
                    evidence=build_evidence(
                        EvidenceSource.POLICY,
                        verdict=Verdict.SUPPORTED,
                        reason="authorized by exact grant",
                        fingerprint_value=request_fingerprint,
                    ),
                    matched=grant,
                )
        return PolicyDecision(
            allowed=False,
            evidence=build_evidence(
                EvidenceSource.POLICY,
                verdict=Verdict.REFUSED,
                reason="no grant matches the requested action",
                fingerprint_value=request_fingerprint,
            ),
            matched=None,
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "PolicyEngine":
        """Build an engine from a parsed policy object, failing closed."""

        try:
            grants = _parse_grants(payload)
        except ValueError as exc:
            return cls(unreadable_reason=str(exc))
        return cls(grants)

    @classmethod
    def load(cls, path: str | Any) -> "PolicyEngine":
        """Load a policy file, failing closed on any unreadable input.

        An absent file yields a deny-all engine (no grants). A present but
        corrupt, unreadable, or schema-invalid file yields an engine whose
        every decision is ``UNKNOWN`` and denied.
        """

        try:
            data = read_state(path)
        except StateError as exc:
            return cls(unreadable_reason=f"{exc.code}: {exc}")
        if data is None:
            return cls()
        return cls.from_mapping(data)


def _parse_grants(payload: Mapping[str, Any]) -> tuple[Grant, ...]:
    if not isinstance(payload, Mapping):
        raise ValueError(f"policy must be an object, got {type(payload).__name__}")
    if "grants" not in payload:
        raise ValueError("policy is missing the required 'grants' key")
    raw_grants = payload["grants"]
    if not isinstance(raw_grants, list):
        raise ValueError("policy 'grants' must be a list")

    grants: list[Grant] = []
    for index, raw in enumerate(raw_grants):
        if not isinstance(raw, Mapping):
            raise ValueError(f"grant #{index} must be an object")
        unknown = set(raw) - _GRANT_KEYS
        if unknown:
            fields = ", ".join(sorted(map(str, unknown)))
            raise ValueError(f"grant #{index} has unsupported field(s): {fields}")
        kind_value = raw.get("kind")
        try:
            kind = ActionKind(kind_value)
        except ValueError as exc:
            raise ValueError(f"grant #{index} has unknown kind {kind_value!r}") from exc
        for required in ("plugin_id", "workspace_id"):
            value = raw.get(required)
            if not isinstance(value, str) or not value:
                raise ValueError(f"grant #{index} requires a non-empty {required!r}")
        grants.append(
            Grant(
                kind=kind,
                plugin_id=raw["plugin_id"],
                workspace_id=raw["workspace_id"],
                execution_id=_optional_str(raw, "execution_id", index),
                session_id=_optional_str(raw, "session_id", index),
            )
        )
    return tuple(grants)


def _optional_str(raw: Mapping[str, Any], key: str, index: int) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"grant #{index} has an invalid {key!r}")
    return value
