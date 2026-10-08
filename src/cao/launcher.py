"""Gated launcher: turn a start request into an execution attempt.

Launching a worker is an authority-sensitive action, so the order of checks is
the point of this module:

1. the request must be a well formed :class:`StartRequest`;
2. policy must authorize the exact start action;
3. the runtime plugin must advertise :data:`Capability.START`;
4. only then is the injected spawner invoked.

A refusal or an unsupported capability therefore produces exactly zero spawns
and an explicit ``UNAVAILABLE`` :class:`CapabilityResult` with ``value=None``.
There is no fallback runtime, no retry ladder and no queue: a single attempt is
made behind the injected spawner callable, so library code never calls
``subprocess``, ``os.fork`` or ``Popen`` itself and never fabricates a result.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .contracts import (
    Capability,
    CapabilityResult,
    ExecutionRef,
    StartRequest,
)
from .evidence import (
    EvidenceSource,
    Verdict,
    build_evidence,
    fingerprint,
)
from .policy import ActionKind, Grant, PolicyDecision

__all__ = ["Launcher", "LauncherError", "Spawner"]

Clock = Callable[[], datetime]
Spawner = Callable[[StartRequest], "ExecutionRef | None"]


class LauncherError(Exception):
    """Raised when a launcher is constructed with invalid dependencies."""

    code = "LAUNCHER_ERROR"


class Launcher:
    """One runtime's launch path, with policy and capability gates.

    ``capabilities`` is the runtime plugin's advertised capability set;
    ``spawner`` is the only thing that may create a process and is supplied by
    the caller (an adapter over a concrete runtime), never imported here.
    """

    def __init__(
        self,
        *,
        plugin_id: str,
        capabilities: object,
        spawner: object,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(plugin_id, str) or not plugin_id:
            raise LauncherError("plugin_id must be a non-empty string")
        try:
            capability_set = frozenset(capabilities)  # type: ignore[arg-type]
        except TypeError as exc:
            raise LauncherError(
                f"capabilities must be iterable, got {type(capabilities).__name__}"
            ) from exc
        if not all(isinstance(cap, Capability) for cap in capability_set):
            raise LauncherError("capabilities must contain only Capability values")
        if not callable(spawner):
            raise LauncherError("spawner must be callable")
        self._plugin_id = plugin_id
        self._capabilities = capability_set
        self._spawner = spawner
        self._clock = clock

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self._capabilities

    def launch(
        self, request: StartRequest, decision: PolicyDecision
    ) -> CapabilityResult[ExecutionRef]:
        """Attempt one start, checking policy and capability before acting."""

        if not isinstance(request, StartRequest):
            return self._unavailable(
                Verdict.INVALID,
                EvidenceSource.RUNTIME,
                f"start requires a StartRequest, got {type(request).__name__}",
            )
        if not isinstance(decision, PolicyDecision):
            return self._unavailable(
                Verdict.INVALID,
                EvidenceSource.POLICY,
                f"start requires a PolicyDecision, got {type(decision).__name__}",
            )

        # Gate 1: authority. Refuse before anything is spawned. An allowed
        # decision is not enough on its own: it must be internally consistent
        # and must authorize this exact action on this plugin and workspace.
        if not decision.allowed:
            policy_verdict = decision.evidence.verdict.value
            policy_reason = decision.evidence.reason or "no matching grant"
            return self._unavailable(
                Verdict.UNAVAILABLE,
                EvidenceSource.POLICY,
                f"start not authorized (policy verdict {policy_verdict}): "
                f"{policy_reason}",
            )
        if decision.evidence.verdict is not Verdict.SUPPORTED:
            return self._unavailable(
                Verdict.INVALID,
                EvidenceSource.POLICY,
                "policy decision is contradictory: allowed but its evidence "
                f"verdict is {decision.evidence.verdict.value}",
            )
        matched = decision.matched
        if matched is None:
            return self._unavailable(
                Verdict.INVALID,
                EvidenceSource.POLICY,
                "policy decision is contradictory: allowed with no matching grant",
            )
        if not isinstance(matched, Grant):
            return self._unavailable(
                Verdict.INVALID,
                EvidenceSource.POLICY,
                f"policy matched grant must be a Grant, got "
                f"{type(matched).__name__}",
            )
        if matched.kind is not ActionKind.START:
            return self._unavailable(
                Verdict.UNAVAILABLE,
                EvidenceSource.POLICY,
                f"policy grant kind {matched.kind.value!r} does not authorize "
                f"{ActionKind.START.value!r}",
            )
        if matched.plugin_id != self._plugin_id:
            return self._unavailable(
                Verdict.UNAVAILABLE,
                EvidenceSource.POLICY,
                f"policy grant plugin_id {matched.plugin_id!r} does not match "
                f"launcher plugin {self._plugin_id!r}",
            )
        if matched.workspace_id != request.workspace_id:
            return self._unavailable(
                Verdict.UNAVAILABLE,
                EvidenceSource.POLICY,
                f"policy grant workspace_id {matched.workspace_id!r} does not "
                f"match requested workspace {request.workspace_id!r}",
            )

        # Gate 2: capability truth. An unadvertised capability is unavailable,
        # not something to attempt and hope for.
        if Capability.START not in self._capabilities:
            return self._unavailable(
                Verdict.UNAVAILABLE,
                EvidenceSource.RUNTIME,
                f"runtime plugin {self._plugin_id!r} does not advertise "
                f"capability {Capability.START.value!r}",
            )

        # Both gates passed: act exactly once.
        try:
            spawned = self._spawner(request)
        except Exception as exc:  # noqa: BLE001 - fail closed, never leak
            return self._unavailable(
                Verdict.UNKNOWN,
                EvidenceSource.RUNTIME,
                f"spawner raised {type(exc).__name__}: {exc}",
            )
        if spawned is None:
            return self._unavailable(
                Verdict.UNKNOWN,
                EvidenceSource.RUNTIME,
                "spawner produced no execution reference",
            )
        problem = self._validate_ref(request, spawned)
        if problem is not None:
            return self._unavailable(
                Verdict.INVALID, EvidenceSource.RUNTIME, problem
            )
        return CapabilityResult(
            capability=Capability.START,
            evidence=build_evidence(
                EvidenceSource.RUNTIME,
                Verdict.SUPPORTED,
                fingerprint_value=fingerprint(spawned),
                clock=self._clock,
            ),
            value=spawned,
        )

    def _validate_ref(self, request: StartRequest, spawned: object) -> str | None:
        if not isinstance(spawned, ExecutionRef):
            return (
                f"spawner must return an ExecutionRef, got "
                f"{type(spawned).__name__}"
            )
        if spawned.runtime_id != self._plugin_id:
            return (
                f"execution runtime_id {spawned.runtime_id!r} does not match "
                f"plugin id {self._plugin_id!r}"
            )
        if spawned.workspace_id != request.workspace_id:
            return (
                f"execution workspace_id {spawned.workspace_id!r} does not "
                f"match requested workspace {request.workspace_id!r}"
            )
        if not isinstance(spawned.execution_id, str) or not spawned.execution_id:
            return "execution execution_id must be a non-empty string"
        return None

    def _unavailable(
        self, verdict: Verdict, source: EvidenceSource, reason: str
    ) -> CapabilityResult[ExecutionRef]:
        return CapabilityResult(
            capability=Capability.START,
            evidence=build_evidence(
                source, verdict, reason=reason, clock=self._clock
            ),
            value=None,
        )
