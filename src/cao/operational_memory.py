"""Operational memory: a rebuildable, scoped evidence projection.

This module is a **private, rebuildable projection** of episodes that CAO
already evidenced. It is not canonical Company State, not a second ledger, not a
queue and not a scheduler. Deleting every projected record loses no authoritative
state: the source records remain the truth and a projection can be rebuilt from
them. It grants no authority, calls no model, writes nothing outside its own
private ``0700`` store, and never mutates a source record.

What an episode is
------------------

One record per immutable projection unit. The identity of the unit
(``episode_id``) is a one-way digest of the immutable source identity and
scope, and the content is bound by a content fingerprint, so a reused id with
different content is a ``CONFLICT`` and is never merged. The record carries:

* ``source`` -- the exact immutable source metadata: the source record's own
  ``schema``, its ``id``, a deterministic ``ref`` and its content ``digest``;
* ``scope`` -- ``owner`` / ``repository`` / ``data_class`` triple, the exact
  retrieval scope (a ``None`` value means the field is genuinely UNKNOWN);
* ``event`` -- a bounded ``event_type``, the source ``verdict`` (preserved
  verbatim, never rounded up) and the distinct ``event_time`` (source event) and
  ``observed_at`` (source observation/capture);
* ``repo`` -- the exact source ``head``/``base`` SHAs and the relative
  ``path_scope`` (never an absolute private path);
* ``context`` -- evidence references as ``{location, digest, verdict}`` plus the
  declared evidence ``cutoff`` -- references, never bodies;
* ``narrative`` -- always ``None`` in this landing: no proved sanitized
  narrative adapter exists, and a pattern match cannot prove text free of
  personal data, so directly-supplied narrative is refused, never trusted;
* ``lesson`` -- always ``None`` in this landing: no independently qualified
  acceptance adapter exists here, so every non-null lesson is refused.

No raw prompts, transcripts, log bodies, credentials, secrets or personal
identifiers are ever projected: an adapter references evidence by opaque
location and digest rather than copying raw inbound text, and this module
never claims a pattern match proves text free of personal data.

Source-to-field map
-------------------

``FIELD_SOURCE_MAP`` below maps every episode field path to the exact source
that grounds it, or to ``UNSUPPORTED`` when no attributable origin exists in this
landing. ``tests/test_operational_memory.py`` asserts the map covers exactly
``EPISODE_FIELD_PATHS`` so schema drift is caught.

This landing implements **one** read-only adapter, over an already-admitted
intake submission (``cao.intake.submission.v1``). Every other field names a real
existing producer (team checkpoints, triage packets, Registry rows, recovery
decisions, improvement/feedback records) but is **not** adapted here, and no
adapter coverage is claimed for those producers. An integration adapter must be
a read function over an existing record; it may never invent a field it cannot
substantiate, and an absent field stays ``UNKNOWN``.

Separation
----------

Standard library only. No shell, no subprocess, no network or egress module is
imported. This module reaches the Work Registry only read-only (in fact not at
all in this landing) and never imports the runtime contract, policy, plugins or
product-daemon modules.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from . import evidence
from . import intake
from .product_state import state_dir

__all__ = [
    "EPISODE_SCHEMA",
    "EVIDENCE_SCHEMA",
    "SUPPORTED_SCHEMAS",
    "RECORD_TYPE",
    "VERDICTS",
    "DATA_CLASSES",
    "EVENT_TYPES",
    "UNSUPPORTED",
    "EPISODE_FIELD_PATHS",
    "FIELD_SOURCE_MAP",
    "MAX_EPISODE_BYTES",
    "MAX_RETENTION_SECONDS",
    "OperationalMemoryError",
    "EpisodeInvalid",
    "EpisodeUnavailable",
    "EpisodeConflict",
    "EpisodeStateError",
    "episodes_dir",
    "episode_path",
    "episode_id_for",
    "project_episode",
    "evidence_from_submission",
    "ingest",
    "ingest_submission",
    "read_episode",
    "list_episodes",
]

#: The one supported projection schema. A record whose ``schema`` is anything
#: else is refused, never partially parsed.
EPISODE_SCHEMA = "cao.operational.episode.v1"
#: The schema of the normalized adapter input document.
EVIDENCE_SCHEMA = "cao.operational.evidence.v1"
#: The explicit version map a reader resolves through. An unknown version is
#: refused rather than read by the nearest shape.
SUPPORTED_SCHEMAS = (EPISODE_SCHEMA,)
RECORD_TYPE = "operational_episode"
PROJECTION = True
AUTHORITY = "none"

#: Evidence status vocabulary. Born from the frozen evidence contract so an
#: episode can never carry a verdict the rest of CAO does not understand.
VERDICTS = frozenset(item.value for item in evidence.Verdict)
UNKNOWN = "UNKNOWN"
#: Data classes an episode scope may declare. Absent/unknown is refused.
DATA_CLASSES = frozenset({"synthetic", "source_attested"})
#: Bounded event vocabulary; no free text.
EVENT_TYPES = frozenset({"routing", "review", "recovery", "engineering", "feedback"})

#: Marker for a field with no attributable source in this landing.
UNSUPPORTED = "UNSUPPORTED"

MAX_EPISODE_BYTES = 512 * 1024
MAX_TEXT = 1000
MAX_ID = 200
MAX_REF = 500
MAX_REFS = 64
MAX_PATH_SCOPE = 128
#: A deliberate upper bound so "bounded retention" cannot be bypassed with an
#: effectively infinite window.
MAX_RETENTION_SECONDS = 10 * 366 * 24 * 60 * 60

_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
_HEAD_RE = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SCHEMA_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,199}\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_REPO_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]{0,98}/[A-Za-z0-9][A-Za-z0-9._-]{0,98}\Z"
)
_PATH_RE = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")
_REF_RE = re.compile(r"(?:urn:|git:|https://)[^\s\x00-\x1f]{1,499}\Z")
_UTC_TIME_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z\Z"
)

_EVIDENCE_KEYS = frozenset(
    {"schema", "source", "scope", "event", "repo", "context", "narrative", "lesson"}
)
_SOURCE_KEYS = frozenset({"schema", "id", "ref", "digest"})
_SCOPE_KEYS = frozenset({"owner", "repository", "data_class"})
_EVENT_KEYS = frozenset({"event_type", "verdict", "event_time", "observed_at"})
_REPO_KEYS = frozenset({"head", "base", "path_scope"})
_CONTEXT_KEYS = frozenset({"refs", "cutoff"})
_REF_KEYS = frozenset({"location", "digest", "verdict"})
_NARRATIVE_KEYS = frozenset(
    {
        "objective",
        "case",
        "actor",
        "team",
        "hypothesis",
        "action",
        "outcome",
        "correction",
    }
)
_RECORD_KEYS = frozenset(
    {
        "schema",
        "record_type",
        "projection",
        "authority",
        "episode_id",
        "content_fingerprint",
        "created_at",
        "inputs",
        "source",
        "scope",
        "event",
        "repo",
        "context",
        "narrative",
        "lesson",
    }
)

#: Every episode field path, and the exact source that grounds it. A value of
#: ``UNSUPPORTED`` means no attributable origin exists in this landing and the
#: field stays absent/``UNKNOWN`` rather than being manufactured.
EPISODE_FIELD_PATHS = (
    "schema",
    "record_type",
    "projection",
    "authority",
    "episode_id",
    "content_fingerprint",
    "created_at",
    "inputs",
    "source.schema",
    "source.id",
    "source.ref",
    "source.digest",
    "scope.owner",
    "scope.repository",
    "scope.data_class",
    "event.event_type",
    "event.verdict",
    "event.event_time",
    "event.observed_at",
    "repo.head",
    "repo.base",
    "repo.path_scope",
    "context.refs",
    "context.cutoff",
    "narrative.objective",
    "narrative.case",
    "narrative.actor",
    "narrative.team",
    "narrative.hypothesis",
    "narrative.action",
    "narrative.outcome",
    "narrative.correction",
    "lesson",
)

FIELD_SOURCE_MAP = {
    "schema": "own constant cao.operational.episode.v1",
    "record_type": "own constant",
    "projection": "own constant (always true)",
    "authority": "own constant (always none)",
    "episode_id": "derived: one-way digest of the immutable identity inputs",
    "content_fingerprint": "derived: evidence.fingerprint of the record content",
    "created_at": "own: projection capture time (excluded from the fingerprint)",
    "inputs": "the normalized adapter input document (evidence)",
    "source.schema": "the immutable source record's own schema constant",
    "source.id": "the immutable source record's own identity field",
    "source.ref": "deterministic urn built from the source schema and id",
    "source.digest": "the source record's own content fingerprint (sha256)",
    "scope.owner": "caller-declared owner identity; null when UNKNOWN",
    "scope.repository": "caller-declared owner/repository identity; null when UNKNOWN",
    "scope.data_class": "caller-declared bounded data class",
    "event.event_type": "caller-declared bounded event type",
    "event.verdict": "the source record's own verdict; UNKNOWN when it declares none",
    "event.event_time": "the source record's own event time; null when UNKNOWN",
    "event.observed_at": "the source record's own observation/capture time",
    "repo.head": "the source record's exact HEAD/SHA; null when UNKNOWN",
    "repo.base": "the source record's declared base revision",
    "repo.path_scope": "the source record's declared relative path scope",
    "context.refs": "the source record's own evidence references (location+digest)",
    "context.cutoff": "the source record's declared evidence cutoff; null when UNKNOWN",
    "narrative.objective": f"{UNSUPPORTED}: direct narrative is refused (no proved sanitized adapter)",
    "narrative.case": f"{UNSUPPORTED}: direct narrative is refused (no proved sanitized adapter)",
    "narrative.actor": f"{UNSUPPORTED}: direct narrative is refused (no proved sanitized adapter)",
    "narrative.team": f"{UNSUPPORTED}: direct narrative is refused (no proved sanitized adapter)",
    "narrative.hypothesis": f"{UNSUPPORTED}: direct narrative is refused (no proved sanitized adapter)",
    "narrative.action": f"{UNSUPPORTED}: direct narrative is refused (no proved sanitized adapter)",
    "narrative.outcome": f"{UNSUPPORTED}: direct narrative is refused (no proved sanitized adapter)",
    "narrative.correction": f"{UNSUPPORTED}: direct narrative is refused (no proved sanitized adapter)",
    "lesson": f"{UNSUPPORTED}: no independently qualified acceptance adapter exists here",
}


class OperationalMemoryError(Exception):
    """Base class for operational-memory failures."""

    code = "OPERATIONAL_MEMORY_ERROR"


class EpisodeInvalid(OperationalMemoryError):
    """Malformed, out-of-vocabulary or unsafe projection input."""

    code = "OPERATIONAL_MEMORY_INVALID"


class EpisodeUnavailable(OperationalMemoryError):
    """A required episode or store is genuinely absent or out of window."""

    code = "OPERATIONAL_MEMORY_UNAVAILABLE"


class EpisodeConflict(OperationalMemoryError):
    """An episode id was reused with different content."""

    code = "OPERATIONAL_MEMORY_CONFLICT"


class EpisodeStateError(OperationalMemoryError):
    """A stored record is corrupt, tampered or not the expected shape."""

    code = "OPERATIONAL_MEMORY_STATE"


# ---------------------------------------------------------------------------
# Storage guards: reuse the accepted intake private-store toolkit, mapping its
# failures onto this module's own fail-closed error surface.
# ---------------------------------------------------------------------------


def _open_store_for_write(directory: str) -> int:
    try:
        return intake.open_private_dir(directory)
    except intake.IntakeError as exc:
        raise EpisodeStateError(
            f"operational-memory store is not usable ({type(exc).__name__})"
        ) from exc


def _open_store_for_read(directory: str) -> int | None:
    try:
        return intake.open_private_dir_readonly(directory)
    except intake.IntakeError as exc:
        raise EpisodeStateError(
            f"operational-memory store is not usable ({type(exc).__name__})"
        ) from exc


def _write_stored(dir_fd: int, name: str, payload: str) -> bool:
    try:
        return intake.write_private_record(dir_fd, name, payload)
    except intake.IntakeError as exc:
        raise EpisodeStateError(
            f"episode could not be written ({type(exc).__name__})"
        ) from exc


def _read_stored(dir_fd: int, name: str) -> dict[str, Any] | None:
    try:
        return intake.read_private_record(dir_fd, name)
    except intake.IntakeStateError as exc:
        raise EpisodeStateError(
            f"stored episode could not be read ({type(exc).__name__})"
        ) from exc
    except intake.IntakeUnavailable as exc:
        raise EpisodeUnavailable(
            f"stored episode is not readable ({type(exc).__name__})"
        ) from exc


def _list_stored(dir_fd: int) -> list[str]:
    try:
        return intake.list_private_names(dir_fd)
    except intake.IntakeError as exc:
        raise EpisodeStateError(
            f"operational-memory directory cannot be listed ({type(exc).__name__})"
        ) from exc


# ---------------------------------------------------------------------------
# Bounded, strict field validation
# ---------------------------------------------------------------------------


def _text(value: Any, field: str, limit: int, *, allow_none: bool = True) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise EpisodeInvalid(f"{field}: required text is absent")
    if not isinstance(value, str) or not value.strip():
        raise EpisodeInvalid(f"{field}: expected non-empty text")
    if len(value) > limit:
        raise EpisodeInvalid(f"{field}: text exceeds {limit} characters")
    if any(ord(ch) < 0x20 for ch in value):
        raise EpisodeInvalid(f"{field}: text contains a control character")
    if intake.looks_like_credential(value):
        raise EpisodeInvalid(f"{field}: text contains what looks like a credential")
    return value


def _token(value: Any, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise EpisodeInvalid(f"{field}: not a bounded safe identity")
    return value


def _optional_token(
    value: Any, field: str, pattern: re.Pattern[str]
) -> str | None:
    if value is None:
        return None
    return _token(value, field, pattern)


def _safe_ref(value: Any, field: str) -> str:
    text = _text(value, field, MAX_REF, allow_none=False)
    assert text is not None
    if not _REF_RE.fullmatch(text):
        raise EpisodeInvalid(f"{field}: expected a urn:, git: or safe https reference")
    if text.startswith("https://"):
        authority = text[len("https://") :].split("/", 1)[0]
        if "@" in text or "?" in text or "#" in text or not authority:
            raise EpisodeInvalid(f"{field}: unsafe HTTPS reference")
    return text


def _digest(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise EpisodeInvalid(f"{field}: expected a sha256 content digest")
    return value


def _head(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _HEAD_RE.fullmatch(value):
        raise EpisodeInvalid(f"{field}: expected a 40-hex commit id")
    return value


def _utc_time(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _UTC_TIME_RE.fullmatch(value):
        raise EpisodeInvalid(f"{field}: expected an RFC 3339 UTC timestamp ending Z")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:  # pragma: no cover - regex already constrains shape
        raise EpisodeInvalid(f"{field}: invalid UTC timestamp") from exc
    if moment.utcoffset() != timedelta(0):
        raise EpisodeInvalid(f"{field}: timestamp must be UTC")
    return value


def _moment(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _require_keys(value: Any, allowed: frozenset[str], required: frozenset[str], what: str) -> dict:
    if type(value) is not dict:
        raise EpisodeInvalid(f"{what}: expected an object")
    unknown = set(value) - allowed
    if unknown:
        raise EpisodeInvalid(f"{what}: unsupported field(s) {sorted(unknown)}")
    missing = required - set(value)
    if missing:
        raise EpisodeInvalid(f"{what}: missing required field(s) {sorted(missing)}")
    return value


# ---------------------------------------------------------------------------
# Adapter input normalization (the evidence document)
# ---------------------------------------------------------------------------


def _normalize_source(raw: Any) -> dict[str, Any]:
    node = _require_keys(
        raw, _SOURCE_KEYS, frozenset({"schema", "id", "ref"}), "source"
    )
    return {
        "schema": _token(node["schema"], "source.schema", _SCHEMA_RE),
        "id": _token(node["id"], "source.id", _TOKEN_RE),
        "ref": _safe_ref(node["ref"], "source.ref"),
        "digest": _digest(node.get("digest"), "source.digest"),
    }


def _normalize_scope(raw: Any) -> dict[str, Any]:
    node = _require_keys(raw, _SCOPE_KEYS, frozenset({"data_class"}), "scope")
    data_class = node["data_class"]
    if not isinstance(data_class, str) or data_class not in DATA_CLASSES:
        raise EpisodeInvalid("scope.data_class: unsupported data class")
    return {
        "owner": _optional_token(node.get("owner"), "scope.owner", _TOKEN_RE),
        "repository": _optional_token(
            node.get("repository"), "scope.repository", _REPO_RE
        ),
        "data_class": data_class,
    }


def _normalize_event(raw: Any) -> dict[str, Any]:
    node = _require_keys(
        raw, _EVENT_KEYS, frozenset({"event_type", "observed_at"}), "event"
    )
    event_type = node["event_type"]
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise EpisodeInvalid("event.event_type: unsupported event type")
    verdict = node.get("verdict", UNKNOWN)
    if not isinstance(verdict, str) or verdict not in VERDICTS:
        raise EpisodeInvalid("event.verdict: unsupported verdict")
    event_time = node.get("event_time")
    if event_time is not None:
        event_time = _utc_time(event_time, "event.event_time")
    return {
        "event_type": event_type,
        "verdict": verdict,
        "event_time": event_time,
        "observed_at": _utc_time(node["observed_at"], "event.observed_at"),
    }


def _normalize_repo(raw: Any) -> dict[str, Any]:
    node = _require_keys(raw, _REPO_KEYS, frozenset(), "repo")
    scope = node.get("path_scope", [])
    if not isinstance(scope, list) or len(scope) > MAX_PATH_SCOPE:
        raise EpisodeInvalid("repo.path_scope: expected a bounded list")
    paths: list[str] = []
    for entry in scope:
        path = _token(entry, "repo.path_scope", _PATH_RE)
        if path.startswith("/") or ".." in path.split("/"):
            raise EpisodeInvalid("repo.path_scope: path must be relative and safe")
        if path not in paths:
            paths.append(path)
    return {
        "head": _head(node.get("head"), "repo.head"),
        "base": _head(node.get("base"), "repo.base"),
        "path_scope": sorted(paths),
    }


def _normalize_context(raw: Any) -> dict[str, Any]:
    node = _require_keys(raw, _CONTEXT_KEYS, frozenset(), "context")
    refs = node.get("refs", [])
    if not isinstance(refs, list) or len(refs) > MAX_REFS:
        raise EpisodeInvalid("context.refs: expected a bounded list")
    normalized: list[dict[str, Any]] = []
    for item in refs:
        entry = _require_keys(item, _REF_KEYS, _REF_KEYS, "context.ref")
        verdict = entry["verdict"]
        if not isinstance(verdict, str) or verdict not in VERDICTS:
            raise EpisodeInvalid("context.ref.verdict: unsupported verdict")
        normalized.append(
            {
                "location": _safe_ref(entry["location"], "context.ref.location"),
                "digest": _digest(entry["digest"], "context.ref.digest"),
                "verdict": verdict,
            }
        )
    cutoff = node.get("cutoff")
    if cutoff is not None:
        cutoff = _utc_time(cutoff, "context.cutoff")
    return {"refs": normalized, "cutoff": cutoff}


def _normalize_narrative(raw: Any) -> dict[str, Any]:
    """Refuse direct narrative: no proved sanitized narrative adapter exists.

    A pattern match cannot prove text free of personal data, so this isolated
    module accepts no narrative text at all. Every field stays ``None`` until a
    source adapter can *prove* the text passed a sanitizing boundary.
    """

    node = _require_keys(raw, _NARRATIVE_KEYS, frozenset(), "narrative")
    if any(value is not None for value in node.values()):
        raise EpisodeInvalid(
            "narrative: direct narrative is refused without a proved sanitized adapter"
        )
    return {key: None for key in _NARRATIVE_KEYS}


def _normalize_lesson(raw: Any) -> None:
    """Refuse every non-null lesson: no independent acceptance exists here.

    A lesson is only ever accepted by an independently qualified, operator-
    attributed source adapter. None exists in this module, so a caller-,
    model- or intake-supplied lesson is refused outright and the field stays
    ``None``.
    """

    if raw is not None:
        raise EpisodeInvalid(
            "lesson: no independently qualified acceptance source exists in this module"
        )
    return None


def _normalize_evidence(document: Any) -> dict[str, Any]:
    """Validate and canonicalize one adapter input document, fail closed."""

    root = _require_keys(
        document, _EVIDENCE_KEYS, _EVIDENCE_KEYS, "evidence"
    )
    if root["schema"] != EVIDENCE_SCHEMA:
        raise EpisodeInvalid("evidence: unsupported schema")
    return {
        "schema": EVIDENCE_SCHEMA,
        "source": _normalize_source(root["source"]),
        "scope": _normalize_scope(root["scope"]),
        "event": _normalize_event(root["event"]),
        "repo": _normalize_repo(root["repo"]),
        "context": _normalize_context(root["context"]),
        "narrative": _normalize_narrative(root["narrative"]),
        "lesson": _normalize_lesson(root["lesson"]),
    }


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def _content(evidence_doc: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": evidence_doc["source"],
        "scope": evidence_doc["scope"],
        "event": evidence_doc["event"],
        "repo": evidence_doc["repo"],
        "context": evidence_doc["context"],
        "narrative": evidence_doc["narrative"],
        "lesson": evidence_doc["lesson"],
    }


def _identity_inputs(evidence_doc: Mapping[str, Any]) -> dict[str, Any]:
    """The immutable identity of the projection unit.

    The source identity, the scope and the event type define *which* episode a
    record is; the content fingerprint binds everything else, so a reused id
    with any different content is a conflict rather than a silent merge.
    """

    return {
        "source": evidence_doc["source"],
        "scope": evidence_doc["scope"],
        "event_type": evidence_doc["event"]["event_type"],
    }


def _sha256_hex(value: Any) -> str:
    return hashlib.sha256(evidence.canonical_json(value).encode("utf-8")).hexdigest()


def episode_id_for(document: Any) -> str:
    """The deterministic episode id for an adapter input document."""

    return _sha256_hex(_identity_inputs(_normalize_evidence(document)))


def _content_fingerprint(record: Mapping[str, Any]) -> str:
    content = {
        key: value
        for key, value in record.items()
        if key not in {"episode_id", "created_at", "content_fingerprint"}
    }
    return evidence.fingerprint(content)


def project_episode(document: Any, *, clock: Any = None) -> dict[str, Any]:
    """Build one immutable episode record from a validated evidence document.

    Pure: it reads nothing and writes nothing. The same normalized document
    always yields the same id and the same content fingerprint.
    """

    normalized = _normalize_evidence(document)
    record: dict[str, Any] = {
        "schema": EPISODE_SCHEMA,
        "record_type": RECORD_TYPE,
        "projection": PROJECTION,
        "authority": AUTHORITY,
        "episode_id": _sha256_hex(_identity_inputs(normalized)),
        "content_fingerprint": "",
        "created_at": evidence.utc_now(clock),
        "inputs": normalized,
    }
    record.update(_content(normalized))
    record["content_fingerprint"] = _content_fingerprint(record)
    return record


def _verify_record(data: Any, expected_id: str) -> dict[str, Any]:
    """Verify one stored record against its file name and its own content.

    Both bindings are re-derived from the stored bytes: the identity digest over
    the stored inputs and the content fingerprint over the whole record. The
    projection is additionally rebuilt from the stored inputs and must match, so
    a tampered or drifted record fails closed instead of being partially read.
    """

    if not isinstance(data, Mapping):
        raise EpisodeStateError("stored episode is not a JSON object")
    if data.get("schema") not in SUPPORTED_SCHEMAS:
        raise EpisodeStateError("stored episode has an unknown schema")
    if set(data) != _RECORD_KEYS:
        raise EpisodeStateError("stored episode has an unexpected shape")
    if data.get("record_type") != RECORD_TYPE:
        raise EpisodeStateError("stored record is not an operational episode")
    if data.get("projection") is not True or data.get("authority") != AUTHORITY:
        raise EpisodeStateError("stored episode is not an authority-free projection")
    declared = data.get("episode_id")
    if not isinstance(declared, str) or not _ID_RE.fullmatch(declared):
        raise EpisodeStateError("stored episode id is malformed")
    if declared != expected_id:
        raise EpisodeStateError("stored episode id does not match its file")
    _utc_time(data.get("created_at"), "created_at")
    stored_print = data.get("content_fingerprint")
    if not isinstance(stored_print, str) or not _DIGEST_RE.fullmatch(stored_print):
        raise EpisodeStateError("stored episode content fingerprint is malformed")
    try:
        normalized = _normalize_evidence(data.get("inputs"))
    except EpisodeInvalid as exc:
        raise EpisodeStateError("stored episode inputs are malformed") from exc
    if normalized != data.get("inputs"):
        raise EpisodeStateError("stored episode inputs are not canonical")
    if _sha256_hex(_identity_inputs(normalized)) != declared:
        raise EpisodeStateError("stored episode id does not bind its inputs")
    rebuilt = _content(normalized)
    for key, value in rebuilt.items():
        if data.get(key) != value:
            raise EpisodeStateError("stored episode projection does not match its inputs")
    if _content_fingerprint(data) != stored_print:
        raise EpisodeStateError("stored episode content does not match its fingerprint")
    return dict(data)


# ---------------------------------------------------------------------------
# Private, atomic, exclusive storage (reusing the accepted intake toolkit)
# ---------------------------------------------------------------------------


def episodes_dir(env: Mapping[str, str] | None = None) -> str:
    """The private operational-memory directory under the XDG CAO state root."""

    return os.path.join(state_dir(env), "operational-memory")


def episode_path(episode_id: str, env: Mapping[str, str] | None = None) -> str:
    if not isinstance(episode_id, str) or not _ID_RE.fullmatch(episode_id):
        raise EpisodeInvalid("episode id must be 64 lowercase hex characters")
    return os.path.join(episodes_dir(env), episode_id + ".json")


def _admission(status: str, record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": EPISODE_SCHEMA,
        "status": status,
        "duplicate": status == "DUPLICATE",
        "episode": dict(record),
    }


def ingest(
    document: Any, *, env: Mapping[str, str] | None = None, clock: Any = None
) -> dict[str, Any]:
    """Persist one projection idempotently and return its acknowledgement.

    Re-ingesting the same evidence yields ``DUPLICATE`` with the original
    durable record. The same id with different content raises
    :class:`EpisodeConflict` and leaves the original untouched. Nothing is
    promoted, dispatched or claimed.
    """

    record = project_episode(document, clock=clock)
    name = record["episode_id"] + ".json"
    payload = json.dumps(
        record, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False
    )
    if len(payload.encode("utf-8")) > MAX_EPISODE_BYTES:
        raise EpisodeInvalid(f"episode exceeds {MAX_EPISODE_BYTES} bytes")
    dir_fd = _open_store_for_write(episodes_dir(env))
    try:
        created = _write_stored(dir_fd, name, payload)
        data = _read_stored(dir_fd, name)
    finally:
        os.close(dir_fd)
    if created:
        if data is None:
            raise EpisodeStateError("episode failed its read-back check")
        stored = _verify_record(data, record["episode_id"])
        if stored != record:
            raise EpisodeStateError("episode failed its read-back check")
        return _admission("ADMITTED", stored)
    if data is None:
        raise EpisodeUnavailable("an existing episode disappeared before read")
    stored = _verify_record(data, record["episode_id"])
    if stored["content_fingerprint"] == record["content_fingerprint"]:
        return _admission("DUPLICATE", stored)
    raise EpisodeConflict(
        "episode id already exists with different content; refusing to merge"
    )


# ---------------------------------------------------------------------------
# Read-only, bounded retrieval
# ---------------------------------------------------------------------------


def _retention_cutoff(retention_seconds: Any, now: Any) -> datetime:
    if isinstance(retention_seconds, bool) or not isinstance(retention_seconds, int):
        raise EpisodeInvalid("retention_seconds: an explicit integer window is required")
    if retention_seconds <= 0:
        raise EpisodeInvalid("retention_seconds: window must be positive")
    if retention_seconds > MAX_RETENTION_SECONDS:
        raise EpisodeInvalid("retention_seconds: window exceeds the supported bound")
    stamp = evidence.utc_now() if now is None else _utc_time(now, "now")
    return _moment(stamp) - timedelta(seconds=retention_seconds)


def _retrieval_time(record: Mapping[str, Any]) -> str:
    """The timestamp used to order and retain a record, deterministic and named.

    The source ``event_time`` is authoritative when it exists. An episode whose
    source never declared an event time keeps ``event_time = None`` and is
    ordered and retained by its ``observed_at`` capture time instead. The
    fallback is applied here only; it is never written back into the record.
    """

    event_time = record["event"]["event_time"]
    return event_time if event_time is not None else record["event"]["observed_at"]


def _in_window(record: Mapping[str, Any], cutoff: datetime) -> bool:
    """Whether a record is inside the window, using the named retrieval time.

    The boundary is inclusive: a record exactly at the cutoff is retained. An
    episode with no source event time is retained by its ``observed_at``
    fallback rather than being discarded for an absent field.
    """

    return _moment(_retrieval_time(record)) >= cutoff


def _provenance(
    record: Mapping[str, Any],
    resolver: Callable[[Mapping[str, Any]], Any] | None,
) -> tuple[str, bool]:
    """Resolve a record's provenance status on read, never mutating it."""

    if resolver is None:
        return "UNVERIFIED", False
    try:
        resolved = resolver(dict(record["source"]))
    except Exception:
        return "UNRESOLVED", False
    if resolved is None:
        return "UNRESOLVED", False
    if not isinstance(resolved, Mapping):
        raise EpisodeInvalid("source resolver must return a mapping or None")
    source = record["source"]
    head = record["repo"]["head"]
    if source["digest"] is not None and resolved.get("digest") != source["digest"]:
        return "INVALIDATED", False
    if head is not None and resolved.get("head") != head:
        return "STALE", False
    if source["digest"] is None and head is None:
        return "UNVERIFIED", False
    return "CURRENT", True


def _view(
    record: Mapping[str, Any],
    resolver: Callable[[Mapping[str, Any]], Any] | None,
) -> dict[str, Any]:
    view = dict(record)
    status, grounded = _provenance(record, resolver)
    view["provenance_status"] = status
    view["grounded"] = grounded
    return view


def read_episode(
    episode_id: str,
    *,
    scope: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    retention_seconds: int,
    now: Any = None,
    resolver: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Read one episode view for one exact scope, or fail closed.

    Retrieval is read-only: no record is mutated, promoted or swept. A record
    outside the explicit retention window is refused rather than returned, and
    a record with no source event time is retained and ordered by its
    ``observed_at`` (see :func:`_retrieval_time`).

    ``scope`` is a required, exact ``owner`` / ``repository`` / ``data_class``
    triple (``None`` owner/repository means genuinely UNKNOWN). It is enforced
    *before* any content is returned: an omitted or mismatched scope refuses.

    LIMITATION: this is a logical scope filter over recorded metadata, **not**
    an authenticated identity or authorization check -- a caller can assert
    any scope. Authenticating the caller is a separate integration concern and
    is deliberately not claimed here.
    """

    if scope is None:
        raise EpisodeInvalid(
            "scope: an explicit owner/repository/data_class scope is required"
        )
    want = _normalize_scope(scope)
    eid = episode_id if isinstance(episode_id, str) and _ID_RE.fullmatch(episode_id) else None
    if eid is None:
        raise EpisodeInvalid("episode id must be 64 lowercase hex characters")
    cutoff = _retention_cutoff(retention_seconds, now)
    dir_fd = _open_store_for_read(episodes_dir(env))
    if dir_fd is None:
        raise EpisodeUnavailable("no operational-memory store exists")
    try:
        data = _read_stored(dir_fd, eid + ".json")
    finally:
        os.close(dir_fd)
    if data is None:
        raise EpisodeUnavailable("no episode with that id")
    record = _verify_record(data, eid)
    if record["scope"] != want:
        raise EpisodeUnavailable("episode is not in the requested scope")
    if not _in_window(record, cutoff):
        raise EpisodeUnavailable("episode is outside the retention window")
    return _view(record, resolver)


def list_episodes(
    *,
    env: Mapping[str, str] | None = None,
    scope: Mapping[str, Any],
    retention_seconds: int,
    now: Any = None,
    resolver: Callable[[Mapping[str, Any]], Any] | None = None,
) -> list[dict[str, Any]]:
    """Bounded, deterministic episode views for one exact scope.

    Only records whose ``scope`` equals the requested triple are returned; a
    cross-scope query returns nothing rather than a partial match. Order is
    ``(retrieval_time, episode_id)``, where an absent source event time falls
    back to ``observed_at`` (:func:`_retrieval_time`), and is stable across
    restart and re-read.
    """

    want = _normalize_scope(scope)
    cutoff = _retention_cutoff(retention_seconds, now)
    dir_fd = _open_store_for_read(episodes_dir(env))
    if dir_fd is None:
        return []
    try:
        views: list[dict[str, Any]] = []
        for name in _list_stored(dir_fd):
            if name.startswith(".") or not name.endswith(".json"):
                continue
            eid = name[: -len(".json")]
            if not _ID_RE.fullmatch(eid):
                raise EpisodeStateError(
                    "operational-memory directory holds an unexpected file"
                )
            data = _read_stored(dir_fd, name)
            if data is None:
                continue
            record = _verify_record(data, eid)
            if record["scope"] != want:
                continue
            if not _in_window(record, cutoff):
                continue
            views.append(_view(record, resolver))
    finally:
        os.close(dir_fd)
    views.sort(key=lambda view: (_retrieval_time(view), view["episode_id"]))
    return views


# ---------------------------------------------------------------------------
# One read-only adapter over an existing immutable record
# ---------------------------------------------------------------------------


def evidence_from_submission(
    submission_id: str,
    *,
    event_type: str,
    data_class: str,
    owner: str | None = None,
    repository: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build an evidence document from one admitted intake submission.

    This is a read function over an existing durable record. It never copies
    raw inbound text into the projection: the title, item id and evidence-ref
    locations are free text that may carry personal data, so they are left
    explicitly UNKNOWN/``None`` rather than copied. Evidence references are
    replaced with opaque, source-scoped locations that carry no inbound
    substring, and the declared reference digest is preserved only when it is a
    real content digest. The source event time, verdict, base revision and
    write scope are UNKNOWN for a submission, so they are left null and never
    manufactured. The raw body, prompt and transcript are never read into the
    projection.
    """

    record = intake.read_submission(submission_id, env)
    sid = record["submission_id"]
    refs: list[dict[str, Any]] = []
    for index, ref in enumerate(record["evidence_refs"]):
        declared = ref.get("fingerprint")
        digest = (
            declared
            if isinstance(declared, str) and _DIGEST_RE.fullmatch(declared)
            else None
        )
        refs.append(
            {
                "location": f"urn:cao:intake-submission:{sid}#evidence-{index}",
                "digest": digest,
                "verdict": UNKNOWN,
            }
        )
    return {
        "schema": EVIDENCE_SCHEMA,
        "source": {
            "schema": intake.SUBMISSION_SCHEMA,
            "id": sid,
            "ref": f"urn:cao:intake-submission:{sid}",
            "digest": record["fingerprint"],
        },
        "scope": {
            "owner": owner,
            "repository": repository,
            "data_class": data_class,
        },
        "event": {
            "event_type": event_type,
            "verdict": UNKNOWN,
            "event_time": None,
            "observed_at": record["admitted_at"],
        },
        "repo": {"head": None, "base": None, "path_scope": []},
        "context": {"refs": refs, "cutoff": None},
        "narrative": {key: None for key in _NARRATIVE_KEYS},
        "lesson": None,
    }


def ingest_submission(
    submission_id: str,
    *,
    event_type: str,
    data_class: str,
    owner: str | None = None,
    repository: str | None = None,
    env: Mapping[str, str] | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Project and persist one episode from an admitted intake submission."""

    document = evidence_from_submission(
        submission_id,
        event_type=event_type,
        data_class=data_class,
        owner=owner,
        repository=repository,
        env=env,
    )
    return ingest(document, env=env, clock=clock)
