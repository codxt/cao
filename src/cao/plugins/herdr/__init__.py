"""First-class Herdr runtime plugin for CAO.

Herdr is the designated first runtime integration. This module lives strictly
behind the runtime plugin boundary: CAO core never imports it, and it imports
no CAO concern other than the runtime contract, the evidence primitives, and
the shared contract types. It holds no policy, registry, or state awareness and
grants no write authority.

Transport is injected. The default transport shells out to a configurable
Herdr executable (``herdr`` by default); tests supply a fake runner and never
touch a live runtime. Nothing here bakes in host paths, usernames, sockets,
wrapper commands, tenant identity, model routes or privilege assumptions.

Supported generic capabilities
------------------------------

``capability.discovery``
    Static self-description; no I/O.

``snapshot.capture``
    Exactly one ``api snapshot`` call. The live session snapshot is filtered to
    the requested workspace and mapped to a :class:`~cao.contracts.Snapshot`.

``target.inspect``
    Exactly one ``pane get`` call for one pane target, mapped to a
    :class:`~cao.contracts.TargetInspection`.

``session.prompt``
    One ``api snapshot`` read resolves the requested session to a pane through
    the snapshot identity model (``session_id`` -> ``target_id``, the
    ``pane_id``), then exactly one ``herdr agent prompt <pane_id> <text>`` call
    submits the prompt. The acknowledgement is bound to the requested session
    and workspace, and is produced only when Herdr answers with an
    ``agent_prompted`` result for the same pane whose agent still reports the
    requested session.

    Because a write follows the read, the resolution refuses to act on any view
    it cannot trust. A snapshot with incomplete coverage (below 1.0), an absent
    session, a session matching more than one pane, or a snapshot bound to
    another workspace are each an explicit error, and none of them issues a
    prompt call. ``snapshot()`` still reports partial coverage as data; it is
    ``prompt()`` that declines to write against it.

    Two limits are deliberate. Prompt text beginning with ``-`` is refused with
    ``ValueError`` before any call, because ``herdr agent prompt <TARGET>
    <TEXT>`` passes the text as a bare argument and it cannot be proven from
    here that a leading hyphen is not parsed as an option. And once the send has
    been issued, any later failure -- including the returned-identity checks --
    raises even though the prompt may already have landed, so a caller that
    simply retries can submit the same prompt twice. There is no retry,
    idempotency or deduplication here.

Everything else stays deliberately unsupported (``UNAVAILABLE``) because it
cannot be mapped safely to the frozen generic contract against Herdr's current
protocol:

``output.observe``
    ``herdr agent read`` returns undifferentiated terminal text with no event
    boundaries. The frozen :class:`~cao.contracts.RuntimeEvent` requires a
    per-event ``timestamp`` that Herdr does not report, so synthesising events
    would invent observations.

``execution.start``
    ``herdr agent start <name> --kind <kind> --pane <id>`` requires an explicit
    ``--pane`` in an already-open pane. A :class:`~cao.contracts.StartRequest`
    carries a workspace, argv and label but no target pane, so choosing one
    would invent a selection policy the contract does not express.

``session.close``
    Herdr closes a *pane* (``herdr pane close <pane_id>``), a layout slot, not a
    session, and a :class:`~cao.contracts.SessionRef` carries no pane identity
    to target one safely; closing the layout slot to satisfy a session close
    would destroy the wrong object. For the record: ``herdr`` has no
    agent-level close -- the ``herdr agent`` subcommands are list, get, read,
    send-keys, prompt, rename, focus, wait, attach, start and explain -- so
    this stays unavailable rather than being re-litigated.

Identity mapping
----------------

For each pane in the requested workspace a snapshot entry records three
distinct identities so a caller never confuses a layout slot with a session:

* ``target_id`` — the Herdr ``pane_id`` (the addressable layout target).
* ``execution_id`` — the pane's ``terminal_id`` (stable across layout moves).
* ``session_id`` — the agent's opaque session value, only when Herdr reports
  it with ``kind == "id"``. A ``kind == "path"`` value is a filesystem path and
  is deliberately not surfaced as a session identity.

A workspace that is not present in Herdr's snapshot is an explicit not-found
error, never an empty workspace. A malformed or unsupported-protocol response
is an explicit protocol error, never a rounded-up result.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Callable, Sequence

from ...contracts import (
    Capability,
    PromptAck,
    SessionRef,
    Snapshot,
    SnapshotEntry,
    TargetInspection,
    TargetRef,
    WorkspaceId,
)
from ...evidence import fingerprint, utc_now
from ...runtime import RuntimePlugin

__all__ = [
    "PLUGIN_ID",
    "SUPPORTED_PROTOCOL",
    "HerdrRuntimePlugin",
    "HerdrError",
    "HerdrTransportError",
    "HerdrProtocolError",
    "HerdrNotFoundError",
    "create_plugin",
]

PLUGIN_ID = "herdr"
#: The Herdr wire protocol generation this plugin has been verified against.
#: A response advertising any other protocol is rejected rather than guessed at.
SUPPORTED_PROTOCOL = 22

_SNAPSHOT_COMMAND = ("api", "snapshot")
_PANE_GET_PREFIX = ("pane", "get")
_AGENT_PROMPT_PREFIX = ("agent", "prompt")

#: The only Herdr error codes this plugin acts on. A code is reflected in a
#: public message only when it is a member of this allowlist; every other
#: value (including path-shaped strings that merely end in ``_not_found``) is
#: treated as unrecognized and never echoed.
_NOT_FOUND_CODES = frozenset({"pane_not_found", "workspace_not_found"})

#: A transport runs one Herdr CLI command and returns its stdout text.
Runner = Callable[[Sequence[str]], str]


class HerdrError(RuntimeError):
    """Base class for Herdr plugin failures."""

    code = "HERDR_ERROR"


class HerdrTransportError(HerdrError):
    """The Herdr CLI could not be executed or reported a failure."""

    code = "HERDR_TRANSPORT_ERROR"


class HerdrProtocolError(HerdrError):
    """A Herdr response could not be read as the supported protocol."""

    code = "HERDR_PROTOCOL_ERROR"


class HerdrNotFoundError(HerdrError):
    """The requested workspace or target is not present in Herdr state."""

    code = "HERDR_NOT_FOUND"


def _recognized_not_found(code: Any) -> str | None:
    """Return an allowlisted not-found code, or ``None`` for anything else.

    Only codes this plugin explicitly recognizes are ever reflected. A
    path-shaped or otherwise untrusted string that merely ends in
    ``_not_found`` is *not* recognized and is never surfaced.
    """

    if isinstance(code, str) and code in _NOT_FOUND_CODES:
        return code
    return None


def _error_for_code(code: Any) -> HerdrError:
    """Build a typed error from an untrusted Herdr error ``code``.

    A recognized not-found code maps to a precise not-found failure; every
    other value, whatever its shape, yields a fixed public protocol failure.
    No untrusted value is echoed.
    """

    recognized = _recognized_not_found(code)
    if recognized is not None:
        return HerdrNotFoundError(f"Herdr reports {recognized!r}")
    return HerdrProtocolError("Herdr returned an unrecognized error response")


def _error_from_stderr(stderr: str, returncode: int) -> HerdrError:
    """Map a failed Herdr invocation to a typed error.

    Only an allowlisted structured error ``code`` is reflected. Raw stderr,
    the invoked argv, the executable path and any unrecognized code value can
    carry host paths, so none of them are echoed into the public exception.
    """

    code = None
    if stderr:
        try:
            parsed = json.loads(stderr)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, Mapping):
            error = parsed.get("error")
            if isinstance(error, Mapping):
                code = error.get("code")
    recognized = _recognized_not_found(code)
    if recognized is not None:
        return HerdrNotFoundError(f"Herdr reports {recognized!r}")
    return HerdrTransportError(f"Herdr command failed with exit code {returncode}")


def _subprocess_runner(executable: str, timeout: float) -> Runner:
    """Build the default transport over a configurable Herdr executable.

    Public failures are sanitized: neither the executable path, the argv, nor
    the raw OS error text (which can name host paths) is surfaced. The raise
    happens after the ``try``/``except`` so no unsanitized exception is chained
    as ``__cause__`` or ``__context__`` either.
    """

    def run(argv: Sequence[str]) -> str:
        failure: str | None = None
        process: subprocess.CompletedProcess[str] | None = None
        try:
            process = subprocess.run(
                [executable, *argv],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            failure = "Herdr executable was not found"
        except subprocess.TimeoutExpired:
            failure = f"Herdr command timed out after {timeout}s"
        except OSError:
            failure = "Herdr command could not be executed"
        if failure is not None:
            raise HerdrTransportError(failure)
        if process is None:  # pragma: no cover - defensive
            raise HerdrTransportError("Herdr command did not run")
        if process.returncode != 0:
            raise _error_from_stderr(process.stderr or "", process.returncode)
        return process.stdout

    return run


class HerdrRuntimePlugin(RuntimePlugin):
    """CAO's first-class integration for the Herdr terminal workspace manager."""

    runtime_name = "Herdr"
    runtime_version = f"protocol-{SUPPORTED_PROTOCOL}"

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        executable: str = "herdr",
        timeout: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if runner is not None and not callable(runner):
            raise TypeError("runner must be callable")
        self._executable = executable
        self._runner = (
            runner if runner is not None else _subprocess_runner(executable, timeout)
        )
        self._clock = clock

    @property
    def plugin_id(self) -> str:
        return PLUGIN_ID

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.DISCOVERY,
                Capability.SNAPSHOT,
                Capability.INSPECT,
                Capability.PROMPT,
            }
        )

    # -- snapshot.capture --------------------------------------------------

    def snapshot(self, workspace_id: WorkspaceId) -> Snapshot:
        result = _read_result(self._runner(_SNAPSHOT_COMMAND), "session_snapshot")
        raw = result.get("snapshot")
        if not isinstance(raw, Mapping):
            raise HerdrProtocolError("snapshot result is missing a snapshot object")
        _require_protocol(raw)

        workspaces = _require_list(raw, "workspaces")
        raw_panes = _require_list(raw, "panes")
        workspace = _find(workspaces, "workspace_id", workspace_id)
        if workspace is None:
            raise HerdrNotFoundError(
                f"workspace {workspace_id!r} is not present in the Herdr snapshot"
            )
        expected = _pane_count(workspace)
        entries = _workspace_entries(raw_panes, workspace_id)
        coverage = _coverage(expected, len(entries))
        return Snapshot(
            runtime_id=PLUGIN_ID,
            workspace_id=workspace_id,
            captured_at=utc_now(self._clock),
            fingerprint=_snapshot_fingerprint(
                workspace_id, expected, coverage, entries
            ),
            coverage=coverage,
            entries=entries,
        )

    # -- target.inspect ----------------------------------------------------

    def inspect(self, target: TargetRef) -> TargetInspection:
        result = _read_result(
            self._runner((*_PANE_GET_PREFIX, target.target_id)), "pane_info"
        )
        pane = result.get("pane")
        if not isinstance(pane, Mapping):
            raise HerdrProtocolError("pane_info result is missing a pane object")
        # Bind the result to the exact requested identity before producing any
        # positive evidence: a returned pane that is not the requested one is
        # not a hit, so the requested id is never copied through blindly.
        pane_id = _require_str(pane, "pane_id")
        if pane_id != target.target_id:
            raise HerdrProtocolError(
                f"returned pane id {pane_id!r} does not match requested "
                f"target {target.target_id!r}"
            )
        workspace_id = _require_str(pane, "workspace_id")
        if workspace_id != target.workspace_id:
            raise HerdrNotFoundError(
                f"target {pane_id!r} belongs to workspace {workspace_id!r}, "
                f"not requested workspace {target.workspace_id!r}"
            )
        return TargetInspection(
            runtime_id=PLUGIN_ID,
            workspace_id=target.workspace_id,
            target_id=pane_id,
            state=_require_state(pane),
            execution_id=_terminal_id(pane),
            session_id=_session_id(pane),
            detail=_pane_detail(pane),
        )

    # -- session.prompt ----------------------------------------------------

    def prompt(self, session: SessionRef, text: str) -> PromptAck:
        # ``agent prompt <TARGET> <TEXT>`` passes the text as a bare argument
        # with no ``--`` separator, so a leading hyphen could be parsed as an
        # option. That cannot be proven safe from here, so such text is refused
        # before any call rather than sent unverified.
        if text.startswith("-"):
            raise ValueError(
                "prompt text starting with '-' is not supported: the Herdr CLI "
                "takes it as a bare argument and could read it as an option"
            )
        snapshot = self.snapshot(session.workspace_id)
        # Resolve the requested session to a pane through the plugin's own
        # snapshot identity model. A snapshot bound to another workspace, an
        # incomplete (partially observed) snapshot, an absent session and an
        # ambiguous multi-match are each explicit errors and none of them is
        # resolved by guessing.
        if snapshot.workspace_id != session.workspace_id:
            raise HerdrNotFoundError(
                f"snapshot is for workspace {snapshot.workspace_id!r}, not "
                f"requested workspace {session.workspace_id!r}"
            )
        # A write follows this read, so the view must be complete: a partial
        # snapshot cannot prove the session is unique (a hidden pane may carry
        # the same session id), which would turn the ambiguity scan into a
        # guess. coverage is the plugin's own observed/declared fraction, and
        # anything below a full 1.0 is incomplete. ``snapshot()`` still reports
        # partial coverage as data; only this write path refuses to act on it.
        if snapshot.coverage < 1.0:
            raise HerdrProtocolError(
                f"snapshot coverage {snapshot.coverage} is incomplete; refusing "
                f"to prompt against a partially observed workspace"
            )
        target_id = _resolve_session_target(snapshot, session)
        result = _read_result(
            self._runner((*_AGENT_PROMPT_PREFIX, target_id, text)),
            "agent_prompted",
        )
        agent = result.get("agent")
        if not isinstance(agent, Mapping):
            raise HerdrProtocolError(
                "agent_prompted result is missing an agent object"
            )
        # A bound ``agent_prompted`` result is Herdr's acceptance signal; a
        # rejected submission arrives as an error envelope and never reaches
        # here. Bind the acknowledgement to the exact requested identity before
        # producing it: the returned pane must be the pane the session resolved
        # to and the returned workspace must be the requested one. The requested
        # session id is echoed from the request, never copied from the response.
        pane_id = _require_str(agent, "pane_id")
        if pane_id != target_id:
            raise HerdrProtocolError(
                f"returned pane id {pane_id!r} does not match prompted "
                f"target {target_id!r}"
            )
        workspace_id = _require_str(agent, "workspace_id")
        if workspace_id != session.workspace_id:
            raise HerdrNotFoundError(
                f"prompted pane {pane_id!r} belongs to workspace "
                f"{workspace_id!r}, not requested workspace "
                f"{session.workspace_id!r}"
            )
        # The send already happened and cannot be undone, but a false
        # acknowledgement is still avoided: if the pane's agent session changed
        # between the snapshot and the prompt, the pane no longer carries the
        # requested session and must not be reported as a success. The returned
        # session value is untrusted and is not echoed.
        if _session_id(agent) != session.session_id:
            raise HerdrProtocolError(
                f"prompted pane {pane_id!r} no longer reports requested "
                f"session {session.session_id!r}"
            )
        return PromptAck(
            session_id=session.session_id,
            accepted=True,
            detail=f"prompted pane {target_id!r}",
        )


def create_plugin() -> HerdrRuntimePlugin:
    """Entry-point factory: build a plugin with the default CLI transport."""

    return HerdrRuntimePlugin()


# -- response reading ------------------------------------------------------


def _read_result(stdout: str, expected_type: str) -> Mapping[str, Any]:
    """Parse one Herdr CLI envelope and require the expected result type.

    The JSON handler is left before the sanitized error is raised, so a
    malformed response is not retained through ``__cause__`` or
    ``__context__`` (``from None`` alone would still keep ``__context__``).
    """

    malformed = False
    envelope: Any = None
    try:
        envelope = json.loads(stdout)
    except (TypeError, ValueError):
        malformed = True
    if malformed:
        raise HerdrProtocolError("Herdr returned output that is not JSON")
    if not isinstance(envelope, Mapping):
        raise HerdrProtocolError("Herdr response must be a JSON object")
    if "error" in envelope:
        error = envelope.get("error")
        code = error.get("code") if isinstance(error, Mapping) else None
        raise _error_for_code(code)
    result = envelope.get("result")
    if not isinstance(result, Mapping):
        raise HerdrProtocolError("Herdr response is missing a result object")
    if result.get("type") != expected_type:
        # The returned type is untrusted and is deliberately omitted.
        raise HerdrProtocolError(f"expected a {expected_type!r} result")
    return result
    return result


def _require_protocol(snapshot: Mapping[str, Any]) -> None:
    protocol = snapshot.get("protocol")
    if isinstance(protocol, bool) or not isinstance(protocol, int):
        raise HerdrProtocolError("snapshot protocol must be an integer")
    if protocol != SUPPORTED_PROTOCOL:
        raise HerdrProtocolError(
            f"unsupported Herdr protocol {protocol}; this plugin supports "
            f"protocol {SUPPORTED_PROTOCOL}"
        )


def _require_list(mapping: Mapping[str, Any], key: str) -> list[Any]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise HerdrProtocolError(f"{key} must be a JSON array")
    return value


def _require_str(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise HerdrProtocolError(f"{key} must be a non-empty string")
    return value


def _find(
    items: list[Any], key: str, wanted: Any
) -> Mapping[str, Any] | None:
    for item in items:
        if isinstance(item, Mapping) and item.get(key) == wanted:
            return item
    return None


def _require_state(pane: Mapping[str, Any]) -> str:
    state = pane.get("agent_status")
    if not isinstance(state, str) or not state:
        raise HerdrProtocolError("pane agent_status must be a non-empty string")
    return state


def _terminal_id(pane: Mapping[str, Any]) -> str | None:
    value = pane.get("terminal_id")
    if isinstance(value, str) and value:
        return value
    return None


def _session_id(pane: Mapping[str, Any]) -> str | None:
    session = pane.get("agent_session")
    if not isinstance(session, Mapping):
        return None
    # Only an opaque ``id`` is a session identity. A ``path`` is a filesystem
    # location and is not surfaced as identity (it may be host-private).
    if session.get("kind") != "id":
        return None
    value = session.get("value")
    if isinstance(value, str) and value:
        return value
    return None


def _resolve_session_target(snapshot: Snapshot, session: SessionRef) -> str:
    """Resolve one requested session to its pane target via snapshot identity.

    Matching is on the snapshot's recorded ``session_id`` (the agent's opaque
    session value), and the result is the pane's ``target_id``. No match is a
    not-found error; more than one match is an explicit ambiguity error. Neither
    is ever resolved by picking a row.

    The caller must pass a *complete* snapshot (coverage 1.0): a partial listing
    cannot prove the session is unique, so the ambiguity scan below is only
    meaningful over a fully observed workspace.
    """

    matches = [
        entry
        for entry in snapshot.entries
        if entry.session_id is not None and entry.session_id == session.session_id
    ]
    if not matches:
        raise HerdrNotFoundError(
            f"session {session.session_id!r} is not present in workspace "
            f"{session.workspace_id!r}"
        )
    if len(matches) > 1:
        raise HerdrProtocolError(
            f"session {session.session_id!r} resolves to {len(matches)} panes; "
            f"refusing to guess"
        )
    return matches[0].target_id


def _pane_entry(pane: Mapping[str, Any]) -> SnapshotEntry:
    target_id = _require_str(pane, "pane_id")
    metadata: list[tuple[str, str]] = []
    terminal_id = _terminal_id(pane)
    if terminal_id is not None:
        metadata.append(("terminal_id", terminal_id))
    agent = pane.get("agent")
    if isinstance(agent, str) and agent:
        metadata.append(("agent", agent))
    return SnapshotEntry(
        target_id=target_id,
        kind="pane",
        state=_require_state(pane),
        execution_id=terminal_id,
        session_id=_session_id(pane),
        metadata=tuple(metadata),
    )


def _pane_detail(pane: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    # Opaque identifiers only; filesystem paths (e.g. cwd) are deliberately
    # omitted so inspection never surfaces host-local paths.
    detail: list[tuple[str, str]] = []
    for key in ("terminal_id", "agent", "tab_id"):
        value = pane.get(key)
        if isinstance(value, str) and value:
            detail.append((key, value))
    return tuple(detail)


def _entry_payload(entry: SnapshotEntry) -> dict[str, Any]:
    return {
        "target_id": entry.target_id,
        "kind": entry.kind,
        "state": entry.state,
        "execution_id": entry.execution_id,
        "session_id": entry.session_id,
        "metadata": [list(pair) for pair in entry.metadata],
    }


def _workspace_entries(
    raw_panes: list[Any], workspace_id: WorkspaceId
) -> tuple[SnapshotEntry, ...]:
    """Validate pane identity for every row, then select the workspace's panes.

    Every row must be an object carrying a non-empty ``workspace_id`` and
    ``pane_id``. Rows outside the workspace are filtered; duplicate targets
    inside it are rejected. Entries are ordered by target so output and
    fingerprints do not depend on Herdr's row order.
    """

    selected: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for pane in raw_panes:
        if not isinstance(pane, Mapping):
            raise HerdrProtocolError("each pane entry must be a JSON object")
        pane_workspace = _require_str(pane, "workspace_id")
        pane_id = _require_str(pane, "pane_id")
        if pane_id in seen:
            raise HerdrProtocolError(
                f"duplicate pane target {pane_id!r} in the Herdr snapshot"
            )
        seen.add(pane_id)
        if pane_workspace != workspace_id:
            continue
        selected.append(pane)
    selected.sort(key=lambda pane: pane["pane_id"])
    return tuple(_pane_entry(pane) for pane in selected)


def _snapshot_fingerprint(
    workspace_id: WorkspaceId,
    expected: int,
    coverage: float,
    entries: tuple[SnapshotEntry, ...],
) -> str:
    """Fingerprint the full qualification, not just the visible rows.

    Runtime/workspace identity, the expected pane count, the resulting
    coverage and the deterministically sorted entries are all included, so a
    change in what was qualified changes the fingerprint even when the visible
    rows are unchanged.
    """

    payload = {
        "runtime_id": PLUGIN_ID,
        "workspace_id": workspace_id,
        "expected_pane_count": expected,
        "coverage": coverage,
        "entries": sorted(
            (_entry_payload(entry) for entry in entries),
            key=lambda item: (item["target_id"], item["kind"]),
        ),
    }
    return fingerprint(payload)


def _pane_count(workspace: Mapping[str, Any]) -> int:
    pane_count = workspace.get("pane_count")
    if isinstance(pane_count, bool) or not isinstance(pane_count, int):
        raise HerdrProtocolError("workspace pane_count must be an integer")
    if pane_count < 0:
        raise HerdrProtocolError("workspace pane_count must be non-negative")
    return pane_count


def _coverage(expected: int, observed: int) -> float:
    if observed > expected:
        raise HerdrProtocolError(
            f"observed {observed} panes exceeds declared pane_count {expected}"
        )
    if expected == 0:
        return 1.0
    return observed / expected
