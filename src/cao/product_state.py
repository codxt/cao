"""Durable CAO product state.

Product state lives under the XDG state root (``$XDG_STATE_HOME/cao``), kept
separate from the Work Registry ledger (``cao-work-registry``) and from the
managed runtime's own configuration, so CAO never shares a state store with
anything else it observes or manages.

Reads and writes go through :mod:`cao.state`: an unreadable or malformed state
file raises rather than being treated as absent, and a write is atomic and
read back.

Two records are owned here:

``managed``
    The outcome of managing the runtime dependency (installed, adopted,
    pending, ...). Written by the bootstrap; read by ``doctor``.

``service``
    The desired/observed service status. This module owns its vocabulary and
    default; the orchestration lane is the only writer of a live status, so on
    a fresh clone the record is simply absent and the service is honestly
    reported as not started.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from . import state as state_module

__all__ = [
    "MANAGED_SCHEMA",
    "SERVICE_SCHEMA",
    "TASK_SCHEMA",
    "STATE_APP_DIR",
    "TASKS_DIRNAME",
    "state_home",
    "state_dir",
    "managed_bin_dir",
    "managed_record_path",
    "service_record_path",
    "tasks_dir",
    "task_record_path",
    "read_managed",
    "write_managed",
    "read_service",
    "write_service",
    "read_task",
    "write_task",
    "update_task",
    "write_checkpoint",
    "list_tasks",
    "latest_task",
    "default_service_record",
]

MANAGED_SCHEMA = "cao.product.managed.v0"
SERVICE_SCHEMA = "cao.product.service.v0"
TASK_SCHEMA = "cao.product.task.v0"
STATE_APP_DIR = "cao"
TASKS_DIRNAME = "tasks"


def state_home(env: Mapping[str, str] | None = None) -> str:
    """The XDG state root (``XDG_STATE_HOME`` or ``~/.local/state``)."""

    source = os.environ if env is None else env
    base = source.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.abspath(base)


def state_dir(env: Mapping[str, str] | None = None) -> str:
    return os.path.join(state_home(env), STATE_APP_DIR)


def managed_bin_dir(env: Mapping[str, str] | None = None) -> str:
    """Where CAO keeps binaries it manages, isolated from any unrelated copy."""

    return os.path.join(state_dir(env), "managed")


def managed_record_path(env: Mapping[str, str] | None = None) -> str:
    return os.path.join(state_dir(env), "managed.json")


def service_record_path(env: Mapping[str, str] | None = None) -> str:
    return os.path.join(state_dir(env), "service.json")


def read_managed(env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    return state_module.read_state(managed_record_path(env))


def write_managed(
    record: Mapping[str, Any], env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    path = managed_record_path(env)
    if path and not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    return state_module.write_state(path, record)


def read_service(env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    return state_module.read_state(service_record_path(env))


def write_service(
    record: Mapping[str, Any], env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    path = service_record_path(env)
    if path and not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    return state_module.write_state(path, record)


def default_service_record() -> dict[str, Any]:
    """The honest default before any service has ever started."""

    return {
        "schema": SERVICE_SCHEMA,
        "desired": "stopped",
        "observed": "not_started",
    }


# ---------------------------------------------------------------------------
# Task records (task-orchestration lane)
# ---------------------------------------------------------------------------


def tasks_dir(env: Mapping[str, str] | None = None) -> str:
    return os.path.join(state_dir(env), TASKS_DIRNAME)


def _task_id(task_id: Any) -> str:
    if not isinstance(task_id, str) or not task_id:
        raise state_module.StateError("task id must be a non-empty string")
    if any(ch in task_id for ch in "/\\") or task_id in (".", ".."):
        raise state_module.StateError("task id must not contain path separators")
    return task_id


def task_record_path(task_id: str, env: Mapping[str, str] | None = None) -> str:
    return os.path.join(tasks_dir(env), _task_id(task_id) + ".json")


def read_task(task_id: str, env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """Read one task record, or ``None`` when it does not exist.

    An unreadable or malformed record raises; it is never treated as absent.
    """

    return state_module.read_state(task_record_path(task_id, env))


def write_task(
    record: Mapping[str, Any], env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Atomically write a task record whose schema and id it carries."""

    if not isinstance(record, Mapping):
        raise state_module.StateError("task record must be a mapping")
    if record.get("schema") != TASK_SCHEMA:
        raise state_module.StateError(f"task record schema must be {TASK_SCHEMA!r}")
    task_id = _task_id(record.get("task_id"))
    path = task_record_path(task_id, env)
    if path and not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    return state_module.write_state(path, record)


def update_task(
    task_id: str, patch: Mapping[str, Any], env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Merge ``patch`` into an existing task record and persist it."""

    record = read_task(task_id, env)
    if record is None:
        raise state_module.StateError(f"no task record for {task_id!r}")
    updated = dict(record)
    updated.update(dict(patch))
    return write_task(updated, env)


def write_checkpoint(
    task_id: str, checkpoint: Mapping[str, Any], env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Persist the task checkpoint before the effect it describes."""

    if not isinstance(checkpoint, Mapping):
        raise state_module.StateError("checkpoint must be a mapping")
    return update_task(task_id, {"checkpoint": dict(checkpoint)}, env)


def list_tasks(env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Every task record, ordered oldest-first by ``updated_at``.

    A corrupt record raises rather than being skipped.
    """

    directory = tasks_dir(env)
    if not os.path.isdir(directory):
        return []
    records: list[dict[str, Any]] = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        record = state_module.read_state(os.path.join(directory, name))
        if record is not None:
            records.append(record)
    records.sort(key=lambda item: (item.get("updated_at") or "", item.get("task_id") or ""))
    return records


def latest_task(env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    records = list_tasks(env)
    return records[-1] if records else None
