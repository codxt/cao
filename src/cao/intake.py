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
  are ``0700`` and files ``0600``. A written record is fsynced, then linked into
  place and the containing directory fsynced before an admission is
  acknowledged, so an acknowledgement never precedes durability.
* Records are read through a strict, bounded reader that refuses duplicate
  JSON keys and non-finite numbers, and re-derives the id and content
  fingerprint from the stored bytes. An unreadable, tampered or malformed
  record raises and is never rounded up to an empty result or a valid record.
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

__all__ = [
    "REQUEST_SCHEMA",
    "SUBMISSION_SCHEMA",
    "ADMISSION_SCHEMA",
    "INTAKE_STATE_ADMITTED",
    "RECORD_TYPE",
    "MAX_SUBMISSION_BYTES",
    "MAX_RECORD_BYTES",
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
#: A stored record re-encodes the bounded body with ``ensure_ascii``, which can
#: expand non-ASCII input up to ~3x, so the durable record is bounded more
#: generously than the submission. The bound is fixed and never derived from
#: untrusted input, and a record over it fails closed rather than being read.
MAX_RECORD_BYTES = 1024 * 1024
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

#: Assignment keys whose value is a credential. A key may be bare
#: (``token: ...``) or quoted JSON-style with either delimiter
#: (``"token": "..."`` or ``'token': '...'``).
_SECRET_KEY = (
    r"(password|passwd|pwd|secret|token|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|authorization|client[_-]?secret)"
)
#: An optional closing quote after the key name, in either delimiter. The
#: opening quote is outside the key match; without this, a single-quoted key
#: (``'token' = ...``) slips past every assignment rule entirely.
_KEY_CLOSE_QUOTE = r"[\"']?"

#: Order is load-bearing. Known provider tokens and the private-key block are
#: replaced first. ``Bearer`` is replaced *before* the generic assignment rule,
#: so an ``Authorization: Bearer <token>`` line can never be reduced to
#: ``Authorization: [REDACTED] <token>`` with the credential left as a suffix.
#: A quoted value is replaced whole (so an inner space cannot leak a suffix),
#: and a bare value is consumed to the end of its line, comma or semicolon.
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
    (re.compile(r"(?i)\bBearer\s+[^\s,;\"']{3,}"), "Bearer [REDACTED]"),
    (
        re.compile(
            rf"(?i)\b{_SECRET_KEY}\b({_KEY_CLOSE_QUOTE}\s*[:=]\s*)"
            r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')"
        ),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(
            rf"(?i)\b{_SECRET_KEY}\b({_KEY_CLOSE_QUOTE}\s*[:=]\s*)"
            r"([^\r\n,;]{1,})"
        ),
        r"\1\2[REDACTED]",
    ),
)


def redact_secrets(text: str) -> tuple[str, bool]:
    """Return ``(redacted_text, changed)`` with obvious secrets removed.

    Complete credential values are consumed, never split: a ``Bearer`` token is
    redacted before any assignment rule can leave it behind as a suffix.
    """

    changed = False
    for pattern, replacement in _REDACTIONS:
        text, count = pattern.subn(replacement, text)
        if count:
            changed = True
    return text, changed


def looks_like_credential(text: str) -> bool:
    """Whether ``text`` contains anything the redactor would have to redact."""

    if not isinstance(text, str):
        return False
    _, changed = redact_secrets(text)
    return changed


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
    if field == "item_id" and looks_like_credential(text):
        # The item id is a durable, public-safe identity that names the record.
        # A credential-shaped id would leak a secret through every listing, so
        # it is refused rather than silently redacted into a different id.
        raise IntakeInvalid("item_id must not be credential-shaped")
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


def _dir_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _harden_owned_dir(directory: str) -> None:
    """Require ``directory`` to be a real ``0700`` directory owned by this user.

    The directory is opened with ``O_NOFOLLOW``/``O_DIRECTORY`` and hardened
    through the descriptor, so a symlink or a non-directory cannot be followed
    and a directory owned by someone else is never chmod'ed through.
    """

    try:
        fd = os.open(directory, _dir_open_flags())
    except OSError as exc:
        raise IntakeUnavailable(
            f"intake directory is not usable ({type(exc).__name__})"
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise IntakeUnavailable("intake directory is not a real directory")
        if info.st_uid != os.getuid():
            raise IntakeUnavailable("intake directory is not owned by this user")
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    except OSError as exc:
        raise IntakeUnavailable(
            f"intake directory is not usable ({type(exc).__name__})"
        ) from exc
    finally:
        os.close(fd)


def _ensure_private_dir(directory: str) -> None:
    """Create and harden the private intake directory and its CAO state parent.

    ``os.makedirs`` applies ``mode`` only to the leaf it creates, so the CAO
    state root above the intake directory is created and hardened too: a
    private intake dir under a readable parent still leaks the submission ids in
    its listing. Both directories are validated as real, owned ``0700``
    directories, so a symlinked or foreign path is refused rather than followed.
    """

    parent = os.path.dirname(directory)
    if parent:
        try:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise IntakeUnavailable(
                f"intake directory is not usable ({type(exc).__name__})"
            ) from exc
        _harden_owned_dir(parent)
    try:
        os.mkdir(directory, mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise IntakeUnavailable(
            f"intake directory is not usable ({type(exc).__name__})"
        ) from exc
    _harden_owned_dir(directory)


def _fsync_dir(directory: str) -> None:
    """Fsync a directory so a just-linked name survives a crash."""

    fd = os.open(directory, _dir_open_flags())
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_create(path: str, text: str) -> bool:
    """Create ``path`` with ``text`` atomically, or report it already exists.

    The content is written and fsynced to a private ``0600`` temp file first,
    then hard-linked into place and the containing directory fsynced.
    ``os.link`` is atomic and fails closed on an existing path, so two
    concurrent submitters can never overwrite each other and a partial write is
    never visible at ``path``. The directory fsync happens before this returns
    ``True``, so the caller only acknowledges an admission after the name is
    durable; a failure here raises, and an honest retry finds the record and
    reconciles it as a duplicate.
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
            # The record already exists, so this is the duplicate/retry path.
            # The link that created it may still be un-durable, so the
            # containing directory is synced here too and a failure propagates
            # rather than acknowledging a duplicate that might not survive.
            _fsync_dir(directory)
            return False
        _fsync_dir(directory)
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


def _open_dir_or_none(directory: str) -> int | None:
    """Open the intake directory read-only under a validated CAO state root.

    The CAO state root above the intake directory is opened first with
    ``O_NOFOLLOW`` and required to be a real, owned ``0700`` directory; the
    intake directory is then opened *relative to that validated descriptor*, so
    a symlinked, dangling, foreign-owned or world-readable root fails closed
    instead of being silently followed. The leaf is likewise no-follow checked
    as an owned ``0700`` directory. Only a genuinely absent path returns
    ``None``, so a caller never mistakes invalid state for first-run absence.
    """

    root = os.path.dirname(directory)
    if not root:
        raise IntakeUnavailable("intake directory has no CAO state root")
    try:
        root_fd = os.open(root, _dir_open_flags())
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IntakeUnavailable(
            f"CAO state root cannot be opened ({type(exc).__name__})"
        ) from exc
    try:
        root_info = os.fstat(root_fd)
        if not stat.S_ISDIR(root_info.st_mode):
            raise IntakeStateError("CAO state root is not a directory")
        if root_info.st_uid != os.getuid():
            raise IntakeStateError("CAO state root is not owned by this user")
        if stat.S_IMODE(root_info.st_mode) != 0o700:
            raise IntakeStateError("CAO state root has unsafe permissions")
        try:
            fd = os.open(
                os.path.basename(directory), _dir_open_flags(), dir_fd=root_fd
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise IntakeUnavailable(
                f"intake directory cannot be opened ({type(exc).__name__})"
            ) from exc
    finally:
        os.close(root_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise IntakeStateError("intake path is not a directory")
        if info.st_uid != os.getuid():
            raise IntakeStateError("intake directory is not owned by this user")
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise IntakeStateError("intake directory has unsafe permissions")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_record_at(dir_fd: int, name: str) -> int | None:
    """Open one stored record read-only, no-follow, relative to ``dir_fd``.

    The file is opened with ``O_NOFOLLOW`` and ``O_NONBLOCK``: without
    ``O_NONBLOCK`` a FIFO substituted for a record would block the open before
    its type can be checked, so the reader could hang. The opened file is
    fstat-checked: it must be a regular file, owned by this user and mode
    ``0600``. A symlink, a device, a FIFO, a foreign file or an unsafe mode
    fails closed. ``None`` means the name is genuinely absent.
    """

    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IntakeUnavailable(
            f"stored submission cannot be opened ({type(exc).__name__})"
        ) from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise IntakeStateError("stored submission is not a regular file")
    if info.st_uid != os.getuid():
        os.close(fd)
        raise IntakeStateError("stored submission is not owned by this user")
    if stat.S_IMODE(info.st_mode) != 0o600:
        os.close(fd)
        raise IntakeStateError("stored submission has unsafe permissions")
    if info.st_size > MAX_RECORD_BYTES:
        os.close(fd)
        raise IntakeStateError("stored submission exceeds the record bound")
    return fd


def _read_bounded_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError as exc:
            raise IntakeStateError(
                f"stored submission cannot be read ({type(exc).__name__})"
            ) from exc
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RECORD_BYTES:
            raise IntakeStateError("stored submission exceeds the record bound")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_record_bytes(raw: bytes) -> dict[str, Any]:
    """Parse stored record bytes with the same strictness as a submission.

    Duplicate keys, non-finite numbers and non-object roots are refused; the
    bytes must be valid UTF-8. Every failure is :class:`IntakeStateError`.
    """

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntakeStateError("stored submission is not valid UTF-8") from exc
    try:
        data = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except IntakeInvalid as exc:
        raise IntakeStateError("stored submission is not strict JSON") from exc
    except ValueError as exc:
        raise IntakeStateError("stored submission is not valid JSON") from exc
    if not isinstance(data, dict):
        raise IntakeStateError("stored submission is not a JSON object")
    return data


def _read_record_data_at(dir_fd: int, name: str) -> dict[str, Any] | None:
    fd = _open_record_at(dir_fd, name)
    if fd is None:
        return None
    try:
        raw = _read_bounded_fd(fd)
    finally:
        os.close(fd)
    return _parse_record_bytes(raw)


def _read_record(
    directory: str, name: str, expected_id: str
) -> dict[str, Any] | None:
    """Read and verify one stored record, or ``None`` when it is absent."""

    dir_fd = _open_dir_or_none(directory)
    if dir_fd is None:
        return None
    try:
        data = _read_record_data_at(dir_fd, name)
    finally:
        os.close(dir_fd)
    if data is None:
        return None
    return _record_from_state(data, expected_id)


def _record_from_state(data: Any, expected_id: str) -> dict[str, Any]:
    """Verify one stored record against its file name and its own content.

    Beyond shape and schema, two bindings are re-derived from the stored bytes
    and must hold, so tampering any field fails closed:

    * ``submission_id`` must equal ``sha256(item_id)`` -- the id binds the item
      it names; and
    * the canonical content ``fingerprint`` is recomputed over the stored
      ``item_id``, ``title``, ``source``, ``body`` and ``evidence_refs`` and
      must equal the stored one -- a changed body, id or evidence ref cannot be
      presented as the record it claims to be.
    """

    if not isinstance(data, Mapping):
        raise IntakeStateError("stored submission is not a JSON object")
    if data.get("schema") != SUBMISSION_SCHEMA:
        raise IntakeStateError("stored submission has an unknown schema")
    if set(data) != _RECORD_KEYS:
        raise IntakeStateError("stored submission has an unexpected shape")
    if data.get("record_type") != RECORD_TYPE:
        raise IntakeStateError("stored record is not an intake submission")
    if data.get("state") != INTAKE_STATE_ADMITTED:
        raise IntakeStateError("stored submission has an unknown state")
    try:
        declared_id = _record_id(data.get("submission_id"))
        item_id = _identity(data.get("item_id"), "item_id", MAX_ITEM_ID)
        body = _bounded_str(data.get("body"), "body", MAX_BODY_CHARS,
                            allow_empty=True)
        title = _bounded_str(data.get("title"), "title", MAX_TITLE,
                             allow_empty=True)
        source = _bounded_str(data.get("source"), "source", MAX_SOURCE,
                              allow_empty=True)
        _bounded_str(data.get("admitted_at"), "admitted_at", 64,
                     allow_empty=False)
        stored_fingerprint = _bounded_str(
            data.get("fingerprint"), "fingerprint", 128, allow_empty=False
        )
    except IntakeInvalid as exc:
        raise IntakeStateError("stored submission is malformed") from exc
    if declared_id != expected_id:
        raise IntakeStateError("stored submission id does not match its file")
    if submission_id_for(item_id) != declared_id:
        raise IntakeStateError("stored submission id does not bind its item id")
    if not isinstance(data.get("body_redacted"), bool):
        raise IntakeStateError("stored submission body_redacted is not a boolean")
    raw_refs = data.get("evidence_refs")
    if not isinstance(raw_refs, list):
        raise IntakeStateError("stored submission evidence_refs must be a list")
    try:
        refs = _evidence_refs(raw_refs)
    except IntakeInvalid as exc:
        raise IntakeStateError("stored submission evidence_refs is malformed") from exc
    if refs != raw_refs:
        # Canonicalizing must not launder the stored bytes: an added
        # ``fingerprint: null`` (or any other dropped key) would otherwise be
        # fingerprinted away and returned as part of the raw record. The stored
        # refs must already be exactly canonical, so absent and null differ.
        raise IntakeStateError("stored submission evidence_refs is not canonical")
    content = {
        "item_id": item_id,
        "title": title,
        "source": source,
        "body": body,
        "evidence_refs": refs,
    }
    if fingerprint(content) != stored_fingerprint:
        raise IntakeStateError(
            "stored submission content does not match its fingerprint"
        )
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
    submission_id = record["submission_id"]
    path = submission_path(submission_id, env)
    payload = json.dumps(
        record, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False
    )
    if _atomic_create(path, payload):
        readback = _read_record(
            intake_dir(env), submission_id + ".json", submission_id
        )
        if readback != record:
            raise IntakeStateError("intake record failed its read-back check")
        return _admission("ADMITTED", record)

    stored = _read_record(intake_dir(env), submission_id + ".json", submission_id)
    if stored is None:
        raise IntakeUnavailable("existing intake record disappeared before read")
    if stored["fingerprint"] == record["fingerprint"]:
        return _admission("DUPLICATE", stored)
    raise IntakeConflict(
        "item id already exists with different content; refusing to merge"
    )


def list_submissions(
    env: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Public-safe metadata for every stored submission, oldest first.

    An absent intake directory is an empty list. An existing but invalid path
    -- a symlink, a non-directory or a foreign-owned directory -- raises, and a
    corrupt record raises rather than being skipped or read as absent.
    """

    directory = intake_dir(env)
    dir_fd = _open_dir_or_none(directory)
    if dir_fd is None:
        return []
    try:
        try:
            names: Sequence[str] = sorted(os.listdir(dir_fd))
        except OSError as exc:
            raise IntakeUnavailable(
                f"intake directory cannot be listed ({type(exc).__name__})"
            ) from exc
        views: list[dict[str, Any]] = []
        for name in names:
            if name.startswith(".") or not name.endswith(".json"):
                continue
            submission_id = name[: -len(".json")]
            if len(submission_id) != 64 or any(
                ch not in _HEX for ch in submission_id
            ):
                raise IntakeStateError("intake directory holds an unexpected file")
            data = _read_record_data_at(dir_fd, name)
            if data is None:
                continue
            views.append(public_view(_record_from_state(data, submission_id)))
    finally:
        os.close(dir_fd)
    views.sort(key=lambda view: (view["admitted_at"], view["submission_id"]))
    return views


def read_submission(
    submission_id: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Read one full submission record, or fail closed."""

    sid = _record_id(submission_id)
    record = _read_record(intake_dir(env), sid + ".json", sid)
    if record is None:
        raise IntakeUnavailable("no submission with that id")
    return record
