"""Evidence primitives for CAO.

CAO is a control plane, so every observation it reports must be traceable
and honestly qualified. An :class:`Evidence` value records where a fact came
from, what verdict was reached, when it was captured, the content fingerprint,
how much of the target was covered, and why the verdict was reached.

Two rules are load-bearing:

* Unreadable or unestablished facts stay ``UNKNOWN``; they are never rounded
  up to a positive verdict.
* Required facts fail closed. :meth:`Evidence.require_supported` raises unless
  the verdict is ``SUPPORTED``.

This module is deliberately dependency-free (standard library only) so every
other CAO module can rely on it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

__all__ = [
    "EvidenceSource",
    "Verdict",
    "Evidence",
    "EvidenceError",
    "EvidenceUnavailable",
    "canonical_json",
    "fingerprint",
    "fingerprint_bytes",
    "utc_now",
    "validate_coverage",
    "build_evidence",
]

Clock = Callable[[], "datetime"]


class EvidenceError(Exception):
    """Base class for evidence failures."""

    code = "EVIDENCE_ERROR"


class EvidenceUnavailable(EvidenceError):
    """Raised when required evidence is not positively supported."""

    code = "EVIDENCE_UNAVAILABLE"


class EvidenceSource(str, Enum):
    """Where an evidence value originated."""

    PLUGIN = "plugin"
    RUNTIME = "runtime"
    POLICY = "policy"
    STATE = "state"
    CLI = "cli"
    INTERNAL = "internal"


class Verdict(str, Enum):
    """The outcome an evidence value records.

    Only ``SUPPORTED`` is a positive verdict. ``UNAVAILABLE`` means the
    capability or fact was explicitly not offered. ``UNKNOWN`` means it could
    not be established (including unreadable input). ``REFUSED`` means an
    authority check denied the action. ``INVALID`` means malformed input.
    """

    SUPPORTED = "SUPPORTED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"
    REFUSED = "REFUSED"
    INVALID = "INVALID"


def utc_now(clock: Clock | None = None) -> str:
    """Return an RFC 3339 UTC timestamp (second precision, ``Z`` suffix)."""

    moment = (clock or (lambda: datetime.now(timezone.utc)))()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_default(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(_json_default(item) for item in value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize ``value`` to canonical, deterministic JSON.

    Dataclasses, enums, sets and bytes are normalized so the same logical
    content always produces the same bytes.
    """

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=_json_default,
    )


def fingerprint_bytes(data: bytes) -> str:
    """Return a stable ``sha256:`` fingerprint for raw bytes."""

    return "sha256:" + hashlib.sha256(data).hexdigest()


def fingerprint(value: Any) -> str:
    """Return a stable ``sha256:`` fingerprint for any canonicalizable value."""

    return fingerprint_bytes(canonical_json(value).encode("utf-8"))


def validate_coverage(coverage: float | None) -> float | None:
    """Validate a coverage fraction in the closed interval ``[0, 1]``."""

    if coverage is None:
        return None
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)):
        raise EvidenceError(f"coverage must be a number, got {type(coverage).__name__}")
    if not 0.0 <= float(coverage) <= 1.0:
        raise EvidenceError(f"coverage must be within [0, 1], got {coverage!r}")
    return float(coverage)


@dataclass(frozen=True)
class Evidence:
    """A qualified observation.

    Every field the frozen contract requires is present: source, verdict,
    capture time, fingerprint, coverage and reason. Fields that do not apply
    to a given operation are ``None`` rather than silently defaulted to a
    positive value.
    """

    source: EvidenceSource
    verdict: Verdict
    capture_time: str | None = None
    fingerprint: str | None = None
    coverage: float | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        validate_coverage(self.coverage)

    @property
    def supported(self) -> bool:
        return self.verdict is Verdict.SUPPORTED

    def require_supported(self) -> "Evidence":
        """Return self, or raise if the verdict is not ``SUPPORTED``.

        This is the fail-closed gate: callers that must not proceed on
        unknown or unavailable evidence call this instead of branching.
        """

        if not self.supported:
            raise EvidenceUnavailable(
                self.reason or f"evidence verdict is {self.verdict.value}"
            )
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "verdict": self.verdict.value,
            "capture_time": self.capture_time,
            "fingerprint": self.fingerprint,
            "coverage": self.coverage,
            "reason": self.reason,
        }

    def to_json(self) -> str:
        return canonical_json(self.as_dict())


def build_evidence(
    source: EvidenceSource,
    verdict: Verdict,
    *,
    reason: str | None = None,
    fingerprint_value: str | None = None,
    coverage: float | None = None,
    capture_time: str | None = None,
    clock: Clock | None = None,
) -> Evidence:
    """Construct :class:`Evidence`, stamping the capture time.

    An explicit ``capture_time`` wins; otherwise the current UTC time (or the
    injected ``clock``) is used.
    """

    if capture_time is None:
        capture_time = utc_now(clock)
    return Evidence(
        source=source,
        verdict=verdict,
        capture_time=capture_time,
        fingerprint=fingerprint_value,
        coverage=coverage,
        reason=reason,
    )
