"""Declarative automation manifest and owner-preference loader for CAO.

This module is a *loader*, not a scheduler. It parses and validates an
owner-authored automation manifest and the owner's preferences, and it does
nothing else: no timer, queue, cron entry, subprocess, background worker,
network call or clock read exists here, and nothing in the module ever fires
the cadence it records. ``schedule`` is DESCRIPTIVE: CAO records what the owner
says the cadence is, and an external scheduler the owner already runs is what
actually fires it.

Owner preferences are READ-ONLY to an agent. The owner edits them in the
governed system; an agent may read them and may never write, merge, default,
repair or silently substitute them. There is deliberately no write,
propose-in-place, merge or default-fill path anywhere in this module, so an
absent or corrupt preferences payload is refused rather than replaced with a
default.

The loader is strict and total. It refuses an unknown version, an unknown key,
a timezone ``zoneinfo`` cannot load, a literal secret-looking key or value, an
unknown sink id (only a local-file sink exists) and an unknown cost-measurement
id. Every failure raises :class:`SpecInvalid`, which names the offending field;
nothing is repaired, defaulted, or carried through. Every free-text field taken
from a source or from run state is wrapped in :class:`UntrustedData` and is data
only: no authority decision ever reads it, and this module imports no authority
module at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .evidence import Verdict, fingerprint

__all__ = [
    "SPEC_SCHEMA",
    "PREFERENCES_SCHEMA",
    "LOCAL_FILE_SINK",
    "KNOWN_COST_MEASUREMENTS",
    "MAX_SOURCES",
    "MAX_NOTES",
    "StopState",
    "SpecInvalid",
    "UntrustedData",
    "SourceSpec",
    "DestinationSpec",
    "ScheduleSpec",
    "Limits",
    "StopSpec",
    "AutomationSpec",
    "OwnerPreferences",
    "load_spec",
    "load_preferences",
]

# The schema discriminators. A payload whose ``version`` is anything else is
# refused outright; there is no migration and no best-effort path.
SPEC_SCHEMA = "cao.automation.spec.v0"
PREFERENCES_SCHEMA = "cao.automation.preferences.v0"

# The only sink that exists in v0. There is no default and no substitute: a
# manifest naming any other sink is refused, and no sink is ever contacted,
# opened or resolved at load time.
LOCAL_FILE_SINK = "local_file"

# The only cost-measurement ids CAO understands. An id outside this set is
# refused rather than assumed.
KNOWN_COST_MEASUREMENTS = frozenset({"none", "tokens", "seconds", "bytes", "calls"})

# Hard caps so every collection this loader walks has a finite, explicit bound
# rather than trusting the payload to be small.
MAX_SOURCES = 256
MAX_NOTES = 256

# Exactly the keys each object may carry. Anything else is a misspelling or a
# surprise, and must make the whole payload invalid rather than be ignored.
_MANIFEST_KEYS = frozenset(
    {
        "version",
        "name",
        "sources",
        "destination",
        "preferences_ref",
        "state_ref",
        "policy_ref",
        "timezone",
        "schedule",
        "limits",
        "stop",
    }
)
_SOURCE_KEYS = frozenset({"source_id", "kind", "locator", "text"})
_DESTINATION_KEYS = frozenset({"sink", "destination_id", "cost_measurement"})
_SCHEDULE_KEYS = frozenset({"cadence", "description"})
_LIMITS_KEYS = frozenset({"max_duration_seconds", "max_concurrency", "max_calls"})
_STOP_KEYS = frozenset({"state", "reason"})
_PREFERENCES_KEYS = frozenset({"version", "timezone", "notes"})

# Literal credential markers. A key or string value containing any of these is
# refused: the loader never accepts, echoes or carries a credential-looking
# literal. Both separator spellings are covered for the multi-word markers.
_SECRET_MARKERS = (
    "token",
    "secret",
    "password",
    "credential",
    "api_key",
    "api key",
    "authorization",
    "bearer",
    "private key",
    "private_key",
)

class SpecInvalid(Exception):
    """A manifest or preferences payload that must be refused, never repaired.

    ``field`` names the offending location, ``reason`` says why, and
    ``verdict`` is the :class:`~cao.evidence.Verdict` category (``INVALID`` for
    malformed input, ``REFUSED`` for a security refusal such as a
    credential-looking literal or an unknown sink).
    """

    code = "SPEC_INVALID"

    def __init__(
        self,
        field: str,
        reason: str,
        *,
        verdict: Verdict = Verdict.INVALID,
    ) -> None:
        self.verdict = verdict
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


@dataclass(frozen=True)
class UntrustedData:
    """Free text from a source or from run state, carried as DATA only.

    ``kind`` is ``"source_text"`` or ``"state_note"``. The ``fingerprint`` is a
    stable content fingerprint. Nothing in CAO interprets the text: it is never
    parsed as a directive and is never read by an authority decision.
    """

    kind: str
    text: str
    fingerprint: str


@dataclass(frozen=True)
class SourceSpec:
    """One declared input source.

    ``text`` is the source's free-text description, wrapped in
    :class:`UntrustedData`: it is data, never an instruction.
    """

    source_id: str
    kind: str
    locator: str
    text: UntrustedData


@dataclass(frozen=True)
class DestinationSpec:
    """Where an external scheduler's output would go.

    Only ``LOCAL_FILE_SINK`` exists in v0. The loader validates the id; it
    never opens, contacts or resolves the sink.
    """

    sink: str
    destination_id: str
    cost_measurement: str


@dataclass(frozen=True)
class ScheduleSpec:
    """A DESCRIPTIVE schedule. No scheduler is created from it.

    ``cadence`` and ``description`` describe what the owner's external
    scheduler is expected to do; CAO only records and validates them.
    """

    cadence: str
    description: str


@dataclass(frozen=True)
class Limits:
    """Explicit, positive, finite bounds on a run.

    ``max_duration_seconds``, ``max_concurrency`` and ``max_calls`` must each
    be a positive integer; an unbounded or non-positive value is refused.
    """

    max_duration_seconds: int
    max_concurrency: int
    max_calls: int


class StopState(str, Enum):
    """The explicit stop/pause/end state a manifest carries."""

    STOP = "stop"
    PAUSE = "pause"
    END = "end"


@dataclass(frozen=True)
class StopSpec:
    """The explicit stop/pause/end state and its owner-supplied reason."""

    state: StopState
    reason: str


@dataclass(frozen=True)
class AutomationSpec:
    """A validated, declarative automation manifest.

    ``preferences_ref`` is agent READ ONLY; ``state_ref`` is operational READ
    WRITE for the owning runtime; ``policy_ref`` names the authority source.
    None of the three is read or written by this loader.
    """

    version: str
    name: str
    sources: tuple[SourceSpec, ...]
    destination: DestinationSpec
    preferences_ref: str
    state_ref: str
    policy_ref: str
    timezone: str
    schedule: ScheduleSpec
    limits: Limits
    stop: StopSpec


@dataclass(frozen=True)
class OwnerPreferences:
    """The owner's preferences, loaded for reading only.

    This module never writes, merges, proposes or defaults these values. An
    agent receives them read-only; the owner edits them in the governed system.
    ``notes`` are free text, each wrapped in :class:`UntrustedData` as DATA.
    """

    version: str
    timezone: str
    notes: tuple[UntrustedData, ...]


def _require_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SpecInvalid(
            field, f"must be an object, got {type(value).__name__}"
        )
    return value


def _require_non_empty_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise SpecInvalid(field, "must be a non-empty string")
    return value


def _require_positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SpecInvalid(field, "must be a positive integer")
    return value


def _require_sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        raise SpecInvalid(
            field, f"must be a list, got {type(value).__name__}"
        )
    return value


def _reject_unknown_keys(
    raw: Mapping[str, object], allowed: frozenset[str], field: str
) -> None:
    unknown = set(raw) - allowed
    if unknown:
        names = ", ".join(sorted(map(str, unknown)))
        raise SpecInvalid(field, f"unsupported field(s): {names}")


def _scan_literal_secrets(value: object, field: str) -> None:
    """Refuse a credential-looking key or value anywhere in ``value``."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SpecInvalid(field, "object keys must be strings")
            lowered = key.lower()
            if any(marker in lowered for marker in _SECRET_MARKERS):
                raise SpecInvalid(
                    f"{field}.{key}",
                    "credential-looking key is refused",
                    verdict=Verdict.REFUSED,
                )
            _scan_literal_secrets(item, f"{field}.{key}")
    elif isinstance(value, str):
        lowered = value.lower()
        if any(marker in lowered for marker in _SECRET_MARKERS):
            raise SpecInvalid(
                field,
                "credential-looking value is refused",
                verdict=Verdict.REFUSED,
            )
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        for index, item in enumerate(value):
            _scan_literal_secrets(item, f"{field}[{index}]")


def _require_timezone(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise SpecInvalid(field, "timezone must be a non-empty string")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, KeyError, ValueError, TypeError) as exc:
        raise SpecInvalid(field, f"unknown timezone {value!r}") from exc
    return value


def _wrap_untrusted(kind: str, text: str) -> UntrustedData:
    return UntrustedData(
        kind=kind,
        text=text,
        fingerprint=fingerprint({"kind": kind, "text": text}),
    )


def _build_source(raw: object, index: int) -> SourceSpec:
    field = f"manifest.sources[{index}]"
    source = _require_mapping(raw, field)
    _reject_unknown_keys(source, _SOURCE_KEYS, field)
    source_id = _require_non_empty_str(source.get("source_id"), f"{field}.source_id")
    kind = _require_non_empty_str(source.get("kind"), f"{field}.kind")
    locator = _require_non_empty_str(source.get("locator"), f"{field}.locator")
    text = _require_non_empty_str(source.get("text"), f"{field}.text")
    return SourceSpec(
        source_id=source_id,
        kind=kind,
        locator=locator,
        text=_wrap_untrusted("source_text", text),
    )


def _build_sources(value: object) -> tuple[SourceSpec, ...]:
    field = "manifest.sources"
    items = _require_sequence(value, field)
    if len(items) > MAX_SOURCES:
        raise SpecInvalid(field, f"exceeds max sources {MAX_SOURCES}")
    return tuple(_build_source(item, index) for index, item in enumerate(items))


def _build_destination(value: object) -> DestinationSpec:
    field = "manifest.destination"
    raw = _require_mapping(value, field)
    _reject_unknown_keys(raw, _DESTINATION_KEYS, field)
    sink = _require_non_empty_str(raw.get("sink"), f"{field}.sink")
    if sink != LOCAL_FILE_SINK:
        raise SpecInvalid(
            f"{field}.sink",
            f"unknown sink id {sink!r}",
            verdict=Verdict.REFUSED,
        )
    destination_id = _require_non_empty_str(
        raw.get("destination_id"), f"{field}.destination_id"
    )
    cost_measurement = _require_non_empty_str(
        raw.get("cost_measurement"), f"{field}.cost_measurement"
    )
    if cost_measurement not in KNOWN_COST_MEASUREMENTS:
        raise SpecInvalid(
            f"{field}.cost_measurement",
            f"unknown cost-measurement id {cost_measurement!r}",
            verdict=Verdict.REFUSED,
        )
    return DestinationSpec(
        sink=sink,
        destination_id=destination_id,
        cost_measurement=cost_measurement,
    )


def _build_schedule(value: object) -> ScheduleSpec:
    field = "manifest.schedule"
    raw = _require_mapping(value, field)
    _reject_unknown_keys(raw, _SCHEDULE_KEYS, field)
    cadence = _require_non_empty_str(raw.get("cadence"), f"{field}.cadence")
    description = _require_non_empty_str(
        raw.get("description"), f"{field}.description"
    )
    return ScheduleSpec(cadence=cadence, description=description)


def _build_limits(value: object) -> Limits:
    field = "manifest.limits"
    raw = _require_mapping(value, field)
    _reject_unknown_keys(raw, _LIMITS_KEYS, field)
    return Limits(
        max_duration_seconds=_require_positive_int(
            raw.get("max_duration_seconds"), f"{field}.max_duration_seconds"
        ),
        max_concurrency=_require_positive_int(
            raw.get("max_concurrency"), f"{field}.max_concurrency"
        ),
        max_calls=_require_positive_int(
            raw.get("max_calls"), f"{field}.max_calls"
        ),
    )


def _build_stop(value: object) -> StopSpec:
    field = "manifest.stop"
    raw = _require_mapping(value, field)
    _reject_unknown_keys(raw, _STOP_KEYS, field)
    raw_state = _require_non_empty_str(raw.get("state"), f"{field}.state")
    try:
        state = StopState(raw_state)
    except ValueError as exc:
        raise SpecInvalid(
            f"{field}.state",
            f"unknown stop state {raw_state!r}",
            verdict=Verdict.REFUSED,
        ) from exc
    reason = _require_non_empty_str(raw.get("reason"), f"{field}.reason")
    return StopSpec(state=state, reason=reason)


def load_spec(payload: object) -> AutomationSpec:
    """Validate and load an automation manifest, or raise :class:`SpecInvalid`.

    Every field is validated; the function is total (it either returns a
    complete :class:`AutomationSpec` or raises naming the offending field) and
    it performs no I/O and no scheduling. Source free text is wrapped in
    :class:`UntrustedData` and is never interpreted.
    """

    if not isinstance(payload, Mapping):
        raise SpecInvalid(
            "manifest", f"must be an object, got {type(payload).__name__}"
        )
    _scan_literal_secrets(payload, "manifest")

    version = payload.get("version")
    if version != SPEC_SCHEMA:
        raise SpecInvalid(
            "manifest.version", f"unknown spec version {version!r}"
        )
    _reject_unknown_keys(payload, _MANIFEST_KEYS, "manifest")

    return AutomationSpec(
        version=version,
        name=_require_non_empty_str(payload.get("name"), "manifest.name"),
        sources=_build_sources(payload.get("sources")),
        destination=_build_destination(payload.get("destination")),
        preferences_ref=_require_non_empty_str(
            payload.get("preferences_ref"), "manifest.preferences_ref"
        ),
        state_ref=_require_non_empty_str(
            payload.get("state_ref"), "manifest.state_ref"
        ),
        policy_ref=_require_non_empty_str(
            payload.get("policy_ref"), "manifest.policy_ref"
        ),
        timezone=_require_timezone(payload.get("timezone"), "manifest.timezone"),
        schedule=_build_schedule(payload.get("schedule")),
        limits=_build_limits(payload.get("limits")),
        stop=_build_stop(payload.get("stop")),
    )


def _build_notes(value: object) -> tuple[UntrustedData, ...]:
    field = "preferences.notes"
    items = _require_sequence(value, field)
    if len(items) > MAX_NOTES:
        raise SpecInvalid(field, f"exceeds max notes {MAX_NOTES}")
    notes: list[UntrustedData] = []
    for index, item in enumerate(items):
        text = _require_non_empty_str(item, f"{field}[{index}]")
        notes.append(_wrap_untrusted("state_note", text))
    return tuple(notes)


def load_preferences(payload: object) -> OwnerPreferences:
    """Read owner preferences, or raise :class:`SpecInvalid`.

    This is a read-only operation by construction: the module exposes no write,
    merge, propose-in-place or default path. An absent payload (``None``) or a
    corrupt one is refused rather than replaced with empty preferences.
    """

    if payload is None:
        raise SpecInvalid(
            "preferences", "owner preferences are absent and are never defaulted"
        )
    if not isinstance(payload, Mapping):
        raise SpecInvalid(
            "preferences", f"must be an object, got {type(payload).__name__}"
        )
    _scan_literal_secrets(payload, "preferences")

    version = payload.get("version")
    if version != PREFERENCES_SCHEMA:
        raise SpecInvalid(
            "preferences.version", f"unknown preferences version {version!r}"
        )
    _reject_unknown_keys(payload, _PREFERENCES_KEYS, "preferences")

    return OwnerPreferences(
        version=version,
        timezone=_require_timezone(
            payload.get("timezone"), "preferences.timezone"
        ),
        notes=_build_notes(payload.get("notes")),
    )
