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
    "STATE_APP_DIR",
    "state_home",
    "state_dir",
    "managed_bin_dir",
    "managed_record_path",
    "service_record_path",
    "read_managed",
    "write_managed",
    "read_service",
    "write_service",
    "default_service_record",
]

MANAGED_SCHEMA = "cao.product.managed.v0"
SERVICE_SCHEMA = "cao.product.service.v0"
STATE_APP_DIR = "cao"


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
