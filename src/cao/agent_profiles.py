"""Local, operator-owned agent profiles for runtime-neutral Team Execution.

CAO selects the runtime agent kind and passes a bounded argument vector.
The runtime integration owns supported kinds and executable lookup; this
module never invokes a shell, probes accounts, or handles credentials.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_MAX_PROFILES = 32
_MAX_ARGS = 32
_MAX_ARG_LENGTH = 256
_UNSAFE = (
    "--dangerously", "--skip-permissions", "--yolo", "bypasspermissions",
    "danger-full-access", "--full-auto", "--no-sandbox",
)


class AgentProfileError(ValueError):
    """A malformed or unqualified local agent launch specification."""


def validate_profiles(value: Any) -> None:
    """Validate optional local profiles without resolving a runtime plugin."""
    if value is None:
        return
    if type(value) is not dict or len(value) > _MAX_PROFILES:
        raise AgentProfileError("agent_profiles must be a bounded object")
    for name, item in value.items():
        if type(name) is not str or not _ID.fullmatch(name):
            raise AgentProfileError("agent profile name must be a bounded identifier")
        if type(item) is not dict or set(item) - {
            "agent_kind", "read_args", "write_args", "provider_id"
        } or "agent_kind" not in item:
            raise AgentProfileError("agent profile has unknown or missing fields")
        kind = item["agent_kind"]
        if type(kind) is not str or not _ID.fullmatch(kind):
            raise AgentProfileError("agent kind must be a bounded runtime identifier")
        if "read_args" not in item and "write_args" not in item:
            raise AgentProfileError("agent profile must declare an allowed task mode")
        provider = item.get("provider_id")
        if provider is not None and (
            type(provider) is not str or not provider.strip()
            or len(provider) > 128 or not provider.isprintable()
        ):
            raise AgentProfileError("provider_id must be safe descriptive text")
        for field in ("read_args", "write_args"):
            if field not in item:
                continue
            args = item[field]
            if type(args) is not list or len(args) > _MAX_ARGS:
                raise AgentProfileError(f"{field} must be a bounded argument array")
            for arg in args:
                if (type(arg) is not str or not arg or len(arg) > _MAX_ARG_LENGTH
                        or not arg.isprintable()
                        or any(bad in arg.lower() for bad in _UNSAFE)):
                    raise AgentProfileError(f"{field} contains an unsafe argument")


def resolve_profile(
    profiles: Mapping[str, Any] | None, name: str, mode: str,
) -> tuple[str, tuple[str, ...], str | None]:
    """Return one exact, mode-specific argv; never substitute write for read."""
    if type(name) is not str or not _ID.fullmatch(name):
        raise AgentProfileError("invalid agent profile reference")
    if mode not in ("read", "write"):
        raise AgentProfileError("unsupported task mode")
    data = dict(profiles or {})
    validate_profiles(data)
    if name not in data:
        raise AgentProfileError("selected agent profile is not configured locally")
    item = data[name]
    field = "read_args" if mode == "read" else "write_args"
    if field not in item:
        raise AgentProfileError("agent profile does not authorize this task mode")
    return item["agent_kind"], tuple(item[field]), item.get("provider_id")
