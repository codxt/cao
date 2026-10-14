"""Truthful request-to-resolution projection and mock outbound gate (B5).

B5 is the *read* capstone of the governed intake middle. It derives a truthful
requester status and an accountable **next mover** from evidence that already
exists -- the B1 admission, the B3 advisory packet, the B4 reviewable proposal,
the existing CAO team checkpoint, the shared Work Registry and an optional
authenticated acceptance source -- and it offers a strictly gated, no-egress
outbound simulation on the originating channel.

It is deliberately small in effect and holds **no execution authority**:

* it is **read-only**. It starts, merges, dispatches, schedules, claims, sends
  and deploys nothing, writes no Registry row, writes no canonical Company
  State, creates no queue and keeps no second ledger. Repeating it is idempotent
  because it persists nothing at all;
* a derived status is **advisory**. It is not acceptance, not an answer and not
  canonical truth. ``VERIFIED_ANSWER``, ``COMPLETION`` and ``LATER_OUTCOME`` are
  emitted **only** when an *authenticated* acceptance source positively reports
  the matching outcome, and only ever **closed downward**: an advisory
  ``ANSWER_CANDIDATE``, a ``COMPLETE`` team summary or a green external check is
  never enough to claim closure. Where acceptance cannot be established the
  status stays ``WAITING_REVIEW`` (or ``UNKNOWN``) and the reviewer/operator is
  named as the next mover;
* external PR/review state is **not locally readable** in this build, so it is
  reported ``UNKNOWN`` rather than invented. B5 adds no GitHub client, no
  network call and no provider credential;
* outbound followups, answers and event receipts are **simulated only**. B1/B2
  source provides no authenticated delivery identity and no outbound credential,
  so an unqualified real send is an explicit ``UNAVAILABLE``. Only a
  deterministic ``--dry-run`` (``egress = "none"``, ``sent = False``) or an
  explicit ``--mock`` simulated adapter (``sent = False``, ``simulation = True``)
  is served, and only after a separate operator authorizing event bound to the
  verified recipient/context. There is no custom MCP server, HTTP service, email,
  Slack, Jira, ServiceNow API, secret or background scheduler anywhere here;
* the optional **guest view** is a redacted **projection** of one submission to
  an externally authenticated caller whose trusted principal and submission
  scope are proven by the operator's control surface. With no host identity it
  fails closed ``UNAVAILABLE``; it never leaks another scope, never becomes a
  second source of truth and never persists a public index.

Standard library only; no third-party dependencies.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from typing import Any

from . import intake, intake_compile, intake_triage, product_config, registry, team
from .evidence import canonical_json, fingerprint, utc_now

__all__ = [
    "STATUS_SCHEMA",
    "GUEST_SCHEMA",
    "SEND_SCHEMA",
    "SEND_AUTH_SCHEMA",
    "STATUSES",
    "SEND_KINDS",
    "HOST_PRINCIPAL_ENV",
    "HOST_SCOPE_ENV",
    "MAX_SCAN",
    "MAX_SCOPE",
    "ResolutionError",
    "ResolutionInvalid",
    "ResolutionUnavailable",
    "ResolutionRefused",
    "ResolutionStateError",
    "derive_status",
    "resolution_fingerprint",
    "guest_identity",
    "guest_projection",
    "send_id_for",
    "reply",
]

#: The schema of one derived, read-only requester status projection.
STATUS_SCHEMA = "cao.intake.status.v1"
#: The schema of one redacted guest projection (no canonical state).
GUEST_SCHEMA = "cao.intake.status.guest.v1"
#: The schema of one outbound reply outcome.
SEND_SCHEMA = "cao.intake.send.v1"
#: The strict schema of the separate operator authorizing event.
SEND_AUTH_SCHEMA = "cao.intake.send-auth.v1"

#: The complete status vocabulary. Every value is reachable only from the
#: evidence that supports it; the three closure values require an authenticated
#: acceptance source and are never inferred from a proposal or a team summary.
STATUSES = (
    "ADMITTED",
    "CLARIFICATION_REQUESTED",
    "WORK_PROPOSED",
    "IN_PROGRESS",
    "WAITING_REVIEW",
    "VERIFIED_ANSWER",
    "COMPLETION",
    "LATER_OUTCOME",
    "DENIED_OR_REPLAN",
    "UNKNOWN",
)

#: The outbound kinds B5 may simulate. Each is a followup, an answer or an
#: event receipt on the originating channel.
SEND_KINDS = ("followup", "answer", "event")

#: The host-provisioned environment keys that carry the guest's authenticated
#: identity and authorized submission scope. They are set by the operator's
#: control surface, never by argv, a submission body or a guest-supplied JSON.
HOST_PRINCIPAL_ENV = "CAO_HOST_PRINCIPAL"
HOST_SCOPE_ENV = "CAO_HOST_PRINCIPAL_SUBMISSIONS"

#: A status derivation reads at most this many sibling packets/drafts, so an
#: on-demand projection stays bounded against a very large private store.
MAX_SCAN = 256
#: The most submissions a host may authorize for one guest principal.
MAX_SCOPE = 64

_ID_RE = re.compile(r"[0-9a-f]{64}")
_IDENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")
_STAMP_RE = re.compile(r"[0-9]{1,12}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

_AUTH_KEYS = frozenset(
    {
        "schema",
        "submission_id",
        "kind",
        "recipient",
        "context",
        "authorizer",
        "authorized_at",
        "resolution_fingerprint",
    }
)

_NOTE_NOT_ACCEPTANCE = (
    "a derived status is an advisory projection of existing evidence; it is not "
    "acceptance and never promotes a proposal, a candidate answer or a team "
    "summary to closure"
)
_NOTE_NO_AUTHORITY = (
    "B5 holds no execution authority and creates no work, claim, queue or ledger"
)


class ResolutionError(Exception):
    """Base class for B5 projection failures."""

    code = "RESOLUTION_ERROR"


class ResolutionInvalid(ResolutionError):
    """A request, an authorization or an option is malformed or unusable."""

    code = "RESOLUTION_INVALID"


class ResolutionUnavailable(ResolutionError):
    """A required capability or fact is explicitly not offered."""

    code = "RESOLUTION_UNAVAILABLE"


class ResolutionRefused(ResolutionError):
    """An authority or scope check denied the requested projection/send."""

    code = "RESOLUTION_REFUSED"


class ResolutionStateError(ResolutionError):
    """Existing state is present but not usable."""

    code = "RESOLUTION_STATE"


# ---------------------------------------------------------------------------
# Bounded validation helpers
# ---------------------------------------------------------------------------


def _bounded_str(value: Any, field: str, limit: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise ResolutionInvalid(f"{field} must be a string")
    if not allow_empty and not value:
        raise ResolutionInvalid(f"{field} must be a non-empty string")
    if len(value) > limit:
        raise ResolutionInvalid(f"{field} exceeds {limit} characters")
    if "\x00" in value:
        raise ResolutionInvalid(f"{field} must not contain a NUL byte")
    return value


def _record_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ResolutionInvalid(f"{field} must be 64 lowercase hex characters")
    return value


def _ident(value: Any, field: str) -> str:
    text = _bounded_str(value, field, 128, allow_empty=False)
    if not _IDENT_RE.fullmatch(text):
        raise ResolutionInvalid(f"{field} must be a bounded printable identity")
    return text


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ResolutionInvalid(f"{field} must be a sha256: digest")
    return value


def _stamp(value: Any, field: str) -> str:
    text = _bounded_str(value, field, 64, allow_empty=False)
    if not (_STAMP_RE.fullmatch(text)
            or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
                            text)):
        raise ResolutionInvalid(f"{field} must be a UTC timestamp")
    return text


def _mover(role: str, name: str | None, action: str, basis: str) -> dict[str, Any]:
    return {"role": role, "name": name, "action": action, "basis": basis}


# ---------------------------------------------------------------------------
# Read-only evidence collection (existing CAO state; never a new store)
# ---------------------------------------------------------------------------


def _latest_packet(submission_id: str, env: Mapping[str, str] | None) -> dict[str, Any] | None:
    """The latest B3 advisory packet for a submission, or ``None``.

    ``intake_triage`` owns and verifies its own strict private store, so a
    malformed packet raises here rather than being silently skipped. The
    duplicate scan is bounded.
    """

    candidates: list[tuple[str, str]] = []
    for view in intake_triage.list_packets(env):
        if not isinstance(view, Mapping):
            continue
        if view.get("submission_id") != submission_id:
            continue
        packet_id = view.get("packet_id")
        created_at = view.get("created_at")
        if not isinstance(packet_id, str) or not isinstance(created_at, str):
            raise ResolutionStateError("a stored triage summary is malformed")
        candidates.append((created_at, packet_id))
        if len(candidates) >= MAX_SCAN:
            break
    if not candidates:
        return None
    candidates.sort()
    return intake_triage.read_packet(candidates[-1][1], env)


def _latest_draft(packet: Mapping[str, Any], env: Mapping[str, str] | None) -> dict[str, Any] | None:
    """The latest B4 draft bound to a packet, or ``None``.

    The binding is re-checked: a draft that does not name the packet, or whose
    bound packet fingerprint differs, is refused rather than used as a stale
    proposal.
    """

    candidates: list[tuple[str, str]] = []
    for view in intake_compile.list_drafts(env):
        if not isinstance(view, Mapping):
            continue
        if view.get("packet_id") != packet["packet_id"]:
            continue
        draft_id = view.get("draft_id")
        created_at = view.get("created_at")
        if not isinstance(draft_id, str) or not isinstance(created_at, str):
            raise ResolutionStateError("a stored route draft summary is malformed")
        candidates.append((created_at, draft_id))
        if len(candidates) >= MAX_SCAN:
            break
    if not candidates:
        return None
    candidates.sort()
    draft = intake_compile.read_draft(candidates[-1][1], env)
    block = draft.get("packet")
    if (not isinstance(block, Mapping)
            or block.get("packet_id") != packet["packet_id"]
            or block.get("content_fingerprint") != packet["content_fingerprint"]):
        raise ResolutionStateError(
            "a stored draft is not bound to the referenced triage packet"
        )
    return draft


def _team_evidence(draft: Mapping[str, Any] | None,
                   env: Mapping[str, str] | None) -> dict[str, Any]:
    """A read-only snapshot of the proposal's existing team, or explicit UNKNOWN."""

    base: dict[str, Any] = {"source": "team", "team_id": None, "status": None,
                            "desired": None}
    if draft is None:
        return {**base, "verdict": "UNAVAILABLE",
                "reason": "no compiled proposal exists to reference a team"}
    team_id = draft["manifest"]["team_id"]
    base["team_id"] = team_id
    try:
        summary = team.status(team_id, env=env)
    except Exception as exc:  # noqa: BLE001 - an unreadable team is never invented
        return {**base, "verdict": "UNKNOWN",
                "reason": f"the referenced team could not be read "
                          f"({type(exc).__name__})"}
    status = summary.get("status")
    desired = summary.get("desired")
    return {
        **base,
        "verdict": "SUPPORTED",
        "reason": None,
        "status": str(status)[:32] if status is not None else None,
        "desired": str(desired)[:32] if desired is not None else None,
    }


def _registry_activity(objective: str,
                       env: Mapping[str, str] | None) -> dict[str, Any]:
    """A read-only snapshot of active Registry claims, or explicit verdict.

    The Registry is opened through ``connect_readonly`` (SQLite ``mode=ro``), so
    a write is impossible by construction and an absent database is reported
    rather than created. Only identifiers and a lexical overlap score leave this
    function.
    """

    base: dict[str, Any] = {"source": "registry", "active_claims": [],
                            "overlaps": []}
    try:
        ledger = product_config.registry_db_path(env)
    except Exception:  # noqa: BLE001 - an unresolvable location is UNKNOWN
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local Work Registry location could not be resolved"}
    if not os.path.isfile(ledger):
        return {**base, "verdict": "UNAVAILABLE",
                "reason": "no local Work Registry was found"}
    try:
        conn = registry.connect_readonly(ledger)
    except Exception:  # noqa: BLE001 - a local read failure is UNKNOWN
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local Work Registry could not be read"}
    try:
        claims = registry.list_active_claims(conn)
    except Exception:  # noqa: BLE001 - a local read failure is UNKNOWN
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local Work Registry could not be read"}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass
    tokens = registry.objective_tokens({"objective": objective})
    active: list[dict[str, Any]] = []
    overlaps: list[dict[str, Any]] = []
    for claim in claims[:64]:
        run_id = claim.get("run_id")
        work_key = claim.get("work_key")
        if not isinstance(run_id, str) or not isinstance(work_key, str):
            continue
        active.append({"run_id": run_id[:64], "work_key": work_key[:64],
                       "mode": str(claim.get("mode") or "")[:16]})
        score = registry.lexical_similarity(
            tokens, registry.objective_tokens(claim)
        )
        if score >= registry.OVERLAP_THRESHOLD:
            overlaps.append({"run_id": run_id[:64], "work_key": work_key[:64],
                             "score": round(float(score), 4)})
    return {
        **base,
        "verdict": "SUPPORTED",
        "reason": None,
        "active_claims": active,
        "overlaps": sorted(
            overlaps, key=lambda item: (-item["score"], item["run_id"],
                                        item["work_key"])
        ),
    }


def _acceptance(acceptance_source: Any, submission_id: str,
                packet: Mapping[str, Any] | None,
                draft: Mapping[str, Any] | None,
                env: Mapping[str, str] | None) -> dict[str, Any]:
    """An authenticated acceptance verdict, or explicit UNKNOWN.

    This is the **only** place a closure status may originate. The source is a
    host adapter supplied in process -- never argv, a submission body or a
    guest-supplied JSON -- and it must positively report an authenticated
    outcome. This build configures no source, so closure is never asserted.
    """

    base: dict[str, Any] = {"source": "acceptance", "kind": None,
                            "authenticated": False}
    if acceptance_source is None:
        return {**base, "verdict": "UNAVAILABLE",
                "reason": "no authenticated acceptance source is configured for "
                          "this build, so no closure can be established"}
    try:
        record = acceptance_source(
            submission_id=submission_id, packet=packet, draft=draft, env=env,
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable source is UNKNOWN
        return {**base, "verdict": "UNKNOWN",
                "reason": f"the acceptance source could not be read "
                          f"({type(exc).__name__})"}
    if record is None:
        return {**base, "verdict": "UNKNOWN",
                "reason": "the acceptance source reported no accepted outcome"}
    if not isinstance(record, Mapping) or record.get("authenticated") is not True:
        return {**base, "verdict": "UNKNOWN",
                "reason": "acceptance evidence is not authenticated"}
    kind = record.get("kind")
    if kind not in ("answer", "completion", "later"):
        return {**base, "verdict": "UNKNOWN",
                "reason": "the acceptance evidence names no supported outcome"}
    return {
        **base,
        "verdict": "SUPPORTED",
        "kind": kind,
        "authenticated": True,
        "authority": _bounded_str(record.get("authority", ""), "authority", 128,
                                  allow_empty=True),
        "reason": "an authenticated acceptance source reports the outcome",
    }


# ---------------------------------------------------------------------------
# The deterministic status decision
# ---------------------------------------------------------------------------


def _decide(
    record: Mapping[str, Any],
    packet: Mapping[str, Any],
    draft: Mapping[str, Any] | None,
    team_evidence: Mapping[str, Any],
    registry_evidence: Mapping[str, Any],
    acceptance: Mapping[str, Any],
) -> tuple[str, dict[str, Any], str, list[str]]:
    """The one deterministic decision table. First match wins.

    Every branch is grounded in already-collected evidence. Closure is offered
    only from the authenticated acceptance verdict; an advisory
    ``ANSWER_CANDIDATE``, a ``PROPOSED`` draft, a running team and a ``COMPLETE``
    team summary all fall back to ``WAITING_REVIEW`` instead of a false closure.
    """

    uncertainties: list[str] = [_NOTE_NOT_ACCEPTANCE, _NOTE_NO_AUTHORITY]
    if acceptance.get("verdict") == "SUPPORTED":
        kind = acceptance["kind"]
        if kind == "answer":
            return ("VERIFIED_ANSWER",
                    _mover("requester", None,
                           "receive the verified answer",
                           "an authenticated acceptance source verified the "
                           "answer"),
                    "verified answer",
                    uncertainties)
        if kind == "completion":
            return ("COMPLETION",
                    _mover("operator", None,
                           "record the completed outcome",
                           "an authenticated acceptance source reports "
                           "completion"),
                    "completion",
                    uncertainties)
        return ("LATER_OUTCOME",
                _mover("operator", None,
                       "record the later outcome",
                       "an authenticated acceptance source reports a later "
                       "outcome"),
                "later outcome",
                uncertainties)

    disposition = packet["disposition"]["value"]

    if disposition == "ASK_CONTEXT":
        return ("CLARIFICATION_REQUESTED",
                _mover("requester", None, "supply the requested context",
                       "the B3 advisory reports ASK_CONTEXT"),
                "clarification requested", uncertainties)

    if disposition == "DENY_OR_REPLAN":
        return ("DENIED_OR_REPLAN",
                _mover("operator", None, "deny or replan the request",
                       "the B3 advisory reports DENY_OR_REPLAN"),
                "denied or replanned", uncertainties)

    if disposition == "HUMAN_REVIEW":
        uncertainties.append(
            "the B3 advisory requires a human decision before any disposition"
        )
        return ("WAITING_REVIEW",
                _mover("operator", None,
                       "decide the request as a human reviewer",
                       "the B3 advisory reports HUMAN_REVIEW"),
                "waiting for review", uncertainties)

    if disposition == "ANSWER_CANDIDATE":
        uncertainties.append(
            "the B3 advisory answer is a candidate only and is not "
            "self-authorized; closure requires authenticated acceptance"
        )
        return ("WAITING_REVIEW",
                _mover("reviewer", None,
                       "review the candidate answer and authorize release",
                       "an ANSWER_CANDIDATE is never an accepted answer"),
                "waiting for review", uncertainties)

    # disposition == "PROPOSE_WORK"
    if draft is None:
        return ("WORK_PROPOSED",
                _mover("operator", None,
                       "review the proposal and compile or decline it",
                       "the B3 advisory proposes work and no draft exists"),
                "work proposed", uncertainties)

    owner = draft["plan"]["owner"]
    reviewer = draft["plan"]["reviewer"]
    team_status = team_evidence.get("status")
    if team_status == "COMPLETE":
        uncertainties.append(
            "the existing team reports COMPLETE, but a team summary is not "
            "acceptance and the request is not closed on it"
        )
        return ("WAITING_REVIEW",
                _mover("reviewer", reviewer,
                       "independently review the completed work and authorize "
                       "acceptance",
                       "a team COMPLETE summary is not acceptance"),
                "waiting for review", uncertainties)
    if team_status in ("BLOCKED", "CORRUPT"):
        return ("WAITING_REVIEW",
                _mover("reviewer", reviewer,
                       "unblock or recover the governed work",
                       f"the existing team reports {team_status}"),
                "waiting for review", uncertainties)
    if team_status in ("RUNNING", "PAUSED") or registry_evidence.get("overlaps"):
        return ("IN_PROGRESS",
                _mover("performer", None,
                       "complete the governed work",
                       "the existing team or an overlapping Registry claim is "
                       "active"),
                "in progress", uncertainties)
    return ("WAITING_REVIEW",
            _mover("owner", owner,
                   "review the draft and run 'cao team init --approved' before "
                   "any dispatch",
                   "a B4 draft is PROPOSED with no authority"),
            "waiting for review", uncertainties)


# ---------------------------------------------------------------------------
# Derivation entry point (read-only, idempotent)
# ---------------------------------------------------------------------------


def _status_document(
    record: Mapping[str, Any],
    packet: Mapping[str, Any] | None,
    draft: Mapping[str, Any] | None,
    team_evidence: Mapping[str, Any],
    registry_evidence: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    *,
    clock: Any,
) -> dict[str, Any]:
    if packet is None:
        status, mover, summary, uncertainties = (
            "ADMITTED",
            _mover("operator", None, "triage the admitted submission",
                   "the submission is admitted and no advisory exists"),
            "admitted", [_NOTE_NOT_ACCEPTANCE, _NOTE_NO_AUTHORITY],
        )
        disposition = None
    else:
        status, mover, summary, uncertainties = _decide(
            record, packet, draft, team_evidence, registry_evidence, acceptance
        )
        disposition = packet["disposition"]["value"]

    packet_ids: list[str] = []
    if packet is not None:
        packet_ids = [packet["packet_id"]]

    document: dict[str, Any] = {
        "schema": STATUS_SCHEMA,
        "submission_id": record["submission_id"],
        "item_id": record["item_id"],
        "status": status,
        "summary": summary,
        "next_mover": mover,
        "authority": "none",
        "advisory": True,
        "deterministic": True,
        "egress": "none",
        "derived_at": utc_now(clock),
        "fingerprint": "",
        "evidence": {
            "submission": {
                "verdict": "SUPPORTED",
                "submission_id": record["submission_id"],
                "fingerprint": record["fingerprint"],
                "state": record["state"],
            },
            "triage": {
                "verdict": "SUPPORTED" if packet is not None else "UNAVAILABLE",
                "packet_ids": packet_ids,
                "disposition": disposition,
                "reason": None if packet is not None else
                          "no B3 advisory packet exists for this submission",
            },
            "proposal": {
                "verdict": "SUPPORTED" if draft is not None else "UNAVAILABLE",
                "draft_id": draft["draft_id"] if draft is not None else None,
                "status": draft["status"] if draft is not None else None,
                "authority": "none",
                "dispatch": False,
                "owner": draft["plan"]["owner"] if draft is not None else None,
                "reviewer": draft["plan"]["reviewer"] if draft is not None else None,
                "reason": None if draft is not None else
                          "no B4 proposal is bound to the advisory",
            },
            "team": dict(team_evidence),
            "registry": dict(registry_evidence),
            "external": {
                "verdict": "UNKNOWN",
                "reason": "external PR/review/acceptance state is not locally "
                          "readable in this build; no client or network call "
                          "exists",
            },
            "acceptance": dict(acceptance),
        },
        "uncertainties": list(dict.fromkeys(uncertainties)),
    }
    document["fingerprint"] = resolution_fingerprint(document)
    return document


def _resolution_content(document: Mapping[str, Any]) -> dict[str, Any]:
    """The deterministic content of a status: everything but the time and print."""

    return {
        "submission_id": document["submission_id"],
        "status": document["status"],
        "next_mover": document["next_mover"],
        "evidence": {
            key: {
                "verdict": value.get("verdict"),
                **{k: value.get(k) for k in
                   ("packet_ids", "disposition", "draft_id", "status", "team_id",
                    "kind")
                   if k in value},
            }
            for key, value in document["evidence"].items()
        },
    }


def resolution_fingerprint(document: Mapping[str, Any]) -> str:
    """The stable fingerprint that binds a status to an authorization/send."""

    if not isinstance(document, Mapping):
        raise ResolutionInvalid("status document must be a mapping")
    return fingerprint(_resolution_content(document))


def derive_status(
    submission_id: str,
    *,
    env: Mapping[str, str] | None = None,
    acceptance_source: Any = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Derive one truthful requester status projection (read-only).

    Reads only the operator's own private intake state and locally available
    CAO evidence. It writes nothing, claims nothing, starts nothing and sends
    nothing; a repeated call returns the same projection modulo its timestamp.
    """

    sid = _record_id(submission_id, "submission id")
    try:
        record = intake.read_submission(sid, env)
    except intake.IntakeUnavailable as exc:
        raise ResolutionUnavailable(str(exc)) from exc
    except intake.IntakeError as exc:
        raise ResolutionStateError(
            f"the admitted submission is present but not usable "
            f"({type(exc).__name__})"
        ) from exc
    try:
        packet = _latest_packet(sid, env)
        draft = _latest_draft(packet, env) if packet is not None else None
    except (intake.IntakeError, intake_triage.TriageError,
            intake_compile.CompileError) as exc:
        raise ResolutionStateError(
            f"stored intake evidence is present but not usable "
            f"({type(exc).__name__})"
        ) from exc
    team_evidence = _team_evidence(draft, env)
    objective = draft["plan"]["objective"] if draft is not None else record["title"]
    registry_evidence = _registry_activity(objective, env)
    acceptance = _acceptance(acceptance_source, sid, packet, draft, env)
    document = _status_document(
        record, packet, draft, team_evidence, registry_evidence, acceptance,
        clock=clock,
    )
    if document["status"] not in STATUSES:
        raise ResolutionStateError("refusing to emit an unknown status value")
    return document


# ---------------------------------------------------------------------------
# Guest projection (operator control surface only; fail closed)
# ---------------------------------------------------------------------------


def guest_identity(env: Mapping[str, str] | None = None) -> tuple[str, frozenset[str]]:
    """The host-provisioned guest principal and its authorized submission scope.

    The principal and scope come from the operator's control surface through
    environment the guest cannot set: with no host identity this raises
    :class:`ResolutionUnavailable` rather than defaulting to any caller.
    """

    source = os.environ if env is None else env
    raw_principal = source.get(HOST_PRINCIPAL_ENV)
    if raw_principal is None or not str(raw_principal).strip():
        raise ResolutionUnavailable(
            "no host identity was provided; the guest view requires an "
            "externally authenticated principal"
        )
    principal = _ident(str(raw_principal), "host principal")
    raw_scope = source.get(HOST_SCOPE_ENV) or ""
    authorized: set[str] = set()
    for entry in str(raw_scope).split(","):
        entry = entry.strip()
        if not entry:
            continue
        if not _ID_RE.fullmatch(entry):
            raise ResolutionInvalid(
                "the host-provided authorization scope is malformed"
            )
        authorized.add(entry)
        if len(authorized) > MAX_SCOPE:
            raise ResolutionInvalid("the host-provided authorization scope is too large")
    if not authorized:
        raise ResolutionUnavailable(
            "the host identity is not authorized for any submission scope"
        )
    return principal, frozenset(authorized)


def guest_projection(
    document: Mapping[str, Any],
    *,
    principal: str,
    authorized: frozenset[str],
) -> dict[str, Any]:
    """A redacted guest projection of one status, or a refusal.

    The projection carries no body, no item id, no raw evidence payload and no
    internal identity; it carries only the truthful status and a role-level next
    mover, and only for a submission inside the guest's proven scope.
    """

    if not isinstance(document, Mapping):
        raise ResolutionInvalid("status document must be a mapping")
    submission_id = document.get("submission_id")
    if not isinstance(submission_id, str) or not _ID_RE.fullmatch(submission_id):
        raise ResolutionStateError("status document has no usable submission id")
    if submission_id not in authorized:
        raise ResolutionRefused(
            "the authenticated principal is not authorized for that submission scope"
        )
    mover = document.get("next_mover") or {}
    return {
        "schema": GUEST_SCHEMA,
        "principal": principal,
        "submission_id": submission_id,
        "status": document["status"],
        "summary": document["summary"],
        "next_mover": {
            "role": mover.get("role"),
            "action": mover.get("action"),
            "basis": mover.get("basis"),
        },
        "authority": "none",
        "note": "redacted projection only; no body, no evidence payload, no "
                "internal identity and no canonical state",
    }


# ---------------------------------------------------------------------------
# Outbound gate (no egress; simulated or dry-run only)
# ---------------------------------------------------------------------------


def _validate_auth(
    document: Any,
    record: Mapping[str, Any],
    kind: str,
    resolution_print: str,
) -> dict[str, str]:
    """Validate the separate operator authorizing event, or fail closed.

    The event is bounded operator input -- evidence, never self-authority. It
    must bind the exact submission, the exact resolution content and the
    **verified** recipient/context, which is the admitted submission's own
    originating source and never a value the authorization asserts on its own.
    """

    if type(document) is not dict or set(document) != _AUTH_KEYS:
        raise ResolutionInvalid("authorization has missing or unsupported field(s)")
    if document["schema"] != SEND_AUTH_SCHEMA:
        raise ResolutionInvalid(f"authorization schema must be {SEND_AUTH_SCHEMA!r}")
    submission_id = _record_id(document["submission_id"], "authorization.submission_id")
    if submission_id != record["submission_id"]:
        raise ResolutionInvalid("authorization names a different submission")
    declared_kind = _bounded_str(document["kind"], "authorization.kind", 16,
                                 allow_empty=False)
    if declared_kind != kind:
        raise ResolutionInvalid("authorization kind does not match the request")
    recipient = _bounded_str(document["recipient"], "authorization.recipient", 200,
                             allow_empty=False)
    context = _bounded_str(document["context"], "authorization.context", 200,
                           allow_empty=False)
    origin = record["source"]
    if recipient != origin:
        raise ResolutionRefused(
            "the authorized recipient is not the verified originating channel"
        )
    if context != origin:
        raise ResolutionRefused(
            "the authorized context is not the verified originating context"
        )
    authorizer = _ident(document["authorizer"], "authorization.authorizer")
    _stamp(document["authorized_at"], "authorization.authorized_at")
    bound = _digest(document["resolution_fingerprint"],
                    "authorization.resolution_fingerprint")
    if bound != resolution_print:
        raise ResolutionRefused(
            "the authorization is bound to a different resolution and is stale"
        )
    return {"recipient": recipient, "context": context, "authorizer": authorizer}


def send_id_for(
    submission_id: str,
    kind: str,
    recipient: str,
    context: str,
    resolution_print: str,
) -> str:
    """The deterministic idempotency key for one simulated outbound reply.

    The key is scoped to the submission, kind, verified recipient/context and the
    resolution content, so the same logical reply always maps to the same key
    across retries and no second ledger is needed to deduplicate it.
    """

    sid = _record_id(submission_id, "submission id")
    if kind not in SEND_KINDS:
        raise ResolutionInvalid("kind must be one of " + ", ".join(SEND_KINDS))
    _bounded_str(recipient, "recipient", 200, allow_empty=False)
    _bounded_str(context, "context", 200, allow_empty=False)
    _digest(resolution_print, "resolution_fingerprint")
    return hashlib.sha256(
        canonical_json(
            {
                "submission_id": sid,
                "kind": kind,
                "recipient": recipient,
                "context": context,
                "resolution_fingerprint": resolution_print,
            }
        ).encode("utf-8")
    ).hexdigest()


class MockChannelAdapter:
    """The only shipped origin-channel adapter: a deterministic simulation.

    It performs no egress whatsoever and returns a simulated acknowledgement.
    A real adapter -- and the credential it needs -- is the operator's and is
    external to CAO, so it is deliberately absent.
    """

    def send(self, key: str, recipient: str, context: str, message: str) -> dict[str, Any]:
        # A deterministic, simulated receipt. It is never a real send: the
        # caller always reports ``sent = False`` and ``simulation = True``, and
        # nothing leaves this process.
        return {
            "positive": True,
            "receipt": "simulated-" + key[:16],
            "simulation": True,
            "detail": "simulated channel adapter; nothing left this process",
        }


def _message(kind: str, status: Mapping[str, Any]) -> str:
    """A deterministic, benign reply body: status only, never the request body."""

    return (
        f"CAO intake status for {status['submission_id'][:12]}: "
        f"{status['status']} ({kind})."
    )


def reply(
    submission_id: str,
    *,
    kind: str,
    auth_file: str | None,
    dry_run: bool = False,
    mock: bool = False,
    env: Mapping[str, str] | None = None,
    acceptance_source: Any = None,
    adapter: Any = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Simulate one gated outbound reply on the originating channel.

    A real send is never available: B1/B2 source provides no authenticated
    delivery identity and no outbound credential, so ``dry_run``/``mock`` decide
    between a deterministic no-egress plan and a simulated adapter. Both report
    ``sent = False``; an ambiguous acknowledgement is an explicit
    ``UNAVAILABLE`` and is never rounded up to a send.
    """

    if kind not in SEND_KINDS:
        raise ResolutionInvalid("kind must be one of " + ", ".join(SEND_KINDS))
    if bool(dry_run) and bool(mock):
        raise ResolutionInvalid("choose either --dry-run or --mock, not both")
    sid = _record_id(submission_id, "submission id")
    record = intake.read_submission(sid, env)
    status = derive_status(sid, env=env, acceptance_source=acceptance_source,
                           clock=clock)
    if auth_file is None:
        raise ResolutionUnavailable(
            "an outbound reply requires a separate operator authorizing event"
        )
    document = intake.load_submission_file(auth_file)
    auth = _validate_auth(document, record, kind, status["fingerprint"])
    send_id = send_id_for(sid, kind, auth["recipient"], auth["context"],
                          status["fingerprint"])
    message = _message(kind, status)

    if not dry_run and not mock:
        raise ResolutionUnavailable(
            "no authenticated channel adapter or credential exists; only "
            "--dry-run or an explicit --mock simulation is available"
        )

    result: dict[str, Any] = {
        "schema": SEND_SCHEMA,
        "submission_id": sid,
        "send_id": send_id,
        "kind": kind,
        "recipient": auth["recipient"],
        "context": auth["context"],
        "authorizer": auth["authorizer"],
        "resolution_fingerprint": status["fingerprint"],
        "sent": False,
        "egress": "none",
        "authority": "none",
    }
    if dry_run:
        result.update({"status": "DRY_RUN", "simulation": False,
                       "message": message})
        return result

    channel = adapter if adapter is not None else MockChannelAdapter()
    try:
        ack = channel.send(send_id, auth["recipient"], auth["context"], message)
    except Exception as exc:  # noqa: BLE001 - a raised send is never confirmed
        raise ResolutionUnavailable(
            f"the simulated send did not complete ({type(exc).__name__})"
        ) from exc
    if not isinstance(ack, Mapping) or ack.get("positive") is not True:
        raise ResolutionUnavailable(
            "the simulated send was not acknowledged; it is not a send"
        )
    receipt = ack.get("receipt")
    if not isinstance(receipt, str) or not receipt:
        raise ResolutionUnavailable(
            "the simulated acknowledgement carried no receipt id; the outcome "
            "is ambiguous and is never confirmed"
        )
    result.update({
        "status": "SIMULATED",
        "simulation": True,
        "receipt": receipt[:128],
        "message": message,
    })
    return result
