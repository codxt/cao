"""Inbound intake bridge: a marketplace contract, not a service (B2).

This is the inbound half of intake. It takes one already-shaped submission
document from a local byte stream (stdin), admits it through
:mod:`cao.intake`, and answers with the *actual* CAO admission: the durable
submission id, its content fingerprint and its state. It is a data path, not a
network endpoint.

The design constraint is deliberate and narrow:

* **Marketplace-only.** The one registered inbound transport is the marketplace
  app transport (:data:`TRANSPORT_MARKETPLACE`). This build does not run, and
  does not pretend to run, a custom MCP server, a webhook receiver, an HTTP
  endpoint or any other public service. A caller that names any other transport
  is refused, not quietly served by a substitute.
* **Data, never a command.** The document is read as bytes and parsed as strict
  JSON. Nothing read from the stream is ever handed to a shell, interpolated
  into a command line, or executed. A body full of shell metacharacters is
  opaque content, and it is stored (after the same redaction as a file
  submission) as data.
* **Acknowledgement and reconciliation.** The answer carries the durable id and
  fingerprint. If a sender loses the answer to an ambiguous send and retries the
  same item, CAO collapses it to ``DUPLICATE`` with the *same* durable id, so
  the sender reconciles to one record instead of creating a second. The same
  item with different content is refused as a conflict.
* **No authority.** An admission here is a durable record only. Nothing is
  started, merged, dispatched, scheduled or claimed, and no permission or
  configuration of any connected external system is read or changed.

A private GitHub Issue adapter is **deferred, not implemented**: see
:data:`GITHUB_ISSUE_DEFERRAL` for the reasoning. It is reported as an explicit
deferred transport rather than silently missing.

Standard library only; no third-party dependencies.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import intake

__all__ = [
    "TRANSPORT_MARKETPLACE",
    "SUPPORTED_TRANSPORTS",
    "DEFERRED_TRANSPORTS",
    "BRIDGE_SCHEMA",
    "CONTRACT_SCHEMA",
    "GITHUB_ISSUE_DEFERRAL",
    "IntakeBridgeError",
    "UnsupportedTransport",
    "DeferredTransport",
    "bridge_contract",
    "admit_document",
    "admit_stream",
]

#: The single inbound transport this build registers: a marketplace app whose
#: action delivers a structured JSON document to the operator's already-connected
#: local adapter, which pipes those bytes to CAO on stdin. No custom MCP server,
#: no new public service.
TRANSPORT_MARKETPLACE = "marketplace.chatgpt-business"
SUPPORTED_TRANSPORTS = frozenset({TRANSPORT_MARKETPLACE})

#: Known-but-deferred transports, mapped to the truthful reason they are not
#: implemented in this build.
GITHUB_ISSUE_DEFERRAL = (
    "the private GitHub Issue adapter is deferred: reading issues requires an "
    "authenticated remote read (a credential this build must not hold) and adds "
    "a remote egress surface, while the marketplace stdin path already covers "
    "the operator's connected flow; no adapter is registered rather than a "
    "half-working one"
)
DEFERRED_TRANSPORTS: Mapping[str, str] = {"github-issue": GITHUB_ISSUE_DEFERRAL}

BRIDGE_SCHEMA = "cao.intake.bridge.v1"
CONTRACT_SCHEMA = "cao.intake.bridge.contract.v1"


class IntakeBridgeError(intake.IntakeError):
    """Base class for inbound bridge failures."""

    code = "INTAKE_BRIDGE"


class UnsupportedTransport(IntakeBridgeError):
    """The named inbound transport is not registered in this build."""

    code = "UNSUPPORTED_TRANSPORT"


class DeferredTransport(IntakeBridgeError):
    """The named inbound transport is known but deliberately not implemented."""

    code = "TRANSPORT_DEFERRED"


def bridge_contract() -> dict[str, Any]:
    """The machine-readable inbound contract a connected adapter is written to.

    It names the request schema CAO accepts, the exact bound, the one registered
    transport and the deferred ones. It is public-safe: no private value, no
    credential, no remote address.
    """

    return {
        "schema": CONTRACT_SCHEMA,
        "request_schema": intake.REQUEST_SCHEMA,
        "admission_schema": intake.ADMISSION_SCHEMA,
        "max_bytes": intake.MAX_SUBMISSION_BYTES,
        "encoding": "utf-8",
        "registered_transports": sorted(SUPPORTED_TRANSPORTS),
        "deferred_transports": sorted(DEFERRED_TRANSPORTS),
        "input": "one submission JSON document on stdin (or the same bytes in a file)",
        "executes": False,
        "starts_services": False,
    }


def _require_transport(transport: Any) -> str:
    if not isinstance(transport, str) or not transport:
        raise UnsupportedTransport("no inbound transport was named")
    if transport in DEFERRED_TRANSPORTS:
        raise DeferredTransport(DEFERRED_TRANSPORTS[transport])
    if transport not in SUPPORTED_TRANSPORTS:
        raise UnsupportedTransport(
            f"transport {transport!r} is not registered; this build accepts only "
            f"the marketplace transport {TRANSPORT_MARKETPLACE!r}"
        )
    return transport


def _acknowledge(acknowledgement: Mapping[str, Any], transport: str) -> dict[str, Any]:
    # The acknowledgement is CAO's own admission, widened by its transport. The
    # durable ``submission.submission_id`` and ``submission.fingerprint`` are
    # what a sender reconciles against after an ambiguous send.
    return {**acknowledgement, "transport": transport, "bridge_schema": BRIDGE_SCHEMA}


def admit_document(
    document: Mapping[str, Any],
    *,
    transport: str,
    env: Mapping[str, str] | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Admit an already-parsed submission document over a registered transport."""

    named = _require_transport(transport)
    return _acknowledge(intake.submit(document, env=env, clock=clock), named)


def admit_stream(
    stream: Any,
    *,
    transport: str,
    env: Mapping[str, str] | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Read one bounded submission document from ``stream`` and admit it.

    The transport is checked *before* anything is read, so an unsupported or
    deferred transport fails closed without consuming the input.
    """

    named = _require_transport(transport)
    document = intake.load_submission_stream(stream)
    return _acknowledge(intake.submit(document, env=env, clock=clock), named)
