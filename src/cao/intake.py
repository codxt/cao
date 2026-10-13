"""Durable, private intake admission for operator-submitted items (B1).

Intake is the inbound gate. It accepts a bounded, strictly-shaped JSON
submission, admits it once, and stores the result in the operator's private XDG
CAO state. It is deliberately *not* a work item, a queue, a scheduler, a Work
Registry claim or an agent launch: an admitted submission is a durable record
with an explicit state, and nothing here starts, merges or dispatches anything.

Load-bearing rules:

* A submission is admitted exactly once. The durable submission id is derived
  from the stable ``item_id`` and the content ``fingerprint`` is stored beside
  it: the same item with the same content collapses to a ``DUPLICATE``, while
  the same item with different content is refused as a conflict and never
  silently merged over the original.
* Storage is one file per submission under ``$XDG_STATE_HOME/cao/intake``,
  written through a private temp file and an atomic, exclusive ``link`` so two
  concurrent submitters cannot overwrite or duplicate each other. Directories
  are ``0700`` and files ``0600``.
* Records are read through :mod:`cao.state`: an unreadable or malformed record
  raises and is never rounded up to an empty result. An unknown schema raises.
* Submission bodies are untrusted, opaque data. They are stored verbatim
  (after secret redaction) and are never parsed as directives. Only public-safe
  metadata -- ids, timestamps, fingerprints -- is reported by ``list``.

Standard library only; no third-party dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any

from .evidence import fingerprint, utc_now
from .product_state import state_dir
from .state import StateError, read_state

__all__ = [
    "REQUEST_SCHEMA",
    "SUBMISSION_SCHEMA",
    "ADMISSION_SCHEMA",
    "INTAKE_STATE_ADMITTED",
    "RECORD_TYPE",
    "MAX_SUBMISSION_BYTES",
    "MAX_BODY_CHARS",
    "MAX_ITEM_ID",
    "MAX_TITLE",
    "MAX_SOURCE",
    "MAX_EVIDENCE_REFS",
    "MAX_REF",
    "IntakeError",
    "IntakeInvalid",
    "IntakeStateError",
    "IntakeUnavailable",
    "IntakeConflict",
    "intake_dir",
    "submission_path",
    "submission_id_for",
    "redact_secrets",
    "load_submission_file",
    "load_submission_bytes",
    "load_submission_stream",
    "normalize_submission",
    "submit",
    "list_submissions",
    "read_submission",
]

#: The strict schema an operator-written submission file must carry.
REQUEST_SCHEMA = "cao.intake.request.v1"
#: The schema of one durable admitted submission record.
SUBMISSION_SCHEMA = "cao.intake.submission.v1"
#: The schema of an acknowledged admission result.
ADMISSION_SCHEMA = "cao.intake.admission.v1"
#: The record type that separates a submission from a work item.
RECORD_TYPE = "intake_submission"
#: The only state B1 writes. Nothing here transitions a submission to started.
INTAKE_STATE_ADMITTED = "ADMITTED"

MAX_SUBMISSION_BYTES = 256 * 1024
MAX_BODY_CHARS = 128 * 1024
MAX_ITEM_ID = 200
MAX_TITLE = 300
MAX_SOURCE = 200
MAX_EVIDENCE_REFS = 64
MAX_REF = 500

_REF_KEYS = frozenset({"ref", "fingerprint"})
_REQUEST_KEYS = frozenset(
    {"schema", "item_id", "title", "source", "body", "evidence_refs"}
)
_RECORD_KEYS = frozenset(
    {
        "schema",
        "record_type",
        "submission_id",
        "item_id",
        "title",
        "source",
        "state",
        "fingerprint",
        "body",
        "body_redacted",
        "evidence_refs",
        "admitted_at",
    }
)
_HEX = frozenset("0123456789abcdef")


class IntakeError(Exception):
    """Base class for intake failures."""

    code = "INTAKE_ERROR"


class IntakeInvalid(IntakeError):
    """A submission is malformed, oversized or otherwise unusable."""

    code = "INTAKE_INVALID"


class IntakeStateError(IntakeError):
    """Persisted intake state is present but not usable."""

    code = "INTAKE_STATE"


class IntakeUnavailable(IntakeError):
    """A submission or its storage could not be reached."""

    code = "INTAKE_UNAVAILABLE"


class IntakeConflict(IntakeError):
    """A stable item id was reused for different content."""

    code = "INTAKE_CONFLICT"


# ---------------------------------------------------------------------------
# Secret redaction (never persist an obvious credential)
# ---------------------------------------------------------------------------

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "[REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "[REDACTED]"),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?"
            r"-----END [A-Z ]*PRIVATE KEY-----"
        ),
        "[REDACTED PRIVATE KEY]",
    ),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|"
            r"access[_-]?key|private[_-]?key|authorization|client[_-]?secret)\b"
            r"(\s*[:=]\s*)([^\s,;]{4,})"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer [REDACTED]"),
)


def redact_secrets(text: str) -> tuple[str, bool]:
    """Return ``(redacted_text, changed)`` with obvious secrets removed."""

    changed = False
    for pattern, replacement in _REDACTIONS:
        text, count = pattern.subn(replacement, text)
        if count:
            changed = True
    return text, changed


# ---------------------------------------------------------------------------
# Strict, bounded JSON input
# ---------------------------------------------------------------------------


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IntakeInvalid("submission has a duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_token: str) -> Any:
    raise IntakeInvalid("submission carries a non-finite JSON number")


def load_submission_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read one bounded, strictly-shaped submission document.

    The final path component is opened with ``O_NOFOLLOW`` and ``fstat``-checked
    as a regular file no larger than :data:`MAX_SUBMISSION_BYTES`, so a symlink
    or a device cannot be substituted. Duplicate keys and non-finite numbers are
    refused. Every failure is :class:`IntakeInvalid`.
    """

    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(os.fspath(path), flags)
    except OSError as exc:
        raise IntakeUnavailable(
            f"submission file cannot be opened ({type(exc).__name__})"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise IntakeInvalid("submission input must be a regular file")
        if info.st_size > MAX_SUBMISSION_BYTES:
            raise IntakeInvalid(
                f"submission exceeds {MAX_SUBMISSION_BYTES} bytes"
            )
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            raw = stream.read(MAX_SUBMISSION_BYTES + 1)
    except OSError as exc:
        raise IntakeUnavailable(
            f"submission file cannot be read ({type(exc).__name__})"
        ) from exc
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
    if len(raw) > MAX_SUBMISSION_BYTES:
        raise IntakeInvalid(f"submission exceeds {MAX_SUBMISSION_BYTES} bytes")
    return load_submission_bytes(raw)


def load_submission_bytes(raw: bytes | bytearray) -> dict[str, Any]:
    """Parse one bounded, strictly-shaped submission from raw bytes.

    Shared by the file loader and the inbound bridge so both apply the same
    bound, UTF-8 requirement, duplicate-key refusal and non-finite refusal.
    """

    if not isinstance(raw, (bytes, bytearray)):
        raise IntakeInvalid("submission input must be bytes")
    if len(raw) > MAX_SUBMISSION_BYTES:
        raise IntakeInvalid(f"submission exceeds {MAX_SUBMISSION_BYTES} bytes")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntakeInvalid("submission is not valid UTF-8") from exc
    try:
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except IntakeInvalid:
        raise
    except ValueError as exc:
        raise IntakeInvalid("submission is not valid JSON") from exc
    if not isinstance(document, dict):
        raise IntakeInvalid("submission must be a JSON object")
    return document


def load_submission_stream(stream: Any) -> dict[str, Any]:
    """Read one bounded submission document from a binary stream.

    At most ``MAX_SUBMISSION_BYTES + 1`` bytes are read, so an unbounded writer
    cannot exhaust memory, and a trailing byte over the bound is refused. The
    document is parsed by :func:`load_submission_bytes`, so a stream gets the
    exact same strictness as a file. The stream is read as data only: nothing
    here is ever handed to a shell, and nothing is executed.
    """

    read = getattr(stream, "read", None)
    if read is None:
        raise IntakeUnavailable("submission stream is not readable")
    try:
        raw = read(MAX_SUBMISSION_BYTES + 1)
    except OSError as exc:
        raise IntakeUnavailable(
            f"submission stream cannot be read ({type(exc).__name__})"
        ) from exc
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return load_submission_bytes(raw)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _bounded_str(value: Any, field: str, limit: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise IntakeInvalid(f"{field} must be a string")
    if not allow_empty and not value:
        raise IntakeInvalid(f"{field} must be a non-empty string")
    if len(value) > limit:
        raise IntakeInvalid(f"{field} exceeds {limit} characters")
    return value


def _identity(value: Any, field: str, limit: int) -> str:
    text = _bounded_str(value, field, limit, allow_empty=False)
    if not text.isprintable() or any(ch.isspace() for ch in text):
        raise IntakeInvalid(f"{field} must be a printable, space-free identity")
    return text


def _evidence_refs(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise IntakeInvalid("evidence_refs must be a list")
    if len(value) > MAX_EVIDENCE_REFS:
        raise IntakeInvalid(
            f"evidence_refs exceeds {MAX_EVIDENCE_REFS} entries"
        )
    refs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise IntakeInvalid("each evidence_ref must be an object")
        unknown = set(item) - _REF_KEYS
        if unknown:
            raise IntakeInvalid("evidence_ref has unsupported field(s)")
        ref = _bounded_str(item.get("ref"), "evidence_ref.ref", MAX_REF,
                           allow_empty=False)
        entry = {"ref": ref}
        declared = item.get("fingerprint")
        if declared is not None:
            entry["fingerprint"] = _bounded_str(
                declared, "evidence_ref.fingerprint", MAX_REF, allow_empty=False
            )
        refs.append(entry)
    return refs


# ---------------------------------------------------------------------------
# Normalization and admission
# ---------------------------------------------------------------------------


def submission_id_for(item_id: str) -> str:
    """Return the deterministic durable id for a stable item id."""

    _identity(item_id, "item_id", MAX_ITEM_ID)
    return hashlib.sha256(item_id.encode("utf-8")).hexdigest()


def normalize_submission(
    document: Mapping[str, Any], *, clock: Any = None
) -> dict[str, Any]:
    """Validate and normalize a submission document into a durable record.

    The body is untrusted, opaque data: it is redacted and copied verbatim,
    never interpreted. Raises :class:`IntakeInvalid` on any shape violation.
    """

    if not isinstance(document, Mapping):
        raise IntakeInvalid("submission must be a JSON object")
    unknown = set(document) - _REQUEST_KEYS
    if unknown:
        raise IntakeInvalid("submission has unsupported field(s)")
    if document.get("schema") != REQUEST_SCHEMA:
        raise IntakeInvalid(f"submission schema must be {REQUEST_SCHEMA!r}")

    item_id = _identity(document.get("item_id"), "item_id", MAX_ITEM_ID)
    body = _bounded_str(document.get("body"), "body", MAX_BODY_CHARS,
                        allow_empty=True)
    title = _bounded_str(document.get("title", ""), "title", MAX_TITLE,
                         allow_empty=True)
    source = _bounded_str(document.get("source", "operator"), "source",
                          MAX_SOURCE, allow_empty=False)
    refs = _evidence_refs(document.get("evidence_refs"))

    body_redacted, changed = redact_secrets(body)
    title, title_changed = redact_secrets(title)
    source, source_changed = redact_secrets(source)
    changed = changed or title_changed or source_changed
    clean_refs: list[dict[str, str]] = []
    for entry in refs:
        ref, ref_changed = redact_secrets(entry["ref"])
        changed = changed or ref_changed
        clean = {"ref": ref}
        if "fingerprint" in entry:
            digest, digest_changed = redact_secrets(entry["fingerprint"])
            changed = changed or digest_changed
            clean["fingerprint"] = digest
        clean_refs.append(clean)

    content = {
        "item_id": item_id,
        "title": title,
        "source": source,
        "body": body_redacted,
        "evidence_refs": clean_refs,
    }
    return {
        "schema": SUBMISSION_SCHEMA,
        "record_type": RECORD_TYPE,
        "submission_id": submission_id_for(item_id),
        "item_id": item_id,
        "title": title,
        "source": source,
        "state": INTAKE_STATE_ADMITTED,
        "fingerprint": fingerprint(content),
        "body": body_redacted,
        "body_redacted": changed,
        "evidence_refs": clean_refs,
        "admitted_at": utc_now(clock),
    }


# ---------------------------------------------------------------------------
# Private, atomic, exclusive durable storage
# ---------------------------------------------------------------------------


def intake_dir(env: Mapping[str, str] | None = None) -> str:
    """The private intake directory under the XDG CAO state root."""

    return os.path.join(state_dir(env), "intake")


def _record_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in _HEX for ch in value)
    ):
        raise IntakeInvalid("submission id must be 64 lowercase hex characters")
    return value


def submission_path(
    submission_id: str, env: Mapping[str, str] | None = None
) -> str:
    return os.path.join(intake_dir(env), _record_id(submission_id) + ".json")


def _ensure_private_dir(directory: str) -> None:
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        # ``os.makedirs`` applies ``mode`` only to the leaf it creates, so the
        # CAO state root the intake directory sits under can be left
        # group-readable. Harden that parent too: it is CAO's own state root,
        # and a private intake dir under a readable parent is still a leak of
        # the submission ids in its listing.
        parent = os.path.dirname(directory)
        if parent:
            os.chmod(parent, 0o700)
    except OSError as exc:
        raise IntakeUnavailable(
            f"intake directory is not usable ({type(exc).__name__})"
        ) from exc


def _atomic_create(path: str, text: str) -> bool:
    """Create ``path`` with ``text`` atomically, or report it already exists.

    The content is written and fsynced to a private ``0600`` temp file first,
    then hard-linked into place. ``os.link`` is atomic and fails closed on an
    existing path, so two concurrent submitters can never overwrite each other
    and a partial write is never visible at ``path``.
    """

    directory = os.path.dirname(path)
    _ensure_private_dir(directory)
    fd, temp = tempfile.mkstemp(prefix=".intake-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            return False
    except OSError as exc:
        raise IntakeUnavailable(
            f"intake record could not be written ({type(exc).__name__})"
        ) from exc
    finally:
        try:
            os.unlink(temp)
        except OSError:
            pass
    return True


def _record_from_state(data: Any, expected_id: str) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise IntakeStateError("stored submission is not a JSON object")
    if data.get("schema") != SUBMISSION_SCHEMA:
        raise IntakeStateError("stored submission has an unknown schema")
    if set(data) != _RECORD_KEYS:
        raise IntakeStateError("stored submission has an unexpected shape")
    if data.get("record_type") != RECORD_TYPE:
        raise IntakeStateError("stored record is not an intake submission")
    if data.get("submission_id") != expected_id:
        raise IntakeStateError("stored submission id does not match its file")
    if data.get("state") != INTAKE_STATE_ADMITTED:
        raise IntakeStateError("stored submission has an unknown state")
    try:
        _identity(data.get("item_id"), "item_id", MAX_ITEM_ID)
        _bounded_str(data.get("body"), "body", MAX_BODY_CHARS, allow_empty=True)
        _bounded_str(data.get("title"), "title", MAX_TITLE, allow_empty=True)
        _bounded_str(data.get("source"), "source", MAX_SOURCE, allow_empty=True)
        _bounded_str(data.get("admitted_at"), "admitted_at", 64, allow_empty=False)
        _bounded_str(
            data.get("fingerprint"), "fingerprint", 128, allow_empty=False
        )
    except IntakeInvalid as exc:
        raise IntakeStateError("stored submission is malformed") from exc
    if not isinstance(data.get("body_redacted"), bool):
        raise IntakeStateError("stored submission body_redacted is not a boolean")
    refs = data.get("evidence_refs")
    if not isinstance(refs, list):
        raise IntakeStateError("stored submission evidence_refs must be a list")
    return dict(data)


def public_view(record: Mapping[str, Any]) -> dict[str, Any]:
    """The public-safe projection of a record: no body, no private payload."""

    return {
        "record_type": record["record_type"],
        "submission_id": record["submission_id"],
        "item_id": record["item_id"],
        "title": record["title"],
        "source": record["source"],
        "state": record["state"],
        "fingerprint": record["fingerprint"],
        "body_redacted": record["body_redacted"],
        "evidence_refs": record["evidence_refs"],
        "admitted_at": record["admitted_at"],
    }


def _admission(status: str, record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": ADMISSION_SCHEMA,
        "status": status,
        "duplicate": status == "DUPLICATE",
        "submission": public_view(record),
    }


def submit(
    document: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Admit one submission, idempotently, and return its acknowledgement.

    A repeated submission of the same item with the same content yields
    ``DUPLICATE`` and the original durable id. The same item with different
    content raises :class:`IntakeConflict` and leaves the original untouched.
    Nothing is started, merged or dispatched.
    """

    record = normalize_submission(document, clock=clock)
    path = submission_path(record["submission_id"], env)
    payload = json.dumps(
        record, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False
    )
    if _atomic_create(path, payload):
        readback = read_state(path)
        if readback != record:
            raise IntakeStateError("intake record failed its read-back check")
        return _admission("ADMITTED", record)

    try:
        existing = read_state(path)
    except StateError as exc:
        raise IntakeStateError(
            f"existing intake record is unreadable ({type(exc).__name__})"
        ) from exc
    stored = _record_from_state(existing, record["submission_id"])
    if stored["fingerprint"] == record["fingerprint"]:
        return _admission("DUPLICATE", stored)
    raise IntakeConflict(
        "item id already exists with different content; refusing to merge"
    )


def list_submissions(
    env: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Public-safe metadata for every stored submission, oldest first.

    A corrupt record raises rather than being skipped or read as absent.
    """

    directory = intake_dir(env)
    if not os.path.isdir(directory):
        return []
    views: list[dict[str, Any]] = []
    try:
        names: Sequence[str] = sorted(os.listdir(directory))
    except OSError as exc:
        raise IntakeUnavailable(
            f"intake directory cannot be listed ({type(exc).__name__})"
        ) from exc
    for name in names:
        if name.startswith(".") or not name.endswith(".json"):
            continue
        submission_id = name[: -len(".json")]
        if len(submission_id) != 64 or any(ch not in _HEX for ch in submission_id):
            raise IntakeStateError("intake directory holds an unexpected file")
        path = os.path.join(directory, name)
        try:
            data = read_state(path)
        except StateError as exc:
            raise IntakeStateError(
                f"stored submission is unreadable ({type(exc).__name__})"
            ) from exc
        if data is None:
            continue
        views.append(public_view(_record_from_state(data, submission_id)))
    views.sort(key=lambda view: (view["admitted_at"], view["submission_id"]))
    return views


def read_submission(
    submission_id: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Read one full submission record, or fail closed."""

    sid = _record_id(submission_id)
    try:
        data = read_state(submission_path(sid, env))
    except StateError as exc:
        raise IntakeStateError(
            f"stored submission is unreadable ({type(exc).__name__})"
        ) from exc
    if data is None:
        raise IntakeUnavailable("no submission with that id")
    return _record_from_state(data, sid)
