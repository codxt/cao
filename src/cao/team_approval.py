"""Lightweight confirmation for CAO teams.

The operator approves the composition in a conversation or other chosen
channel. The CLI records that confirmation, not an authenticated identity.
A saved confirmation binds the immutable manifest together with the exact
model/provider selection each task would actually dispatch, so a change that
no dispatched agent would ever read cannot invalidate it.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

SCHEMA = "cao.team.confirmation.v1"


class ApprovalError(ValueError):
    code = "TEAM_APPROVAL_REQUIRED"


def policy(config: Any) -> dict[str, str]:
    raw = config.raw.get("team_approval") if config is not None else None
    if raw is None:
        return {"mode": "legacy"}
    if type(raw) is not dict or set(raw) != {"mode"} or raw["mode"] not in ("legacy", "chat"):
        raise ApprovalError("team approval mode must be legacy or chat")
    return raw


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def route_digest(config: Any) -> str:
    """Digest of the raw configuration sections that can select a provider.

    Retained for admissions recorded before the binding moved to the resolved
    per-execution routes: a confirmation saved by an earlier release must keep
    verifying against exactly the document it was recorded from.
    """

    raw = config.raw if config is not None else {}
    return _digest({k: raw.get(k) for k in (
        "provider", "task", "agent_profiles", "registry_db", "runtime_id")})


def confirmation(spec: Mapping[str, Any], config: Any,
                 routes: Any = None) -> dict[str, str]:
    if policy(config)["mode"] != "chat" or not spec.get("initiative"):
        raise ApprovalError("chat-approved teams require a named initiative")
    return {
        "schema": SCHEMA,
        "method": "operator-declared",
        "spec_sha256": _digest(spec),
        "routes_sha256": route_digest(config) if routes is None else _digest(routes),
    }


def verify_saved(spec: Mapping[str, Any], config: Any, admission: Any,
                 routes: Any = None) -> None:
    if (type(admission) is not dict or
            set(admission) != {"schema", "method", "spec_sha256", "routes_sha256"}):
        raise ApprovalError("team confirmation record is invalid")
    if (admission != confirmation(spec, config, routes)
            and admission != confirmation(spec, config)):
        raise ApprovalError("approved team or model configuration changed")
