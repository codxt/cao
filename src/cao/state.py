"""Persistent state I/O with loud failure semantics.

CAO state files are machine written. A state file that cannot be read, or
that is present but malformed, must never be silently treated as absent or as
a valid empty state: callers must fail loudly and fail closed.

This module owns only the mechanics of reading and atomically writing JSON
object state. It does not define any state vocabulary or semantics; those
belong to the lane that owns a given state file.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "StateError",
    "StateUnreadableError",
    "StateCorruptError",
    "StateSchemaError",
    "read_state",
    "write_state",
]


class StateError(Exception):
    """Base class for state I/O failures."""

    code = "STATE_ERROR"


class StateUnreadableError(StateError):
    """The state file exists but could not be read."""

    code = "STATE_UNREADABLE"


class StateCorruptError(StateError):
    """The state file contains bytes that are not valid JSON."""

    code = "STATE_CORRUPT"


class StateSchemaError(StateError):
    """The state file is valid JSON but not a JSON object."""

    code = "STATE_SCHEMA"


def _reject_json_constant(token: str) -> Any:
    """Refuse the non-standard ``NaN``/``Infinity`` JSON constants."""

    raise ValueError(f"non-finite JSON constant is not allowed: {token}")


def read_state(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Read a JSON object state file.

    Returns ``None`` only when the file genuinely does not exist (first-run
    bootstrap). Every other failure raises:

    * unreadable (permission, I/O, a directory, an existing broken link) ->
      :class:`StateUnreadableError`
    * invalid UTF-8 or invalid JSON bytes (including non-finite constants) ->
      :class:`StateCorruptError`
    * valid JSON that is not an object -> :class:`StateSchemaError`
    """

    file_path = Path(path)
    # ``lexists`` counts an existing symlink whose target is missing, so a
    # broken link is treated as unreadable rather than as first-run absence.
    if not os.path.lexists(file_path):
        return None
    try:
        raw_bytes = file_path.read_bytes()
    except OSError as exc:
        raise StateUnreadableError(f"cannot read state file {file_path}: {exc}") from exc
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StateCorruptError(
            f"state file is not valid UTF-8: {file_path}: {exc}"
        ) from exc
    try:
        data = json.loads(raw, parse_constant=_reject_json_constant)
    except ValueError as exc:
        raise StateCorruptError(
            f"state file is not valid JSON: {file_path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise StateSchemaError(
            f"state file must contain a JSON object, got {type(data).__name__}: "
            f"{file_path}"
        )
    return data


def write_state(
    path: str | os.PathLike[str], state: Mapping[str, Any]
) -> dict[str, Any]:
    """Atomically write ``state`` and verify it by reading back.

    The write goes to a temporary file in the same directory, is flushed and
    fsynced, then atomically replaces the destination. The file is read back
    and compared; a mismatch raises :class:`StateError` rather than leaving a
    caller believing a write succeeded. Returns the persisted object.
    """

    if not isinstance(state, Mapping):
        raise StateSchemaError(
            f"state must be a mapping, got {type(state).__name__}"
        )
    snapshot = dict(state)
    try:
        payload = json.dumps(
            snapshot, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise StateSchemaError(f"state is not JSON serializable: {exc}") from exc
    # Refuse lossy Python shapes (tuples, non-string keys, ...) before touching
    # the destination, so a rejected write cannot replace valid prior bytes.
    if json.loads(payload) != snapshot:
        raise StateSchemaError(
            "state does not round-trip through JSON without loss; only "
            "JSON-shaped mappings are supported"
        )

    file_path = Path(path)
    directory = file_path.parent if str(file_path.parent) else Path(".")
    temp_name: str | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{file_path.name}.", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            raise
        os.replace(temp_name, file_path)
        temp_name = None
    except OSError as exc:
        raise StateUnreadableError(
            f"cannot write state file {file_path}: {exc}"
        ) from exc
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except OSError:
                pass

    readback = read_state(file_path)
    if readback != snapshot:
        raise StateError(f"state read-back mismatch after writing {file_path}")
    return readback
