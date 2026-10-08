"""Runtime execution interface and explicit plugin selection.

CAO is runtime neutral. A runtime integrates by providing a
:class:`RuntimePlugin`. Plugins are selected explicitly by id through a
:class:`PluginRegistry`; there is no discovery-then-guess fallback and no
built-in runtime.

:class:`RuntimeClient` is the only surface the rest of CAO uses to reach a
runtime. Before each operation it checks the plugin's declared capabilities.
If a capability is not offered, the client returns an explicit
``UNAVAILABLE`` :class:`~cao.contracts.CapabilityResult` instead of calling
unknown code or inventing a result.

Runtime execution is a distinct concern from Work Registry ownership,
reconciliation, and policy/authority. This module intentionally has no
awareness of any of them.
"""

from __future__ import annotations

import importlib.metadata
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable

from .contracts import (
    Capability,
    CapabilityResult,
    CloseAck,
    ExecutionRef,
    Observation,
    PromptAck,
    RuntimeDescriptor,
    SessionRef,
    Snapshot,
    StartRequest,
    TargetInspection,
    TargetRef,
    UnsupportedCapability,
    WorkspaceId,
)
from .evidence import (
    EvidenceError,
    EvidenceSource,
    Verdict,
    build_evidence,
    fingerprint,
    utc_now,
    validate_coverage,
)

__all__ = [
    "RuntimePlugin",
    "PluginRegistry",
    "PluginEntry",
    "PluginError",
    "PluginNotFound",
    "RuntimeClient",
    "DEFAULT_ENTRY_POINT_GROUP",
]

DEFAULT_ENTRY_POINT_GROUP = "cao.runtime_plugins"
PluginFactory = Callable[[], "RuntimePlugin"]


class PluginError(Exception):
    """Base class for plugin selection and loading failures."""

    code = "PLUGIN_ERROR"


class PluginNotFound(PluginError):
    """Raised when an explicitly requested plugin id is not registered."""

    code = "PLUGIN_NOT_FOUND"


class RuntimePlugin(ABC):
    """Base class every runtime integration implements.

    Subclasses declare their identity, version and capabilities and override
    only the capability methods they actually support. Unsupported methods
    keep the inherited behavior of raising :class:`UnsupportedCapability`.
    """

    runtime_name: str = ""
    runtime_version: str = "0"

    @property
    @abstractmethod
    def plugin_id(self) -> str:
        """Stable id used for explicit selection."""

    @abstractmethod
    def capabilities(self) -> frozenset[Capability]:
        """The capabilities this plugin explicitly offers."""

    def describe(self) -> RuntimeDescriptor:
        """Capability discovery: a plugin's self-description."""

        return RuntimeDescriptor(
            id=self.plugin_id,
            name=self.runtime_name or self.plugin_id,
            version=self.runtime_version,
            capabilities=frozenset(self.capabilities()),
        )

    def snapshot(self, workspace_id: WorkspaceId) -> Snapshot:
        raise UnsupportedCapability(Capability.SNAPSHOT)

    def inspect(self, target: TargetRef) -> TargetInspection:
        raise UnsupportedCapability(Capability.INSPECT)

    def observe(self, execution: ExecutionRef) -> Observation:
        raise UnsupportedCapability(Capability.OBSERVE)

    def start(self, request: StartRequest) -> ExecutionRef:
        raise UnsupportedCapability(Capability.START)

    def prompt(self, session: SessionRef, text: str) -> PromptAck:
        raise UnsupportedCapability(Capability.PROMPT)

    def close(self, session: SessionRef) -> CloseAck:
        raise UnsupportedCapability(Capability.CLOSE)


@dataclass(frozen=True)
class PluginEntry:
    """One registered plugin as reported by ``plugins list``."""

    id: str
    descriptor: RuntimeDescriptor | None
    available: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "available": self.available,
            "descriptor": self.descriptor.as_dict() if self.descriptor else None,
            "reason": self.reason,
        }


class PluginRegistry:
    """Explicit plugin selection.

    Plugins are registered by id. Resolution never falls back to a default or
    a similarly named plugin: an unknown id raises :class:`PluginNotFound`.
    """

    def __init__(self) -> None:
        self._factories: dict[str, PluginFactory] = {}
        self._conflicts: dict[str, str] = {}

    def register(
        self, plugin_id: str, factory: PluginFactory, *, replace: bool = False
    ) -> None:
        if not plugin_id:
            raise PluginError("plugin id must be non-empty")
        if (plugin_id in self._factories or plugin_id in self._conflicts) and not replace:
            raise PluginError(f"plugin already registered: {plugin_id}")
        self._conflicts.pop(plugin_id, None)
        self._factories[plugin_id] = factory

    def register_instance(self, plugin: RuntimePlugin, *, replace: bool = False) -> None:
        self.register(plugin.plugin_id, lambda: plugin, replace=replace)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(set(self._factories) | set(self._conflicts)))

    def __contains__(self, plugin_id: object) -> bool:
        return plugin_id in self._factories or plugin_id in self._conflicts

    def resolve(self, plugin_id: str) -> RuntimePlugin:
        """Return the plugin for ``plugin_id`` or raise ``PluginError``.

        An unknown id raises :class:`PluginNotFound`. An id that is present but
        ambiguous (two providers claimed it) fails closed, as does a factory
        that returns a non-plugin or a plugin whose identity differs from the
        explicitly selected id.
        """

        if plugin_id in self._conflicts:
            raise PluginError(
                f"plugin id is ambiguous and cannot be selected: {plugin_id!r} "
                f"({self._conflicts[plugin_id]})"
            )
        factory = self._factories.get(plugin_id)
        if factory is None:
            known = ", ".join(self.ids()) or "none"
            raise PluginNotFound(
                f"plugin not registered: {plugin_id!r} (registered: {known})"
            )
        plugin = factory()
        if not isinstance(plugin, RuntimePlugin):
            raise PluginError(
                f"plugin factory for {plugin_id!r} did not return a "
                f"RuntimePlugin: {type(plugin).__name__}"
            )
        if plugin.plugin_id != plugin_id:
            raise PluginError(
                f"plugin identity mismatch: registered id {plugin_id!r}, "
                f"plugin reports {plugin.plugin_id!r}"
            )
        return plugin

    def entries(self) -> list[PluginEntry]:
        """Describe every registered plugin, reporting load failures honestly."""

        result: list[PluginEntry] = []
        for plugin_id in self.ids():
            try:
                descriptor = self.resolve(plugin_id).describe()
            except Exception as exc:  # noqa: BLE001 - surface, never hide
                result.append(
                    PluginEntry(
                        id=plugin_id,
                        descriptor=None,
                        available=False,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                )
            else:
                result.append(
                    PluginEntry(id=plugin_id, descriptor=descriptor, available=True)
                )
        return result

    @classmethod
    def discover(
        cls,
        group: str = DEFAULT_ENTRY_POINT_GROUP,
        *,
        entry_points: Iterable[Any] | None = None,
    ) -> "PluginRegistry":
        """Build a registry from installed entry points in ``group``.

        With no installed plugins this returns an empty registry, which is an
        honest result rather than an error. A plugin that fails to load is not
        dropped: it is reported as unavailable by :meth:`entries`.
        """

        registry = cls()
        for entry_point in _select_entry_points(group, entry_points):
            name = entry_point.name

            def factory(entry_point: Any = entry_point) -> RuntimePlugin:
                loaded = entry_point.load()
                if isinstance(loaded, type):
                    loaded = loaded()
                return loaded

            if name in registry._factories or name in registry._conflicts:
                # Two providers claim one id: refuse selection entirely rather
                # than keeping a first winner that install order can change.
                registry._factories.pop(name, None)
                registry._conflicts[name] = (
                    "ambiguous plugin id discovered from multiple entry points"
                )
                continue
            try:
                registry.register(name, factory)
            except PluginError as exc:
                # A malformed entry point id stays visible as unavailable.
                registry._conflicts[name] = f"{type(exc).__name__}: {exc}"
        return registry


def _select_entry_points(
    group: str, entry_points: Iterable[Any] | None
) -> list[Any]:
    if entry_points is not None:
        return list(entry_points)
    discovered = importlib.metadata.entry_points()
    if hasattr(discovered, "select"):
        return list(discovered.select(group=group))
    return list(discovered.get(group, []))  # pragma: no cover - legacy API


class RuntimeClient:
    """Runtime-neutral access to one explicitly selected plugin.

    The client never silently substitutes another runtime. Each operation
    returns a :class:`CapabilityResult` whose evidence records the source,
    verdict, capture time, fingerprint, coverage and reason.
    """

    def __init__(
        self,
        plugin: RuntimePlugin,
        *,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self._plugin = plugin
        self._clock = clock

    @property
    def plugin(self) -> RuntimePlugin:
        return self._plugin

    @property
    def plugin_id(self) -> str:
        return self._plugin.plugin_id

    def discover(self) -> CapabilityResult[RuntimeDescriptor]:
        return self._invoke(
            Capability.DISCOVERY,
            lambda: self._plugin.describe(),
            validate=lambda value: _validate_descriptor(self.plugin_id, value),
        )

    def snapshot(self, workspace_id: WorkspaceId) -> CapabilityResult[Snapshot]:
        return self._invoke(
            Capability.SNAPSHOT,
            lambda: self._plugin.snapshot(workspace_id),
            validate=lambda snap: _validate_snapshot(
                self.plugin_id, workspace_id, snap
            ),
            fingerprint_of=lambda snap: snap.fingerprint,
            capture_time_of=lambda snap: snap.captured_at,
            coverage_of=lambda snap: snap.coverage,
        )

    def inspect(self, target: TargetRef) -> CapabilityResult[TargetInspection]:
        rejection = self._check_reference(Capability.INSPECT, target, TargetRef)
        if rejection is not None:
            return rejection
        return self._invoke(
            Capability.INSPECT,
            lambda: self._plugin.inspect(target),
            validate=lambda value: _validate_inspection(
                self.plugin_id, target, value
            ),
        )

    def observe(self, execution: ExecutionRef) -> CapabilityResult[Observation]:
        rejection = self._check_reference(
            Capability.OBSERVE, execution, ExecutionRef
        )
        if rejection is not None:
            return rejection
        return self._invoke(
            Capability.OBSERVE,
            lambda: self._plugin.observe(execution),
            validate=lambda value: _validate_observation(
                self.plugin_id, execution, value
            ),
        )

    def start(self, request: StartRequest) -> CapabilityResult[ExecutionRef]:
        if not isinstance(request, StartRequest):
            return self._invalid_input(
                Capability.START,
                f"start requires a StartRequest, got {type(request).__name__}",
            )
        return self._invoke(
            Capability.START,
            lambda: self._plugin.start(request),
            validate=lambda value: _validate_execution_ref(
                self.plugin_id, request, value
            ),
        )

    def prompt(self, session: SessionRef, text: str) -> CapabilityResult[PromptAck]:
        rejection = self._check_reference(Capability.PROMPT, session, SessionRef)
        if rejection is not None:
            return rejection
        return self._invoke(
            Capability.PROMPT,
            lambda: self._plugin.prompt(session, text),
            validate=lambda value: _validate_prompt_ack(session, value),
        )

    def close(self, session: SessionRef) -> CapabilityResult[CloseAck]:
        rejection = self._check_reference(Capability.CLOSE, session, SessionRef)
        if rejection is not None:
            return rejection
        return self._invoke(
            Capability.CLOSE,
            lambda: self._plugin.close(session),
            validate=lambda value: _validate_close_ack(session, value),
        )

    # -- internals ---------------------------------------------------------

    def _check_reference(
        self,
        capability: Capability,
        reference: Any,
        expected_type: type,
    ) -> CapabilityResult[Any] | None:
        """Reject a reference bound to a different runtime before any call."""

        if not isinstance(reference, expected_type):
            return self._invalid_input(
                capability,
                f"{expected_type.__name__} required, got "
                f"{type(reference).__name__}",
            )
        runtime_id = reference.runtime_id
        if runtime_id != self.plugin_id:
            return self._invalid_input(
                capability,
                f"reference runtime_id {runtime_id!r} does not match client "
                f"plugin {self.plugin_id!r}",
            )
        return None

    def _requirements_met(self, capability: Capability) -> bool:
        return capability in self._plugin.capabilities()

    def _invoke(
        self,
        capability: Capability,
        call: Callable[[], Any],
        *,
        validate: Callable[[Any], str | None] | None = None,
        fingerprint_of: Callable[[Any], str] | None = None,
        capture_time_of: Callable[[Any], str] | None = None,
        coverage_of: Callable[[Any], float] | None = None,
    ) -> CapabilityResult[Any]:
        if not self._requirements_met(capability):
            return self._unavailable(capability)
        try:
            value = call()
        except UnsupportedCapability:
            # Defense in depth: declared-but-unimplemented stays explicit.
            return self._unavailable(capability)
        if value is None:
            return CapabilityResult(
                capability=capability,
                evidence=build_evidence(
                    EvidenceSource.PLUGIN,
                    verdict=Verdict.UNKNOWN,
                    reason=(
                        f"plugin {self.plugin_id!r} returned no value for "
                        f"capability {capability.value!r}"
                    ),
                    clock=self._clock,
                ),
                value=None,
            )
        if validate is not None:
            problem = validate(value)
            if problem is not None:
                return self._malformed(capability, problem)
        return self._supported(
            capability,
            value,
            fingerprint_of=fingerprint_of,
            capture_time_of=capture_time_of,
            coverage_of=coverage_of,
        )

    def _supported(
        self,
        capability: Capability,
        value: Any,
        *,
        fingerprint_of: Callable[[Any], str] | None = None,
        capture_time_of: Callable[[Any], str] | None = None,
        coverage_of: Callable[[Any], float] | None = None,
    ) -> CapabilityResult[Any]:
        computed_fingerprint = (
            fingerprint_of(value) if fingerprint_of else fingerprint(value)
        )
        capture_time = capture_time_of(value) if capture_time_of else None
        coverage = coverage_of(value) if coverage_of else None
        return CapabilityResult(
            capability=capability,
            evidence=build_evidence(
                EvidenceSource.PLUGIN,
                verdict=Verdict.SUPPORTED,
                fingerprint_value=computed_fingerprint,
                coverage=coverage,
                # A plugin-supplied capture time is used exactly; only
                # operations without one get the client's capture time.
                capture_time=capture_time or utc_now(self._clock),
            ),
            value=value,
        )

    def _invalid_input(self, capability: Capability, reason: str) -> CapabilityResult[Any]:
        """A caller-supplied reference or request the client must not act on."""

        return CapabilityResult(
            capability=capability,
            evidence=build_evidence(
                EvidenceSource.RUNTIME,
                verdict=Verdict.INVALID,
                reason=reason,
                clock=self._clock,
            ),
            value=None,
        )

    def _malformed(self, capability: Capability, reason: str) -> CapabilityResult[Any]:
        """A plugin returned a value that violates the declared contract."""

        return CapabilityResult(
            capability=capability,
            evidence=build_evidence(
                EvidenceSource.PLUGIN,
                verdict=Verdict.INVALID,
                reason=(
                    f"plugin {self.plugin_id!r} returned a malformed result for "
                    f"capability {capability.value!r}: {reason}"
                ),
                clock=self._clock,
            ),
            value=None,
        )

    def _unavailable(self, capability: Capability) -> CapabilityResult[Any]:
        return CapabilityResult(
            capability=capability,
            evidence=build_evidence(
                EvidenceSource.PLUGIN,
                verdict=Verdict.UNAVAILABLE,
                reason=(
                    f"plugin {self.plugin_id!r} does not provide capability "
                    f"{capability.value!r}"
                ),
                clock=self._clock,
            ),
            value=None,
        )


# -- result validation -----------------------------------------------------
# A plugin result is positive evidence only when it is the declared type and
# bound to the exact runtime/workspace/execution/session it was requested for.


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _is_rfc3339_timestamp(value: Any) -> bool:
    """True only for a non-empty RFC 3339 timestamp with an explicit timezone.

    A trailing ``Z``/``z`` is accepted as UTC; an explicit numeric offset is
    accepted. Naive (offsetless) timestamps and arbitrary strings are rejected.
    """

    if not _non_empty_str(value):
        return False
    # RFC 3339 mandates the 'T' date/time separator (Python's parser is more
    # permissive and also accepts a space).
    if "T" not in value and "t" not in value:
        return False
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _validate_descriptor(plugin_id: str, value: Any) -> str | None:
    if not isinstance(value, RuntimeDescriptor):
        return f"discovery must return RuntimeDescriptor, got {type(value).__name__}"
    if value.id != plugin_id:
        return f"descriptor id {value.id!r} does not match plugin id {plugin_id!r}"
    if not isinstance(value.name, str) or not isinstance(value.version, str):
        return "descriptor name and version must be strings"
    capabilities = value.capabilities
    if not isinstance(capabilities, frozenset) or not all(
        isinstance(capability, Capability) for capability in capabilities
    ):
        return "descriptor capabilities must be a frozenset of Capability"
    return None


def _validate_snapshot(
    plugin_id: str, workspace_id: WorkspaceId, value: Any
) -> str | None:
    if not isinstance(value, Snapshot):
        return f"snapshot must return Snapshot, got {type(value).__name__}"
    if value.runtime_id != plugin_id:
        return (
            f"snapshot runtime_id {value.runtime_id!r} does not match plugin "
            f"id {plugin_id!r}"
        )
    if value.workspace_id != workspace_id:
        return (
            f"snapshot workspace_id {value.workspace_id!r} does not match "
            f"requested workspace {workspace_id!r}"
        )
    if not _is_rfc3339_timestamp(value.captured_at):
        return (
            "snapshot captured_at must be a valid RFC 3339 timestamp with an "
            "explicit timezone"
        )
    if not _non_empty_str(value.fingerprint):
        return "snapshot fingerprint must be a non-empty string"
    try:
        coverage = validate_coverage(value.coverage)
    except EvidenceError as exc:
        return f"snapshot coverage is invalid: {exc}"
    if coverage is None:
        return "snapshot coverage must be a finite number within [0, 1]"
    return None


def _validate_inspection(
    plugin_id: str, target: TargetRef, value: Any
) -> str | None:
    if not isinstance(value, TargetInspection):
        return f"inspect must return TargetInspection, got {type(value).__name__}"
    if value.runtime_id != plugin_id:
        return (
            f"inspection runtime_id {value.runtime_id!r} does not match plugin "
            f"id {plugin_id!r}"
        )
    if value.workspace_id != target.workspace_id:
        return (
            f"inspection workspace_id {value.workspace_id!r} does not match "
            f"requested workspace {target.workspace_id!r}"
        )
    if value.target_id != target.target_id:
        return (
            f"inspection target_id {value.target_id!r} does not match "
            f"requested target {target.target_id!r}"
        )
    return None


def _validate_observation(
    plugin_id: str, execution: ExecutionRef, value: Any
) -> str | None:
    if not isinstance(value, Observation):
        return f"observe must return Observation, got {type(value).__name__}"
    if value.runtime_id != plugin_id:
        return (
            f"observation runtime_id {value.runtime_id!r} does not match plugin "
            f"id {plugin_id!r}"
        )
    if value.workspace_id != execution.workspace_id:
        return (
            f"observation workspace_id {value.workspace_id!r} does not match "
            f"requested workspace {execution.workspace_id!r}"
        )
    if value.execution_id != execution.execution_id:
        return (
            f"observation execution_id {value.execution_id!r} does not match "
            f"requested execution {execution.execution_id!r}"
        )
    return None


def _validate_execution_ref(
    plugin_id: str, request: StartRequest, value: Any
) -> str | None:
    if not isinstance(value, ExecutionRef):
        return f"start must return ExecutionRef, got {type(value).__name__}"
    if value.runtime_id != plugin_id:
        return (
            f"execution runtime_id {value.runtime_id!r} does not match plugin "
            f"id {plugin_id!r}"
        )
    if value.workspace_id != request.workspace_id:
        return (
            f"execution workspace_id {value.workspace_id!r} does not match "
            f"requested workspace {request.workspace_id!r}"
        )
    if not _non_empty_str(value.execution_id):
        return "execution execution_id must be a non-empty string"
    return None


def _validate_prompt_ack(session: SessionRef, value: Any) -> str | None:
    if not isinstance(value, PromptAck):
        return f"prompt must return PromptAck, got {type(value).__name__}"
    if value.session_id != session.session_id:
        return (
            f"prompt ack session_id {value.session_id!r} does not match "
            f"requested session {session.session_id!r}"
        )
    if not isinstance(value.accepted, bool):
        return "prompt ack accepted must be a boolean"
    return None


def _validate_close_ack(session: SessionRef, value: Any) -> str | None:
    if not isinstance(value, CloseAck):
        return f"close must return CloseAck, got {type(value).__name__}"
    if value.session_id != session.session_id:
        return (
            f"close ack session_id {value.session_id!r} does not match "
            f"requested session {session.session_id!r}"
        )
    if not isinstance(value.closed, bool):
        return "close ack closed must be a boolean"
    return None
