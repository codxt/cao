"""Governed Intake to CAO execution handoff (B6).

B6 is the **bridge** the earlier intake phases deliberately lack. B3 produces an
advisory disposition, B4 compiles a *reviewable* manifest for ``PROPOSE_WORK``
and B5 projects a truthful status, but nothing turns an admitted request into an
actual CAO team. B6 derives, from the *original* admission and its B3 advisory,
one independently reviewable **source-attested eligibility result**, and -- only
when that result qualifies an explicit, operator-qualified, scoped **READ**
execution -- one fail-closed, idempotent **handoff plan**.

It is deliberately conservative about authority and effect:

* **Mention is not request.** A reserved word in a request (``merge``, ``deploy``,
  ``release``, ``publish``, ``credential``, ``permission``, ``destructive``) is
  classified per category as ``REQUESTED``, ``AMBIGUOUS``, ``MENTIONED``,
  ``NEGATED`` or ``ABSENT`` using deterministic, bounded text rules. An explicit
  consequential effect, or an effect the rules cannot prove non-actionable, is a
  ``HOLD`` with a precise reason. A request that mixes a consequential effect
  with a read scope is **never** silently downgraded to a READ plan.
* **Authority is never invented.** The plan may only be executed through an
  **in-process** authenticated operator-authority adapter supplied by the host.
  A CLI flag, a process-environment value or a JSON file is caller-controlled and
  is **never** proof; this build ships no such adapter, so every plan is
  ``PLAN_ONLY`` and reports the exact missing operator integration.
* **No new scheduler, ledger or work.** B6 starts, ticks and schedules nothing.
  It writes no Registry row and no canonical state. When every gate proves it may
  hand one immutable READ manifest to the **existing** CAO team owner API
  (``team.validate_spec`` / ``team.initialize`` / ``team.set_desired``); the
  existing R3/R4 daemon reconciles the team from there. A held, merely-planned or
  refused handoff creates no team, no agent and no claim.
* **External provenance is validated, not trusted.** A running independent audit
  is linked only when the team id resolves, the immutable manifest is fully
  READ, and the manifest itself cites the submission; its status is read from the
  authoritative team checkpoint, never from any caller-supplied field.

Standard library only; no third-party dependencies, no network, no credential.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from typing import Any

from . import (git, intake, intake_triage, product_config, product_state,
               registry, team)
from .evidence import canonical_json, fingerprint, utc_now

__all__ = [
    "RESULT_SCHEMA",
    "PLAN_SCHEMA",
    "STATUS_SCHEMA",
    "ELIGIBILITY_SCHEMA",
    "EFFECT_CATEGORIES",
    "EFFECT_VERDICTS",
    "ELIGIBILITY_VERDICTS",
    "HANDOFF_STATUSES",
    "HandoffError",
    "HandoffInvalid",
    "HandoffUnavailable",
    "HandoffRefused",
    "HandoffStateError",
    "HandoffConflict",
    "classify_effects",
    "eligibility_fingerprint",
    "derive_eligibility",
    "plan_id_for",
    "handoff_plan",
    "handoff_status",
    "recover_plan",
    "linked_state",
    "list_plans",
    "read_plan",
    "plan_dir",
]

#: The result envelope of one handoff plan attempt (mirrors the other phases).
RESULT_SCHEMA = "cao.intake.handoff.result.v1"
#: The immutable, reviewable handoff plan record.
PLAN_SCHEMA = "cao.intake.handoff.plan.v1"
#: The read-only handoff status projection (persists nothing).
STATUS_SCHEMA = "cao.intake.handoff.status.v1"
#: The independent source-attested eligibility result.
ELIGIBILITY_SCHEMA = "cao.intake.eligibility.v1"

#: The reserved consequential-effect categories B6 distinguishes. A *mention*
#: of one of these is not automatically a request; an explicit request, or an
#: unprovable ambiguity, is a hold.
EFFECT_CATEGORIES = (
    "merge",
    "deploy",
    "release",
    "publish",
    "credential",
    "permission",
    "destructive",
)

#: The per-category classification vocabulary.
EFFECT_VERDICTS = ("REQUESTED", "AMBIGUOUS", "MENTIONED", "NEGATED", "ABSENT")

#: The eligibility outcome vocabulary. Only ``ELIGIBLE_READ`` may proceed to a
#: dispatchable plan, and only behind every remaining gate.
ELIGIBILITY_VERDICTS = ("ELIGIBLE_READ", "HOLD")

#: The handoff outcome vocabulary. Each is reachable only from its own evidence.
HANDOFF_STATUSES = (
    "HOLD",
    "ALREADY_STARTED",
    "PLAN_ONLY",
    "AUTHORIZED",
    "DISPATCH_PENDING",
    "REFUSED",
)

#: A handoff plan binds at most this many scoped repository paths.
MAX_SCOPE_PATHS = 128
#: The body text B6 reads is bounded exactly as B1 bounds an admitted body.
MAX_BODY = 128 * 1024
#: The bounded scan over sibling private packets.
MAX_SCAN = 256

_ID_RE = re.compile(r"[0-9a-f]{64}")
_IDENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}\Z")
_TEAM_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_STAMP_RE = re.compile(r"[0-9]{1,12}")
_MODEL_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:@+/#-]{0,150}\Z"
)

# ---------------------------------------------------------------------------
# Deterministic consequential-effect classification (mention vs request)
# ---------------------------------------------------------------------------

_EFFECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("merge", re.compile(
        r"\b(?:merge|merges|merged|merging|squash[ _-]?merge[sd]?)\b", re.I)),
    ("deploy", re.compile(
        r"\b(?:deploy|deploys|deployed|deploying|deployment|deployments|"
        r"redeploy|redeployed|redeployment)\b", re.I)),
    ("release", re.compile(r"\b(?:release|releases|released|releasing)\b", re.I)),
    ("publish", re.compile(
        r"\b(?:publish|publishes|published|publishing|publication|"
        r"publications)\b", re.I)),
    ("credential", re.compile(
        r"\b(?:credential|credentials|secret|secrets|api[ _-]?key|apikey|"
        r"access[ _-]?token|password|passwords)\b", re.I)),
    ("permission", re.compile(
        r"\b(?:permission|permissions|acl|acls|privilege|privileges|"
        r"role[ _-]?binding|sudo|chmod|chown)\b", re.I)),
    ("destructive", re.compile(
        r"\b(?:delete|deletes|deleted|deleting|deletion|remove|removes|"
        r"removed|removing|removal|wipe|wipes|wiped|drop|drops|dropped|purge|"
        r"purges|purged|destroy|destroys|destroyed|revoke|revokes|revoked|"
        r"revocation|decommission|decommissions|decommissioned|rm -rf)\b", re.I)),
)

#: A clause that BEGINS with a prohibition is a stated limit, not a request.
_NEG_START = re.compile(
    r"^(?:no|not|never|without|do not|don't|must not|cannot|avoid|refrain|"
    r"exclude|excluded|hold|stop)\b", re.I)
#: An explicit prohibition anywhere in the clause.
_NEG_ANY = re.compile(
    r"\b(?:do not|don't|never|must not|cannot|avoid|refrain from|without|"
    r"prohibit|forbid)\b", re.I)
#: A local negation immediately before one effect token ("no new deployment").
_NEG_LOCAL = re.compile(r"\b(?:no|not|never|without|non|zero)\s+(?:\w+[ _-]){0,4}\Z",
                        re.I)
#: Request/directive framing. These are the *only* neutral markers that turn an
#: effect word into an asked-for action; the effect verbs themselves are never
#: framing, so a compound reference such as "release notes" stays a mention.
_REQUEST = re.compile(
    r"\b(?:please|seek|require|requires|required|perform|request|requested|"
    r"requesting|execute|run|apply|complete|finish|proceed|then|next action|"
    r"exactly one|single|one|ensure|submit|issue|close|open|make)\b", re.I)
#: How far before an effect word a framing marker must appear to be its request.
_REQUEST_WINDOW = 48
#: A compound/reference form that names an existing artifact rather than an act.
_QUALIFIED = re.compile(
    r"\b(?:merge|release|deploy|publish|credential|permission)s?[ _-]"
    r"(?:note|notes|fingerprint|fingerprints|surface|surfaces|gate|gates|"
    r"candidate|cadence|plan|plans|record|records|history|lineage|tag|tags|"
    r"commit|commits|channel|channels|workflow|workflows|shaped|only|scoped|"
    r"free|safe|evidence|checklist|policy|policies|model)\b"
    r"|\b(?:legacy|previous|prior|existing|current|original|historical|past|"
    r"old|latest|retirement|serving)\s+"
    r"(?:merge|release|deploy|publish|credential|permission)s?\b"
    r"|\bmerged[- ]main\b|\bcredential[- ]shaped\b|\brelease[- ]only\b", re.I)
#: A declared read scope. Only such a request can become an executable READ plan.
_READ_SCOPE = re.compile(
    r"\b(?:read[ _-]only|read only|audit|inspect|inspection|verify|"
    r"verification|revalidate|revalidation|diagnose|diagnosis|assess|"
    r"assessment|report|reporting|classify|evidence|gap|check|checks|confirm|"
    r"examine|survey)\b", re.I)

_CLAUSE_SPLIT = re.compile(r"[.;\n\u2022]")

_NOTE_NOT_AUTHORITY = (
    "a handoff plan is advisory until an authenticated operator authority grants "
    "the exact scoped execution; it is never authority by itself"
)
_NOTE_READ_ONLY_PLAN = (
    "B6 starts, ticks, schedules and claims nothing; the existing daemon "
    "reconciles any team this plan would initialize"
)
_NO_OPERATOR_AUTHORITY = (
    "no authenticated operator-authority adapter exists in this build; a CLI "
    "flag, an environment variable or a JSON file is caller-controlled and is "
    "never proof of operator identity or grant"
)
_OPERATOR_INTEGRATION = (
    "supply an in-process adapter through the library 'operator_authority' "
    "argument that (1) authenticates the operator on a host-owned channel "
    "outside argv/env/JSON, (2) returns a grant bound to this plan's exact "
    "plan_digest with read_only=true, and (3) names the operator, the scoped "
    "repository, the role, the model and the provider privacy qualification; "
    "the CLI never constructs it and this build ships none"
)

_AUTHORITY_GRANT_KEYS = frozenset(
    {
        "plan_digest",
        "read_only",
        "owner",
        "scope",
        "role",
        "model",
        "privacy_qualified",
        "granted_at",
    }
)


# ---------------------------------------------------------------------------
# Exceptions and small validation helpers
# ---------------------------------------------------------------------------


class HandoffError(Exception):
    """Base class for B6 handoff failures."""

    code = "HANDOFF_ERROR"


class HandoffInvalid(HandoffError):
    """A request, an option or an authority record is malformed."""

    code = "HANDOFF_INVALID"


class HandoffUnavailable(HandoffError):
    """A required capability or fact is explicitly not offered."""

    code = "HANDOFF_UNAVAILABLE"


class HandoffRefused(HandoffError):
    """An authority, scope or provenance check denied the handoff."""

    code = "HANDOFF_REFUSED"


class HandoffStateError(HandoffError):
    """Existing state is present but not usable."""

    code = "HANDOFF_STATE"


class HandoffConflict(HandoffError):
    """A plan id was reused for different immutable content."""

    code = "HANDOFF_CONFLICT"


def _bounded_str(value: Any, field: str, limit: int, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise HandoffInvalid(f"{field} must be a string")
    if not allow_empty and not value:
        raise HandoffInvalid(f"{field} must be a non-empty string")
    if len(value) > limit:
        raise HandoffInvalid(f"{field} exceeds {limit} characters")
    if "\x00" in value:
        raise HandoffInvalid(f"{field} must not contain a NUL byte")
    return value


def _record_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise HandoffInvalid(f"{field} must be 64 lowercase hex characters")
    return value


def _ident(value: Any, field: str) -> str:
    text = _bounded_str(value, field, 128, allow_empty=False)
    if not _IDENT_RE.fullmatch(text):
        raise HandoffInvalid(f"{field} must be a bounded printable identity")
    return text


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise HandoffInvalid(f"{field} must be a sha256: digest")
    return value


# ---------------------------------------------------------------------------
# Effect classification (the whole point of the independent eligibility result)
# ---------------------------------------------------------------------------


def _clauses(body: str) -> list[str]:
    return [part.strip() for part in _CLAUSE_SPLIT.split(body) if part.strip()]


def classify_effects(body: str) -> dict[str, str]:
    """Classify each reserved effect category in opaque request text.

    The rules are deterministic and bounded; the body is inspected as data and
    never executed or interpolated into a shell. A category is ``REQUESTED``
    when an effect token appears in a clause that asks for the action,
    ``NEGATED`` when the clause states a limitation, ``MENTIONED`` when the token
    is part of a compound reference to an existing artifact, ``AMBIGUOUS`` when
    none of those is provable, and ``ABSENT`` when the token does not occur. An
    ``AMBIGUOUS`` verdict is a fail-closed hold, never a silent pass.
    """

    if not isinstance(body, str):
        raise HandoffInvalid("request body must be a string")
    text = body[:MAX_BODY]
    clauses = _clauses(text)
    verdicts: dict[str, str] = {}
    # Severity order, least to most consequential. The strongest verdict for a
    # category wins, so one explicit request is never masked by a later mention.
    order = ["ABSENT", "NEGATED", "MENTIONED", "AMBIGUOUS", "REQUESTED"]
    for category, pattern in _EFFECT_PATTERNS:
        best = "ABSENT"
        for clause in clauses:
            forbids = bool(_NEG_START.search(clause) or _NEG_ANY.search(clause))
            qualifies = bool(_QUALIFIED.search(clause))
            for match in pattern.finditer(clause):
                prefix = clause[: match.start()]
                imperative = not prefix.strip()
                window = prefix[-_REQUEST_WINDOW:]
                if forbids or _NEG_LOCAL.search(prefix):
                    verdict = "NEGATED"
                elif imperative or _REQUEST.search(window):
                    # A directive verb/framing outranks a compound reference, so
                    # "publish a new release" is a request, never a mention.
                    verdict = "REQUESTED"
                elif qualifies:
                    verdict = "MENTIONED"
                else:
                    verdict = "AMBIGUOUS"
                if order.index(verdict) > order.index(best):
                    best = verdict
        verdicts[category] = best
    return verdicts


def _eligibility_holds(
    effects: Mapping[str, str], read_scoped: bool
) -> list[dict[str, Any]]:
    holds: list[dict[str, Any]] = []
    for category in EFFECT_CATEGORIES:
        verdict = effects.get(category)
        if verdict == "REQUESTED":
            holds.append(
                {
                    "code": ("mixed_effect_and_read_request" if read_scoped
                             else "explicit_consequential_effect"),
                    "category": category,
                    "reason": (
                        "the request explicitly asks for a consequential "
                        f"{category} effect, so a read scoped execution cannot "
                        "satisfy it and is never substituted for it"
                    ),
                }
            )
        elif verdict == "AMBIGUOUS":
            holds.append(
                {
                    "code": "ambiguous_consequential_effect",
                    "category": category,
                    "reason": (
                        f"the request mentions a {category} effect that cannot "
                        "be proven non-actionable, so it is held rather than "
                        "silently downgraded"
                    ),
                }
            )
    if not holds and not read_scoped:
        holds.append(
            {
                "code": "not_read_scoped_request",
                "category": None,
                "reason": (
                    "the request does not declare a read-only or read scope, so "
                    "no scoped READ execution is qualified"
                ),
            }
        )
    return holds


# ---------------------------------------------------------------------------
# Source-attested eligibility (a NEW result; B3 is never mutated)
# ---------------------------------------------------------------------------


def _eligibility_content(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "submission_id": document["submission_id"],
        "submission_fingerprint": document["evidence"]["submission_fingerprint"],
        "packet_id": document["evidence"]["packet_id"],
        "packet_fingerprint": document["evidence"]["packet_fingerprint"],
        "disposition": document["evidence"]["disposition"],
        "verdict": document["verdict"],
        "effects": document["effects"],
        "holds": document["holds"],
    }


def eligibility_fingerprint(document: Mapping[str, Any]) -> str:
    """The stable fingerprint of an eligibility result."""

    if not isinstance(document, Mapping):
        raise HandoffInvalid("eligibility document must be a mapping")
    return fingerprint(_eligibility_content(document))


def _latest_packet(
    submission_id: str, env: Mapping[str, str] | None
) -> dict[str, Any] | None:
    """The latest B3 advisory packet for a submission, or ``None``.

    ``intake_triage`` owns and verifies its own strict private store, so a
    malformed packet raises here rather than being silently skipped.
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
            raise HandoffStateError("a stored triage summary is malformed")
        candidates.append((created_at, packet_id))
        if len(candidates) >= MAX_SCAN:
            break
    if not candidates:
        return None
    candidates.sort()
    return intake_triage.read_packet(candidates[-1][1], env)


def derive_eligibility(
    submission_id: str,
    *,
    env: Mapping[str, str] | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Derive one independent, source-attested eligibility result (read-only).

    It reads the original admission through intake's strict reader and the B3
    advisory through ``intake_triage``'s strict reader, and it **writes nothing**
    and mutates neither. A missing admission, a missing advisory or unreadable
    evidence fails closed; the result is derived, never asserted.
    """

    sid = _record_id(submission_id, "submission id")
    try:
        record = intake.read_submission(sid, env)
    except intake.IntakeUnavailable as exc:
        raise HandoffUnavailable(str(exc)) from exc
    except intake.IntakeError as exc:
        raise HandoffStateError(
            f"the admitted submission is present but not usable "
            f"({type(exc).__name__})"
        ) from exc
    try:
        packet = _latest_packet(sid, env)
    except (intake.IntakeError, intake_triage.TriageError) as exc:
        raise HandoffStateError(
            f"stored triage evidence is present but not usable "
            f"({type(exc).__name__})"
        ) from exc

    packet_id = packet["packet_id"] if packet is not None else None
    packet_print = packet["content_fingerprint"] if packet is not None else None
    disposition = packet["disposition"]["value"] if packet is not None else None

    effects = classify_effects(record["body"])
    read_scoped = bool(_READ_SCOPE.search(record["body"][:MAX_BODY]))
    holds = _eligibility_holds(effects, read_scoped)
    if packet is None:
        holds.append(
            {
                "code": "no_source_attested_triage",
                "category": None,
                "reason": (
                    "no B3 advisory packet exists for this submission, so no "
                    "source-attested eligibility can be established"
                ),
            }
        )
    verdict = "HOLD" if holds else "ELIGIBLE_READ"
    document: dict[str, Any] = {
        "schema": ELIGIBILITY_SCHEMA,
        "submission_id": sid,
        "item_id": record["item_id"],
        "verdict": verdict,
        "read_scoped": read_scoped,
        "effects": effects,
        "holds": holds,
        "advisory": True,
        "deterministic": True,
        "authority": "none",
        "derived_at": utc_now(clock),
        "evidence": {
            "submission_fingerprint": record["fingerprint"],
            "submission_state": record["state"],
            "packet_id": packet_id,
            "packet_fingerprint": packet_print,
            "disposition": disposition,
        },
        "uncertainties": [_NOTE_NOT_AUTHORITY, _NOTE_READ_ONLY_PLAN],
        "fingerprint": "",
    }
    document["fingerprint"] = eligibility_fingerprint(document)
    return document


# ---------------------------------------------------------------------------
# Read-only source and Registry evidence (existing CAO state; never new)
# ---------------------------------------------------------------------------


def _source_evidence(
    repo: str | None, source_ref: str | None
) -> dict[str, Any]:
    """Bounded read-only Git evidence for the handoff source, or UNKNOWN.

    Delegated to B3's validated ``_git_evidence`` choke point: the same
    allowlisted, sanitized, no-shell probe. A missing or unusable repository is
    ``UNAVAILABLE``/``UNKNOWN``, never an empty success.
    """

    return intake_triage._git_evidence(repo, source_ref, None, None)


def _load_config(
    env: Mapping[str, str] | None,
) -> product_config.ProductConfig | None:
    """Read the product configuration, failing closed on a malformed document."""

    try:
        return product_config.load_config(product_config.config_path(env))
    except product_config.ProductConfigError as exc:
        raise HandoffStateError(
            f"the product configuration is malformed ({type(exc).__name__})"
        ) from exc


def _qualification(
    config: product_config.ProductConfig | None,
) -> dict[str, Any]:
    """A candidate route qualification, explicitly UNKNOWN when unconfigured.

    Role, model and provider privacy qualification are configuration, never
    authority. This build configures no provider privacy classification, so the
    candidate is reported as unqualified rather than assumed suitable.
    """

    model: str | None = None
    if config is not None and config.provider is not None:
        provider = config.provider
        if provider.provider_id and provider.model:
            candidate = f"{provider.provider_id}/{provider.model}"
            if _MODEL_RE.fullmatch(candidate):
                model = candidate
    return {
        "role": "reader",
        "model": model,
        "privacy": {
            "qualified": False,
            "reason": (
                "no provider privacy qualification is configured; CAO performs "
                "no automated provider-privacy classification and the operator "
                "must verify the provider's terms"
            ),
        },
        "verdict": "UNKNOWN" if model is None else "CANDIDATE",
        "reason": (
            "no provider/model is configured for the product"
            if model is None else None
        ),
    }


def _registry_claim_state(
    run_id: str, env: Mapping[str, str] | None
) -> dict[str, Any]:
    """Read-only Registry claims for one run id, or an explicit verdict.

    The Registry is opened through ``connect_readonly`` (SQLite ``mode=ro``), so
    a write is impossible by construction and an absent database is reported
    rather than created.
    """

    base: dict[str, Any] = {"source": "registry", "run_id": run_id, "claims": []}
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
    rows: list[dict[str, Any]] = []
    for claim in claims[:64]:
        if not isinstance(claim, Mapping) or claim.get("run_id") != run_id:
            continue
        rows.append(
            {
                "work_key": str(claim.get("work_key") or "")[:64],
                "mode": str(claim.get("mode") or "")[:16],
                "owner": str(claim.get("owner") or "")[:64],
            }
        )
    return {**base, "verdict": "SUPPORTED", "reason": None, "claims": rows}


# ---------------------------------------------------------------------------
# Validated linking of already-started independent READ audits
# ---------------------------------------------------------------------------


def _manifest_cites(spec: Mapping[str, Any], submission_id: str) -> bool:
    """Whether the immutable manifest itself cites the submission id.

    Only the manifest's own governed fields are scanned (initiative, task
    prompts, role instructions); a caller-supplied field is never consulted, so
    provenance cannot be self-asserted.
    """

    texts: list[str] = [str(spec.get("initiative") or "")]
    for task in spec.get("tasks") or []:
        if isinstance(task, Mapping):
            texts.append(str(task.get("prompt") or ""))
    roles = spec.get("roles")
    if isinstance(roles, Mapping):
        for role in roles.values():
            if isinstance(role, Mapping):
                texts.append(str(role.get("instruction") or ""))
    return any(submission_id in text for text in texts)


def _validate_linked_team(
    team_id: str, submission_id: str, env: Mapping[str, str] | None
) -> dict[str, Any]:
    """Validate one nominated team as a governed READ audit of a submission.

    The nomination is a *link request*, never authority. It is accepted only when
    the team id resolves to an authoritative checkpoint, the immutable manifest
    is entirely READ, and the manifest cites the submission. The team's status
    comes from that checkpoint, never from any caller-supplied field.
    """

    if not isinstance(team_id, str) or not _TEAM_ID_RE.fullmatch(team_id):
        raise HandoffInvalid("linked team id must be a bounded lowercase identifier")
    try:
        record = team._required(team_id, env)
    except Exception as exc:  # noqa: BLE001 - an unreadable team is never adopted
        raise HandoffUnavailable(
            f"the nominated team is not a readable CAO team ({type(exc).__name__})"
        ) from exc
    spec = record.get("spec")
    if not isinstance(spec, Mapping):
        raise HandoffStateError("the nominated team has no immutable manifest")
    tasks = spec.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise HandoffStateError("the nominated team has no tasks")
    for task in tasks:
        if not isinstance(task, Mapping) or task.get("mode") != "read":
            raise HandoffRefused(
                "the nominated team is not a fully READ team; it is not an "
                "independent read audit and is never linked as one"
            )
        if task.get("write_scope"):
            raise HandoffRefused("the nominated READ team carries a write scope")
    if not _manifest_cites(spec, submission_id):
        raise HandoffRefused(
            "the nominated team's immutable manifest does not cite this "
            "submission and is never linked as its audit"
        )
    try:
        summary = team.status(team_id, env=env)
    except Exception as exc:  # noqa: BLE001 - an unreadable team is not a link
        raise HandoffStateError(
            f"the nominated team's status could not be read "
            f"({type(exc).__name__})"
        ) from exc
    return {
        "team_id": team_id,
        "initiative": spec.get("initiative"),
        "spec_hash": record.get("spec_hash"),
        "status": summary.get("status"),
        "desired": summary.get("desired"),
        "tasks": sorted(str(task.get("id")) for task in tasks),
        "repos": sorted({str(task.get("repo")) for task in tasks}),
        "validated": True,
        "reason": None,
    }


def _existing_read_audit(
    submission_id: str, env: Mapping[str, str] | None
) -> dict[str, Any]:
    """Find the already-started, validated READ audit(s) for a submission.

    Zero matches is ``None``. More than one match is an explicit refusal: two
    competing audits are never collapsed into one link.
    """

    matches: list[dict[str, Any]] = []
    try:
        summaries = team.list_teams(env=env)
    except Exception:  # noqa: BLE001 - unreadable team state is not a link
        return {"verdict": "UNKNOWN", "link": None, "matches": [],
                "reason": "local team state could not be read"}
    for view in summaries[:512]:
        if not isinstance(view, Mapping) or view.get("status") == "CORRUPT":
            continue
        team_id = view.get("team_id")
        if not isinstance(team_id, str) or not _TEAM_ID_RE.fullmatch(team_id):
            continue
        try:
            link = _validate_linked_team(team_id, submission_id, env)
        except HandoffError:
            continue
        matches.append(link)
    matches.sort(key=lambda item: item["team_id"])
    if not matches:
        return {"verdict": "UNAVAILABLE", "link": None, "matches": [],
                "reason": "no validated READ audit is linked to this submission"}
    if len(matches) > 1:
        return {"verdict": "REFUSED", "link": None, "matches": matches,
                "reason": "more than one validated READ audit claims this "
                          "submission; the link is ambiguous and is refused"}
    return {"verdict": "SUPPORTED", "link": matches[0], "matches": matches,
            "reason": None}


def linked_state(
    submission_id: str, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """The read-only linked team/claim state for one submission.

    Provenance of an external audit is linked only after the validated team
    id/owner/source checks above, and its Registry claim state is read from the
    authoritative ledger, never from a caller-supplied ``completed`` field.
    """

    sid = _record_id(submission_id, "submission id")
    audit = _existing_read_audit(sid, env)
    link = audit.get("link")
    state: dict[str, Any] = {
        "source": "handoff",
        "verdict": audit["verdict"],
        "reason": audit.get("reason"),
        "team_id": link["team_id"] if link else None,
        "status": link["status"] if link else None,
        "desired": link["desired"] if link else None,
        "spec_hash": link["spec_hash"] if link else None,
        "audit": dict(link) if link else None,
        "claims": None,
    }
    if link is not None:
        state["claims"] = _registry_claim_state(
            "cao-team-" + link["team_id"], env
        )
    return state


def recover_plan(
    submission_id: str, *, env: Mapping[str, str] | None = None
) -> dict[str, Any] | None:
    """The latest persisted handoff plan for a submission, or ``None``.

    Read-only and bounded; a malformed stored plan raises rather than being
    skipped.
    """

    sid = _record_id(submission_id, "submission id")
    candidates: list[tuple[str, str]] = []
    directory = plan_dir(env)
    dir_fd = intake.open_private_dir_readonly(directory)
    if dir_fd is None:
        return None
    try:
        for name in intake.list_private_names(dir_fd):
            if name.startswith(".") or not name.endswith(".json"):
                continue
            plan_id = name[: -len(".json")]
            if not _ID_RE.fullmatch(plan_id):
                raise HandoffStateError("handoff directory holds an unexpected file")
            data = intake.read_private_record(dir_fd, name)
            if data is None:
                continue
            plan = _verify_plan(data, plan_id)
            if plan["submission_id"] != sid:
                continue
            candidates.append((plan["created_at"], plan_id))
            if len(candidates) >= MAX_SCAN:
                break
    finally:
        os.close(dir_fd)
    if not candidates:
        return None
    candidates.sort()
    return read_plan(candidates[-1][1], env)


# ---------------------------------------------------------------------------
# Handoff plan construction, persistence and the governed authorize route
# ---------------------------------------------------------------------------


def plan_dir(env: Mapping[str, str] | None = None) -> str:
    """The private handoff-plan directory under the XDG CAO state root."""

    return os.path.join(product_state.state_dir(env), "intake-handoffs")


def plan_path(plan_id: str, env: Mapping[str, str] | None = None) -> str:
    return os.path.join(plan_dir(env), _record_id(plan_id, "plan id") + ".json")


def _derived_ids(submission_id: str) -> dict[str, str]:
    """Immutable team/task/initiative ids derived from the submission id.

    Derivation is deterministic, so an identical request always maps to the same
    governed identities across processes and restarts; nothing here is supplied
    by a caller and nothing is random.
    """

    return {
        "team_id": "intake-read-" + submission_id[:16],
        "task_id": "read-" + submission_id[:12],
        "initiative": "intake-" + submission_id[:12],
    }


def _binding(
    eligibility: Mapping[str, Any],
    source: Mapping[str, Any],
    qualification: Mapping[str, Any],
    ids: Mapping[str, str],
) -> dict[str, Any]:
    """The immutable identity a handoff plan is derived from.

    It binds the submission id, the B3 packet fingerprint, the source/HEAD, the
    role/model/privacy qualification and the derived immutable team/task ids.
    The Registry owner is granted *by* the operator authority and is therefore
    recorded on the plan's grant, not inside this pre-grant binding.
    """

    return {
        "submission_id": eligibility["submission_id"],
        "submission_fingerprint": eligibility["evidence"]["submission_fingerprint"],
        "packet_id": eligibility["evidence"]["packet_id"],
        "packet_fingerprint": eligibility["evidence"]["packet_fingerprint"],
        "packet_disposition": eligibility["evidence"]["disposition"],
        "eligibility_fingerprint": eligibility["fingerprint"],
        "source": {
            "repo": source.get("repo"),
            "ref": source.get("ref"),
            "head": source.get("head"),
            "commit_at": source.get("commit_at"),
            "verdict": source.get("verdict"),
        },
        "qualification": {
            "role": qualification.get("role"),
            "model": qualification.get("model"),
            "privacy_qualified": qualification.get("privacy", {}).get("qualified"),
            "verdict": qualification.get("verdict"),
        },
        "team_id": ids["team_id"],
        "task_id": ids["task_id"],
        "initiative": ids["initiative"],
        "read_only": True,
    }


def plan_id_for(binding: Mapping[str, Any]) -> str:
    """The deterministic plan id: a one-way digest of the immutable binding."""

    if not isinstance(binding, Mapping):
        raise HandoffInvalid("plan binding must be a mapping")
    return hashlib.sha256(canonical_json(binding).encode("utf-8")).hexdigest()


def _binding_digest(plan: Mapping[str, Any]) -> str:
    """The stable pre-execution digest an operator grant binds to.

    It is computed only from immutable, pre-grant identity, so it is identical
    before and after the grant is applied and can never be replayed onto a
    different request or scope.
    """

    return fingerprint(
        {"plan_id": plan["plan_id"], "binding": plan["binding"]}
    )


def _plan_digest(plan: Mapping[str, Any]) -> str:
    """The stored record-integrity digest of a plan, recomputed on every read."""

    return fingerprint(
        {
            "plan_id": plan["plan_id"],
            "binding": plan["binding"],
            "grant": plan.get("grant"),
            "status": plan["status"],
            "registry_owner": plan.get("registry_owner"),
            "checkpoint": plan.get("checkpoint"),
        }
    )


def _missing(capability: str, reason: str, required: str) -> dict[str, Any]:
    return {
        "capability": capability,
        "status": "MISSING",
        "reason": reason,
        "required_integration": required,
    }


def _authority_verdict(
    authority: Any,
    plan: Mapping[str, Any],
    submission_id: str,
    env: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Evaluate the in-process authenticated operator authority, or report absent.

    This is the **only** place a handoff may become dispatchable. The authority
    is an in-process adapter supplied by the host, never argv, a JSON file or the
    process environment: those are caller-controlled and are never proof. A
    record that is not positively authenticated, or a grant not bound to this
    exact plan digest, is a refusal and never a partial dispatch.
    """

    base: dict[str, Any] = {"present": authority is not None, "authenticated": False}
    if authority is None:
        return {**base, "verdict": "UNAVAILABLE", "reason": _NO_OPERATOR_AUTHORITY}
    try:
        record = authority(submission_id=submission_id, plan=plan, env=env)
    except Exception as exc:  # noqa: BLE001 - an unreadable authority is UNKNOWN
        return {**base, "verdict": "UNKNOWN",
                "reason": f"the operator authority could not be read "
                          f"({type(exc).__name__})"}
    if record is None:
        return {**base, "verdict": "UNKNOWN",
                "reason": "the operator authority reported no grant"}
    if not isinstance(record, Mapping) or record.get("authenticated") is not True:
        return {**base, "verdict": "REFUSED",
                "reason": "the operator authority did not authenticate the operator"}
    operator = record.get("operator")
    try:
        operator = _ident(operator, "authority.operator")
    except HandoffInvalid as exc:
        return {**base, "verdict": "REFUSED", "reason": str(exc)}
    grant = record.get("grant")
    if not isinstance(grant, Mapping) or set(grant) != _AUTHORITY_GRANT_KEYS:
        return {**base, "verdict": "REFUSED",
                "reason": "the operator grant has missing or unsupported field(s)"}
    try:
        bound = _digest(grant["plan_digest"], "authority.grant.plan_digest")
    except HandoffInvalid as exc:
        return {**base, "verdict": "REFUSED", "reason": str(exc)}
    if bound != _binding_digest(plan):
        return {**base, "verdict": "REFUSED", "operator": operator,
                "reason": "the operator grant is bound to a different plan and is "
                          "stale"}
    if grant["read_only"] is not True:
        return {**base, "verdict": "REFUSED", "operator": operator,
                "reason": "the operator grant is not scoped to a READ execution"}
    if grant["privacy_qualified"] is not True:
        return {**base, "verdict": "REFUSED", "operator": operator,
                "reason": "the operator grant does not qualify the provider for "
                          "the data"}
    try:
        owner = _ident(grant["owner"], "authority.grant.owner")
        role = _ident(grant["role"], "authority.grant.role")
        model = _bounded_str(grant["model"], "authority.grant.model", 200,
                             allow_empty=False)
        if (not _MODEL_RE.fullmatch(model) or ".." in model or "//" in model):
            raise HandoffInvalid("authority.grant.model must be a provider/model id")
        scope = grant["scope"]
    except HandoffInvalid as exc:
        return {**base, "verdict": "REFUSED", "operator": operator,
                "reason": str(exc)}
    if not isinstance(scope, Mapping) or set(scope) != {"repo", "paths"}:
        return {**base, "verdict": "REFUSED", "operator": operator,
                "reason": "the operator grant scope is malformed"}
    repo = scope["repo"]
    if not isinstance(repo, str) or not os.path.isabs(repo) or os.path.normpath(repo) != repo:
        return {**base, "verdict": "REFUSED", "operator": operator,
                "reason": "the operator grant repository must be a normalized "
                          "absolute checkout path"}
    plan_repo = plan["binding"]["source"]["repo"]
    if plan_repo is not None and repo != plan_repo:
        return {**base, "verdict": "REFUSED", "operator": operator,
                "reason": "the operator grant repository is not the plan's source"}
    paths = scope["paths"]
    if not isinstance(paths, list) or len(paths) > MAX_SCOPE_PATHS:
        return {**base, "verdict": "REFUSED", "operator": operator,
                "reason": "the operator grant paths must be a bounded list"}
    for entry in paths:
        if (not isinstance(entry, str) or not entry
                or os.path.isabs(entry) or ".." in entry.split("/")):
            return {**base, "verdict": "REFUSED", "operator": operator,
                    "reason": "the operator grant names an out-of-scope path"}
    try:
        policy = product_config.team_model_policy(_load_config(env))
    except (product_config.ProductConfigError, HandoffError) as exc:
        return {**base, "verdict": "REFUSED", "operator": operator,
                "reason": f"the model allowlist could not be read ({exc})"}
    if policy is not None and not policy.permits(model):
        return {**base, "verdict": "REFUSED", "operator": operator,
                "reason": "the operator grant model is not on the configured "
                          "model allowlist"}
    return {
        **base,
        "verdict": "SUPPORTED",
        "authenticated": True,
        "operator": operator,
        "grant": {
            "owner": owner,
            "role": role,
            "model": model,
            "repo": repo,
            "paths": list(paths),
            "privacy_qualified": True,
        },
        "reason": "an in-process authenticated operator authority granted the "
                  "exact scoped READ execution",
    }


def _manifest(
    plan: Mapping[str, Any], grant: Mapping[str, Any]
) -> dict[str, Any]:
    """The immutable ``cao.team.v1`` manifest for the granted READ execution."""

    binding = plan["binding"]
    submission_id = binding["submission_id"]
    prompt = (
        f"Governed CAO Intake READ audit for submission {submission_id} bound to "
        f"handoff plan {plan['plan_id']} (digest {_binding_digest(plan)}). Inspect "
        "the scoped source and report evidence only. Do not merge, deploy, "
        "publish, release, change credentials or permissions, or delete "
        "anything. Return a bounded, truthful evidence report."
    )
    return {
        "schema": "cao.team.v1",
        "team_id": binding["team_id"],
        "initiative": binding["initiative"],
        "roles": {
            grant["role"]: {
                "instruction": (
                    "Read-only governed intake audit. Inspect and report; never "
                    "start, merge, deploy, publish, release, claim or change "
                    "credentials or permissions."
                ),
                "model": grant["model"],
            }
        },
        "tasks": [
            {
                "id": binding["task_id"],
                "role": grant["role"],
                "repo": grant["repo"],
                "prompt": prompt,
                "mode": "read",
            }
        ],
        "max_writers": 0,
    }


def _dispatch(
    plan: dict[str, Any],
    grant: Mapping[str, Any],
    env: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Hand one fully-proven READ manifest to the existing CAO team owner API.

    Only reachable behind the authenticated operator authority. It validates the
    manifest, initializes the team and sets it running; it never ticks, never
    schedules and never writes a Registry row. The existing daemon reconciles
    from there.
    """

    manifest = _manifest(plan, grant)
    selected = team.validate_spec(manifest)
    config = _load_config(env)
    from . import team_approval  # local import keeps the module's top level lean

    try:
        mode = team_approval.policy(config)["mode"]
    except team_approval.ApprovalError as exc:
        raise HandoffRefused(str(exc)) from exc
    try:
        team.initialize(selected, env=env, config=config,
                        approved=(mode == "chat"))
    except team.TeamError as exc:
        raise HandoffRefused(
            f"the existing CAO team owner API refused the handoff: {exc}"
        ) from exc
    plan["registry_owner"] = grant["owner"]
    try:
        team.set_desired(selected["team_id"], "running", env=env)
    except team.TeamError as exc:
        raise HandoffRefused(
            "the team was initialized paused but could not be started; the "
            f"existing daemon will not dispatch it: {exc}"
        ) from exc
    plan["checkpoint"] = {
        "team_id": selected["team_id"],
        "spec_hash": team.fingerprint(selected),
        "started": True,
    }
    return plan


def _verify_plan(
    data: Mapping[str, Any], expected_id: str
) -> dict[str, Any]:
    """Strictly re-verify one stored plan and re-derive its id and digest."""

    if not isinstance(data, Mapping):
        raise HandoffStateError("stored handoff plan is not an object")
    if data.get("schema") != PLAN_SCHEMA:
        raise HandoffStateError("stored handoff plan has an unexpected schema")
    plan_id = data.get("plan_id")
    if plan_id != expected_id or not isinstance(plan_id, str) or not _ID_RE.fullmatch(plan_id):
        raise HandoffStateError("stored handoff plan id is malformed")
    binding = data.get("binding")
    if not isinstance(binding, Mapping):
        raise HandoffStateError("stored handoff plan binding is malformed")
    if plan_id_for(binding) != plan_id:
        raise HandoffStateError("stored handoff plan does not match its own id")
    recorded = data.get("plan_digest")
    if not isinstance(recorded, str) or not _DIGEST_RE.fullmatch(recorded):
        raise HandoffStateError("stored handoff plan digest is malformed")
    if _plan_digest(data) != recorded:
        raise HandoffStateError("stored handoff plan digest does not match content")
    return dict(data)


def list_plans(env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Bounded summaries of every stored plan, oldest first, fail-closed."""

    directory = plan_dir(env)
    dir_fd = intake.open_private_dir_readonly(directory)
    if dir_fd is None:
        return []
    try:
        views: list[dict[str, Any]] = []
        for name in intake.list_private_names(dir_fd):
            if name.startswith(".") or not name.endswith(".json"):
                continue
            plan_id = name[: -len(".json")]
            if not _ID_RE.fullmatch(plan_id):
                raise HandoffStateError("handoff directory holds an unexpected file")
            data = intake.read_private_record(dir_fd, name)
            if data is None:
                continue
            plan = _verify_plan(data, plan_id)
            views.append(
                {
                    "plan_id": plan["plan_id"],
                    "submission_id": plan["submission_id"],
                    "status": plan["status"],
                    "created_at": plan["created_at"],
                }
            )
    finally:
        os.close(dir_fd)
    views.sort(key=lambda view: (view["created_at"], view["plan_id"]))
    return views


def read_plan(plan_id: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Read one full handoff plan, or fail closed."""

    pid = _record_id(plan_id, "plan id")
    dir_fd = intake.open_private_dir_readonly(plan_dir(env))
    if dir_fd is None:
        raise HandoffUnavailable("no handoff plan store exists")
    try:
        data = intake.read_private_record(dir_fd, pid + ".json")
    finally:
        os.close(dir_fd)
    if data is None:
        raise HandoffUnavailable("no handoff plan with that id")
    return _verify_plan(data, pid)


def _persist(
    plan: dict[str, Any], env: Mapping[str, str] | None
) -> dict[str, Any]:
    """Atomically persist one plan through the shared private-store toolkit."""

    plan_id = plan["plan_id"]
    plan["plan_digest"] = _plan_digest(plan)
    payload = canonical_json(plan)
    dir_fd = intake.open_private_dir(plan_dir(env))
    try:
        created = intake.write_private_record(dir_fd, plan_id + ".json", payload)
        data = intake.read_private_record(dir_fd, plan_id + ".json")
    finally:
        os.close(dir_fd)
    if created:
        if data is None:
            raise HandoffStateError("handoff plan failed its read-back check")
        stored = _verify_plan(data, plan_id)
        if stored["plan_digest"] != plan["plan_digest"]:
            raise HandoffStateError("handoff plan failed its read-back check")
        return {"status": "ADMITTED", "plan": stored}
    if data is None:
        raise HandoffUnavailable("an existing handoff plan disappeared before read")
    stored = _verify_plan(data, plan_id)
    if stored["plan_digest"] == plan["plan_digest"]:
        return {"status": "DUPLICATE", "plan": stored}
    raise HandoffConflict(
        "handoff plan id already exists with different content; refusing to merge"
    )


def handoff_plan(
    submission_id: str,
    *,
    env: Mapping[str, str] | None = None,
    repo: str | None = None,
    source_ref: str | None = None,
    operator_authority: Any = None,
    dry_run: bool = False,
    clock: Any = None,
) -> dict[str, Any]:
    """Derive the fail-closed, idempotent handoff plan for one submission.

    The plan is derived from the original admission and its B3 advisory, and
    binds the submission id, the B3 packet fingerprint, the source/HEAD, the
    role/model/privacy qualification, the immutable team/task ids and the
    Registry owner. It is executed only through the in-process authenticated
    ``operator_authority`` adapter; without one it is a truthful ``PLAN_ONLY``
    that names the exact missing operator integration and creates no team, agent
    or claim. ``dry_run`` creates no state at all.
    """

    sid = _record_id(submission_id, "submission id")
    if repo is not None:
        _bounded_str(repo, "repo", 4096, allow_empty=False)
    if source_ref is not None and not (
        git.valid_commit_id(source_ref) or git.valid_short_ref(source_ref)
    ):
        raise HandoffInvalid("source ref must be a commit id or a valid ref name")

    eligibility = derive_eligibility(sid, env=env, clock=clock)
    audit = _existing_read_audit(sid, env)
    source = _source_evidence(repo, source_ref)
    qualification = _qualification(_load_config(env))
    ids = _derived_ids(sid)
    binding = _binding(eligibility, source, qualification, ids)
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "plan_id": plan_id_for(binding),
        "submission_id": sid,
        "item_id": eligibility["item_id"],
        "status": "HOLD",
        "eligibility": {
            "verdict": eligibility["verdict"],
            "fingerprint": eligibility["fingerprint"],
            "holds": eligibility["holds"],
            "effects": eligibility["effects"],
        },
        "binding": binding,
        "checkpoint": {
            "team_id": ids["team_id"],
            "spec_hash": None,
            "started": False,
        },
        "link": audit.get("link"),
        "grant": None,
        "registry_owner": None,
        "authority": "none",
        "dispatch": False,
        "missing": [],
        "refusals": [],
        "advisory": True,
        "deterministic": True,
        "created_at": utc_now(clock),
        "plan_digest": "",
        "uncertainties": [_NOTE_NOT_AUTHORITY, _NOTE_READ_ONLY_PLAN],
    }

    if audit["verdict"] == "REFUSED":
        plan["status"] = "REFUSED"
        plan["refusals"].append({"code": "ambiguous_existing_audit",
                                 "reason": audit["reason"]})
        plan["plan_digest"] = _plan_digest(plan)
        return {"schema": RESULT_SCHEMA, "status": "REFUSED", "persisted": False,
                "duplicate": False, "plan": plan}
    if audit["verdict"] == "SUPPORTED" and audit["link"] is not None:
        # An already-started independent READ audit is preserved; no new work is
        # planned or launched, and nothing is duplicated.
        plan["status"] = "ALREADY_STARTED"
        plan["plan_digest"] = _plan_digest(plan)
        return {"schema": RESULT_SCHEMA, "status": "ALREADY_STARTED",
                "persisted": False, "duplicate": False, "plan": plan}

    if eligibility["verdict"] != "ELIGIBLE_READ":
        plan["status"] = "HOLD"
        plan["plan_digest"] = _plan_digest(plan)
        return {"schema": RESULT_SCHEMA, "status": "HOLD", "persisted": False,
                "duplicate": False, "plan": plan}

    authority = _authority_verdict(operator_authority, plan, sid, env)
    if authority["verdict"] != "SUPPORTED":
        if authority["verdict"] == "UNAVAILABLE":
            plan["status"] = "PLAN_ONLY"
            plan["missing"].append(
                _missing("authenticated_operator_authority",
                         authority["reason"], _OPERATOR_INTEGRATION)
            )
            if source.get("verdict") != "SUPPORTED":
                plan["missing"].append(
                    _missing("source_head_binding",
                             source.get("reason") or "source is unavailable",
                             "supply --repo for a readable checkout so the plan "
                             "binds a source ref and HEAD")
                )
            if qualification["verdict"] != "CANDIDATE":
                plan["missing"].append(
                    _missing("qualified_route",
                             qualification.get("reason") or "no route is configured",
                             "configure a provider/model and verify its privacy "
                             "terms before the operator grants the execution")
                )
        else:
            plan["status"] = "REFUSED"
            plan["refusals"].append(
                {"code": "operator_authority_" + authority["verdict"].lower(),
                 "reason": authority["reason"]}
            )
    else:
        plan["authority"] = "operator"
        plan["grant"] = {
            "operator": authority["operator"],
            "owner": authority["grant"]["owner"],
            "role": authority["grant"]["role"],
            "model": authority["grant"]["model"],
            "repo": authority["grant"]["repo"],
            "paths": authority["grant"]["paths"],
            "privacy_qualified": True,
            "bound": _binding_digest(plan),
        }
        plan["registry_owner"] = authority["grant"]["owner"]
        if dry_run:
            plan["status"] = "AUTHORIZED"
        else:
            try:
                plan = _dispatch(plan, authority["grant"], env)
            except HandoffRefused as exc:
                # The governed owner API refused the handoff. Report it
                # truthfully and record no authority; a partial team left
                # paused is never dispatched and is never persisted as granted.
                plan["status"] = "REFUSED"
                plan["authority"] = "none"
                plan["dispatch"] = False
                plan["grant"] = None
                plan["registry_owner"] = None
                plan["refusals"].append(
                    {"code": "dispatch_refused", "reason": str(exc)}
                )
            else:
                plan["status"] = "DISPATCH_PENDING"
                plan["dispatch"] = True
    plan["plan_digest"] = _plan_digest(plan)

    if dry_run or plan["status"] in ("HOLD", "REFUSED", "ALREADY_STARTED"):
        return {"schema": RESULT_SCHEMA, "status": plan["status"],
                "persisted": False, "duplicate": False, "plan": plan}
    result = _persist(plan, env)
    return {
        "schema": RESULT_SCHEMA,
        "status": result["status"],
        "persisted": True,
        "duplicate": result["status"] == "DUPLICATE",
        "plan": result["plan"],
    }


# ---------------------------------------------------------------------------
# Read-only status projection (persists nothing)
# ---------------------------------------------------------------------------


def _drift(plan: Mapping[str, Any] | None) -> dict[str, Any]:
    """Whether the plan's source HEAD has moved since it was bound."""

    if plan is None:
        return {"verdict": "UNAVAILABLE", "reason": "no persisted plan exists"}
    source = plan["binding"]["source"]
    repo, ref, head = source.get("repo"), source.get("ref"), source.get("head")
    if not repo or not head:
        return {"verdict": "UNAVAILABLE",
                "reason": "the plan bound no source head"}
    current = _source_evidence(repo, ref)
    if current.get("verdict") != "SUPPORTED":
        return {"verdict": "UNKNOWN",
                "reason": "the plan's source could not be re-read"}
    if current.get("head") != head:
        return {"verdict": "DRIFT", "bound_head": head,
                "current_head": current.get("head"),
                "reason": "the source HEAD moved after the plan was bound"}
    return {"verdict": "STABLE", "head": head, "reason": None}


def handoff_status(
    submission_id: str,
    *,
    env: Mapping[str, str] | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Derive one truthful handoff status (read-only; persists nothing).

    It reports the current eligibility, the latest persisted plan (if any), the
    validated linked team/claim state and any source drift, from authoritative
    existing evidence only.
    """

    sid = _record_id(submission_id, "submission id")
    eligibility = derive_eligibility(sid, env=env, clock=clock)
    plan = recover_plan(sid, env=env)
    linked = linked_state(sid, env)
    if linked["verdict"] == "SUPPORTED" and linked["team_id"] is not None:
        status = "ALREADY_STARTED"
    elif plan is not None:
        status = plan["status"]
    elif eligibility["verdict"] == "ELIGIBLE_READ":
        status = "PLAN_ONLY"
    else:
        status = "HOLD"
    if status not in HANDOFF_STATUSES:
        raise HandoffStateError("refusing to emit an unknown handoff status")
    return {
        "schema": STATUS_SCHEMA,
        "submission_id": sid,
        "item_id": eligibility["item_id"],
        "status": status,
        "eligibility": {
            "verdict": eligibility["verdict"],
            "fingerprint": eligibility["fingerprint"],
            "holds": eligibility["holds"],
        },
        "plan": (
            None if plan is None else
            {
                "plan_id": plan["plan_id"],
                "status": plan["status"],
                "plan_digest": plan["plan_digest"],
                "checkpoint": dict(plan["checkpoint"]),
                "missing": list(plan.get("missing") or []),
            }
        ),
        "linked": linked,
        "drift": _drift(plan),
        "authority": "none",
        "dispatch": False,
        "advisory": True,
        "deterministic": True,
        "egress": "none",
        "derived_at": utc_now(clock),
        "uncertainties": [_NOTE_NOT_AUTHORITY, _NOTE_READ_ONLY_PLAN],
    }
