"""Batch B2b: the governed, read-only B8-to-Decision-Agent native binding.

This is the isolated landing of **Small Batch B2b**. It adds exactly one
stdlib-only module with two bounded, read-only entry points -- both
``enabled = False`` by default -- and it writes nothing, starts nothing and
grants nothing. Its whole job is to bind **native CAO identity and evidence**
to the already-landed Batch B2a decision stage (:mod:`cao.decision_stage`)
without weakening any authority or privacy invariant of either side:

* :func:`prepare_read_prompt` verifies one **native SHA256 B8 Intake
  submission** -- the admission is read back through Intake's strict reader and
  re-bound to its own item id, so an id is never a caller-asserted string --
  re-derives the deterministic B6 eligibility and source/HEAD binding through
  the existing read-only handoff projection, requires the **protected standing
  grant** *and* an **independently supplied privacy/route proof**, and only
  then asks B2a for one bounded advisory READ prompt under a **pseudonymous
  ``synthetic-`` alias** derived from the native identity, the granted owner,
  the scope, the exact HEAD and the approved plan.
* :func:`validate_result` verifies the **native result proof** first -- the
  exact derived team, its fully READ admission-bound manifest, the exact task
  slot at the bound HEAD, the task's own terminal receipt in the authoritative
  terminal output, and the task's **released** Work Registry claim -- and only
  then hands that terminal output to ``decision_stage.validate_result`` with an
  independently supplied evidence context.

Load-bearing rules
------------------

* **Native identity is verified, never asserted.** The submission id must be
  64 lowercase hex, must resolve through ``intake.read_submission`` (which
  re-derives ``sha256(item_id)`` and the stored content fingerprint and refuses
  any defect), and must be in state ``ADMITTED``. A bare string, a fake
  ``authenticated: true`` dictionary or a caller-supplied receipt is never
  proof. The title, the body and every credential are never read into anything
  this module returns or renders.
* **Standing authorization comes from the protected grant only.** The grant is
  re-read through the B8 read-only projection (root-owned, descriptor-checked,
  never caller-parameterised here); an absent, refused, revoked, expired,
  out-of-scope or wrong-source/wrong-route grant holds with an explicit code,
  and the B2a ``standing_authorization`` predicate re-reads it again instead of
  rubber-stamping the first read.
* **Privacy/route is independently supplied, never model-asserted.** The
  caller supplies the B2a qualification evidence; it is normalised and required
  to be fully qualified (provider, allowlist, data class, zero retention) and
  to name exactly the model the operator granted.
* **The prompt carries facts, never content.** Only the enumerated B2a fields
  reach the prompt (location, digest, head, cutoff, scope owner/repository/data
  class, verdict, route, data class, the synthetic alias and the exact task
  id). The real submission id and the private request text are never in it.
* **A synthetic id is a one-way alias.** ``synthetic-`` + a 160-bit digest over
  the native id, the granted owner, the scope, the exact HEAD and the approved
  plan: deterministic and collision-resistant, invertible only by re-deriving
  the same proven native binding (:func:`pseudonymous_alias` never maps back).
* **Native result proof is evidence, not text.** The authoritative team
  checkpoint, its admission, the fully READ manifest, the exact task slot, the
  task-bound terminal receipt (present exactly once, terminal, and never
  echoed by its own prompt) and the released, correctly owned READ claim are
  all verified from existing CAO state; a missing, active, foreign or unknown
  claim, an incomplete or restarted task, a forged team, a drifted HEAD or an
  ambiguous receipt holds with an explicit code. Nothing here dispatches, and
  the queue/daemon release path is untouched.
* **Default off.** Both entry points take ``enabled = False`` and return an
  explicit ``HELD`` before touching any evidence.

THE NATIVE RECEIPT BRIDGE (closed in Batch B2c)
-----------------------------------------------
B2a originally accepted the externally verified terminal receipt only as a
**single bounded token**, while the authoritative native CAO team terminal
receipt for the exact task is a **two-token line** (the fixed
``CAO_TEAM_RESULT:`` prefix, one ASCII space, 16 lowercase hex). Batch B2c
closes that gap in :mod:`cao.decision_stage`, which now admits exactly that
native two-token grammar *alongside* the original legacy single token. The
verified native receipt is still derived independently from the exact bound
execution id and the original task prompt and still has to be the unique
terminal line of the authoritative output; nothing here invents, appends or
rewrites a receipt. A stable ``receipt_unverifiable`` therefore now means only
that the externally supplied receipt is not the exact verified native or
legacy token; it never names a missing bridge. The receipt remains **source
evidence, never authority** -- a well-formed receipt never grants, dispatches
or widens a plan, and no tagged plan is accepted without its own verified
receipt proof.

Separation and scope
--------------------
Standard library only. The module performs no egress, opens no shell, runs no
scheduler, creates no team, task, claim, grant or queue, writes no record and
touches no canonical state; every native read goes through the existing B6/B7/B8
read-only APIs (and the Registry through ``connect_readonly``).
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from typing import Any

from . import (decision_agent as da, decision_stage as dst,
               intake, intake_authority, intake_handoff, intake_queue,
               product_config, product_task, registry, team)

__all__ = [
    "DECISION_SCHEMA",
    "ALIAS_PREFIX",
    "ALIAS_HEX",
    "MISSING_RECEIPT_INTERFACE",
    "pseudonymous_alias",
    "prepare_read_prompt",
    "validate_result",
]

#: The read-only B8-to-decision projection envelope. It is advisory data.
DECISION_SCHEMA = "cao.decision.intake.v1"
#: The explicit marker prefix B2a requires on every synthetic submission id.
ALIAS_PREFIX = dst.SYNTHETIC_PREFIX
#: Hex characters of the alias digest: 160 bits, collision-resistant, one-way.
ALIAS_HEX = 40
#: Bounded reason text; anything longer is truncated rather than propagated.
MAX_REASON = 500

#: Retained for public API stability. Batch B2c closed the receipt interface that
#: this constant used to name: :mod:`cao.decision_stage` now admits the exact
#: native CAO team two-token receipt (``CAO_TEAM_RESULT:``, one ASCII space, 16
#: lowercase hex) alongside the original legacy single token. No envelope reports
#: a missing native bridge any more; a stable ``receipt_unverifiable`` now means
#: only that the externally supplied receipt is not the exact verified native or
#: legacy token, and no tagged plan is accepted without its own verified receipt
#: proof.
MISSING_RECEIPT_INTERFACE = (
    "decision_stage.validate_result now admits the exact native CAO team "
    "two-token terminal receipt (CAO_TEAM_RESULT:, one ASCII space, 16 lowercase "
    "hex) alongside the original legacy single-token grammar, so the receipt "
    "grammar bridge is no longer missing. A stable receipt_unverifiable now "
    "means only that the externally supplied receipt is not the exact verified "
    "native or legacy token; no interface is reported missing, no receipt is "
    "invented, appended or rewritten, and no tagged plan is accepted without its "
    "own verified receipt proof."
)

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class _Hold(da.DecisionHeld):
    """A fail-closed hold with an optional bounded evidence detail."""

    def __init__(self, code: str, reason: str, detail: Any = None) -> None:
        super().__init__(code, reason)
        self.detail = detail


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def _envelope(status: str, code: str | None, reason: Any, **extra: Any) -> dict:
    result: dict[str, Any] = {
        "schema": DECISION_SCHEMA,
        "status": status,
        "code": code,
        "reason": None if reason is None else str(reason)[:MAX_REASON],
        "detail": None,
        "submission_id": None,
        "alias": None,
        "plan_id": None,
        "task_id": None,
        "team_id": None,
        "route": None,
        "data_class": None,
        "source": None,
        "binding": None,
        "prompt": None,
        "stage": None,
        "missing_interface": None,
        "authority": "none",
        "dispatch": False,
        "effects": [],
        "egress": "none",
        "advisory_only": True,
    }
    result.update(extra)
    return result


def _held(code: str, reason: str, **extra: Any) -> dict:
    return _envelope("HELD", code, reason, **extra)


def _disabled() -> dict:
    return _held(
        "model_stage_disabled",
        "the decision intake projection is disabled by default; no evidence is "
        "read, no prompt is built and the deterministic path stands",
    )


def _hold_envelope(held: da.DecisionHeld) -> dict:
    return _held(held.code, held.reason, detail=getattr(held, "detail", None))


# ---------------------------------------------------------------------------
# The pseudonymous alias (pure, bounded, one-way)
# ---------------------------------------------------------------------------


def pseudonymous_alias(
    *,
    submission_id: Any,
    owner: Any,
    repository: Any,
    head: Any,
    plan_id: Any,
) -> str:
    """The bounded ``synthetic-`` alias of one verified native binding.

    The alias is a one-way digest over the exact native submission id, the
    granted owner, the scope repository identity, the exact source HEAD and the
    approved plan id, so it changes whenever any of those change and cannot be
    inverted without re-deriving that same proven binding. Unverified or
    out-of-vocabulary material never produces an alias.
    """

    for value, pattern, field in (
        (submission_id, _HEX64, "submission_id"),
        (owner, da._TOKEN_RE, "owner"),
        (repository, da._REPO_RE, "repository"),
        (head, da._HEAD_RE, "head"),
        (plan_id, _HEX64, "plan_id"),
    ):
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise da.DecisionHeld(
                "field_invalid",
                f"alias.{field}: not a bounded, independently verified native "
                "identity; no synthetic alias is derived from unverified material",
            )
    material = "\0".join(
        ("cao.decision.alias.v1", submission_id, owner, repository, head, plan_id)
    )
    return ALIAS_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()[:ALIAS_HEX]


# ---------------------------------------------------------------------------
# Native B8 identity and evidence (every gate fails closed)
# ---------------------------------------------------------------------------


def _submission(submission_id: Any, env: Any) -> dict:
    """One admitted native submission, re-bound to its own item id.

    The strict Intake reader already re-derives ``sha256(item_id)`` and the
    stored content fingerprint from the stored bytes; this re-asserts the id
    binding and the admitted state, and extracts only the non-private fields.
    The title and the body are never copied out of the record.
    """

    if not isinstance(submission_id, str) or not _HEX64.fullmatch(submission_id):
        raise _Hold(
            "submission_id_invalid",
            "submission_id: an exact native 64-hex B8 Intake submission id is "
            "required; a caller-asserted bare string is never trusted",
        )
    try:
        record = intake.read_submission(submission_id, env)
    except intake.IntakeUnavailable as exc:
        raise _Hold(
            "submission_unavailable",
            "no admitted B8 submission carries that exact id",
        ) from exc
    except intake.IntakeError as exc:
        raise _Hold(
            "submission_unusable",
            f"the admitted submission is present but not usable "
            f"({type(exc).__name__})",
        ) from exc
    item_id = record["item_id"]
    if intake.submission_id_for(item_id) != submission_id:
        raise _Hold(
            "submission_mismatch",
            "the stored submission id does not re-derive from its own item id",
        )
    if record["state"] != intake.INTAKE_STATE_ADMITTED:
        raise _Hold(
            "submission_not_admitted",
            "only an admitted submission can reach the decision stage",
        )
    return {
        "submission_id": record["submission_id"],
        "item_id": item_id,
        "fingerprint": record["fingerprint"],
        "admitted_at": record["admitted_at"],
    }


def _eligible(sid: str, env: Any, clock: Any) -> dict:
    """The deterministic B6 eligibility/intent result, or a precise hold."""

    try:
        eligibility = intake_handoff.derive_eligibility(sid, env=env, clock=clock)
    except intake_handoff.HandoffUnavailable as exc:
        raise _Hold("eligibility_unavailable", str(exc)) from exc
    except intake_handoff.HandoffError as exc:
        raise _Hold(
            "eligibility_unusable",
            f"the deterministic eligibility evidence is present but not usable "
            f"({type(exc).__name__})",
        ) from exc
    if eligibility.get("verdict") != "ELIGIBLE_READ":
        holds = list(eligibility.get("holds") or [])
        first = holds[0] if holds else {
            "code": "eligibility_not_read",
            "reason": "no eligibility evidence could be derived",
        }
        raise _Hold(
            str(first["code"]),
            str(first["reason"]),
            detail={"holds": holds, "effects": eligibility.get("effects")},
        )
    return eligibility


def _source(repo: Any, source_ref: Any) -> dict:
    """The exact bound source/HEAD, or an explicit unavailable/unknown hold."""

    if repo is not None and (
        not isinstance(repo, str) or not repo or "\x00" in repo
    ):
        raise _Hold(
            "source_unavailable",
            "repo: an explicit, readable checkout path is required to bind an "
            "exact source ref and HEAD",
        )
    if source_ref is not None and not isinstance(source_ref, str):
        raise _Hold("source_unavailable", "source_ref: expected a string or None")
    evidence = intake_handoff._source_evidence(repo, source_ref)
    verdict = evidence.get("verdict")
    if verdict == "UNAVAILABLE":
        raise _Hold(
            "source_unavailable",
            str(evidence.get("reason") or "no repository path was supplied"),
        )
    if verdict != "SUPPORTED":
        raise _Hold(
            "source_unknown",
            str(evidence.get("reason") or "the bound source could not be read"),
        )
    if not evidence.get("head"):
        raise _Hold("source_head_binding", "the request binds no exact source HEAD")
    if evidence.get("stale") is True:
        raise _Hold(
            "source_stale",
            "the resolved source commit predates the evidence cutoff and is "
            "never current grounding",
        )
    return evidence


def _route(route_qualification: Any) -> tuple[str, str, dict]:
    """The independently supplied privacy/route proof, fully qualified."""

    if not isinstance(route_qualification, dict):
        raise _Hold(
            "field_invalid",
            "route_qualification: an independently supplied qualification "
            "object is required; a model never asserts its own privacy proof",
        )
    try:
        qualification = da._normalize_qualification(route_qualification)
        route, data_class = da._require_qualified(qualification)
    except da.DecisionHeld as exc:
        raise _Hold(exc.code, exc.reason, detail={"gate": "route_qualification"}) from exc
    return route, data_class, route_qualification


def _grant(sid: str, source: Mapping[str, Any], route: str, env: Any,
           clock: Any) -> dict:
    """The protected standing grant envelope, or an explicit hold.

    The grant is read through the B8 read-only projection with its production
    defaults; this module accepts no caller-controlled trust anchor, so a
    caller can never point it at a file it owns.
    """

    try:
        record = intake_authority.status(env=env, clock=clock)
    except Exception as exc:  # noqa: BLE001 - an unreadable grant is never authority
        raise _Hold(
            "standing_grant_unreadable",
            f"the protected standing grant could not be read ({type(exc).__name__})",
        ) from exc
    verdict = record.get("verdict")
    if verdict != "SUPPORTED":
        code = ("standing_grant_unavailable" if verdict == "UNAVAILABLE"
                else "standing_grant_refused")
        raise _Hold(
            code,
            str(record.get("reason") or "no protected standing grant holds"),
            detail={"authority_code": record.get("code"),
                    "required_integration": record.get("required_integration")},
        )
    envelope = record.get("envelope")
    if not isinstance(envelope, Mapping):
        raise _Hold(
            "standing_grant_refused",
            "the protected standing grant carries no usable envelope",
        )
    if envelope.get("privacy_qualified") is not True:
        raise _Hold(
            "standing_grant_unqualified",
            "the standing grant does not qualify the provider for the data",
        )
    submissions = envelope.get("submissions")
    if submissions is not None and sid not in submissions:
        raise _Hold(
            "grant_submission_out_of_scope",
            "the submission is not enumerated by the protected standing grant",
        )
    if envelope.get("scope", {}).get("repo") != source.get("repo"):
        raise _Hold(
            "grant_source_mismatch",
            "the verified source checkout is not the granted repository; a "
            "wrong repository is never bound",
        )
    if envelope.get("source", {}).get("ref") != source.get("ref"):
        raise _Hold(
            "grant_source_ref_mismatch",
            "the verified source ref is not the ref the operator granted",
        )
    granted_model = str(envelope.get("model") or "")
    if granted_model.split("#", 1)[0] != route:
        raise _Hold(
            "route_not_granted",
            "the independently qualified route is not the model the operator "
            "granted; an unauthorized model never reaches a prompt",
        )
    return dict(envelope)


def _binding_for(sid: str, eligibility: Mapping[str, Any],
                 source: Mapping[str, Any], env: Any) -> tuple[str, dict]:
    """The immutable native binding and its deterministic plan id."""

    try:
        qualification = intake_handoff._qualification(
            intake_handoff._load_config(env)
        )
    except intake_handoff.HandoffError as exc:
        raise _Hold(
            "config_unusable",
            f"the product configuration is present but not usable "
            f"({type(exc).__name__})",
        ) from exc
    ids = intake_handoff._derived_ids(sid)
    binding = intake_handoff._binding(eligibility, source, qualification, ids)
    return intake_handoff.plan_id_for(binding), binding


def _repository(owner: str, repo: str) -> str:
    """The bounded scope identity ``granted-owner/checkout-name``.

    It is a label, never a path: the absolute checkout path never reaches a
    prompt, and an unnameable checkout fails closed instead of degrading to a
    vague scope.
    """

    base = os.path.basename(os.path.normpath(repo))
    candidate = f"{owner}/{base}"
    if not isinstance(owner, str) or not da._REPO_RE.fullmatch(candidate):
        raise _Hold(
            "scope_unbounded",
            "the granted owner and checkout name do not form one bounded "
            "scope identity, so no prompt carries a vague scope",
        )
    return candidate


def _persisted(sid: str, plan_id: str, env: Any) -> dict:
    """Existing persisted B6/B7 state for this submission, fail-closed."""

    try:
        plan = intake_handoff.recover_plan(sid, env=env)
        intents = [
            intent
            for intent in intake_queue.list_intents(env)
            if isinstance(intent.get("binding"), Mapping)
            and intent["binding"].get("submission_id") == sid
        ]
        audit = intake_handoff.linked_state(sid, env)
    except (intake_handoff.HandoffError, intake_queue.QueueError) as exc:
        raise _Hold(
            "persisted_state_unusable",
            f"the persisted handoff, queue or team evidence for this "
            f"submission is present but not usable ({type(exc).__name__})",
        ) from exc
    stored: list[tuple[str, Any]] = []
    if plan is not None:
        stored.append(("handoff plan", plan.get("plan_id")))
    stored.extend(("queue intent", intent.get("intent_id")) for intent in intents)
    for kind, identity in stored:
        if identity != plan_id:
            raise _Hold(
                "binding_drift",
                f"the persisted {kind} for this submission binds a different "
                "admission, source, eligibility or qualification than the "
                "current evidence, so nothing is re-prompted on drifted facts",
            )
    verdict = audit.get("verdict")
    if verdict == "REFUSED":
        raise _Hold(
            "audit_ambiguous",
            str(audit.get("reason") or "competing READ audits claim this submission"),
        )
    if verdict == "UNKNOWN":
        raise _Hold(
            "team_state_unreadable",
            str(audit.get("reason") or "local team state could not be read"),
        )
    return {"plan": plan, "intents": intents, "audit": audit}


def _ref(*, location: str, digest: str, cutoff: str, head: str, owner: str,
         repository: str, data_class: str) -> dict:
    """One strict B2a source reference built only from verified native facts."""

    try:
        return da._normalize_ref({
            "location": location,
            "digest": digest,
            "cutoff": cutoff,
            "head": head,
            "scope": {
                "owner": owner,
                "repository": repository,
                "data_class": data_class,
            },
            "verdict": da.POSITIVE,
        })
    except da.DecisionHeld as exc:
        raise _Hold(
            exc.code, exc.reason, detail={"gate": "source_ref"}
        ) from exc


def _resolve(submission_id: Any, *, env: Any, repo: Any, source_ref: Any,
             route_qualification: Any, clock: Any) -> dict:
    """Every verified native fact both entry points share, or one precise hold."""

    admission = _submission(submission_id, env)
    sid = admission["submission_id"]
    eligibility = _eligible(sid, env, clock)
    source = _source(repo, source_ref)
    route, data_class, qualification = _route(route_qualification)
    grant = _grant(sid, source, route, env, clock)
    plan_id, binding = _binding_for(sid, eligibility, source, env)
    persisted = _persisted(sid, plan_id, env)
    owner = str(grant["owner"])
    repository = _repository(owner, str(source["repo"]))
    alias = pseudonymous_alias(
        submission_id=sid,
        owner=owner,
        repository=repository,
        head=str(source["head"]),
        plan_id=plan_id,
    )
    ref = _ref(
        location=f"urn:cao:source:{repository}@{source['head']}",
        digest=intake_handoff._binding_digest(
            {"plan_id": plan_id, "binding": binding}
        ),
        cutoff=admission["admitted_at"],
        head=str(source["head"]),
        owner=owner,
        repository=repository,
        data_class=data_class,
    )
    return {
        "sid": sid,
        "admission": admission,
        "eligibility": eligibility,
        "source": source,
        "route": route,
        "data_class": data_class,
        "qualification": qualification,
        "grant": grant,
        "binding": binding,
        "plan_id": plan_id,
        "persisted": persisted,
        "owner": owner,
        "repository": repository,
        "alias": alias,
        "ref": ref,
        "task_id": binding["task_id"],
        "team_id": binding["team_id"],
        "source_view": {
            "repo": source["repo"],
            "ref": source["ref"],
            "head": source["head"],
            "commit_at": source["commit_at"],
            "digest": ref["digest"],
        },
    }


def _standing(ctx: Mapping[str, Any], env: Any, clock: Any):
    """An independently re-read standing-authorization predicate for B2a.

    It does not capture the first read: it re-reads the protected grant and
    accepts only the same, still-live, still-covering envelope, so a grant that
    disappears, rotates, is revoked or expires before the prompt is built holds
    the prompt instead of rubber-stamping it.
    """

    fingerprint = ctx["grant"].get("grant_fingerprint")
    sid = ctx["sid"]

    def check() -> bool:
        try:
            record = intake_authority.status(env=env, clock=clock)
        except Exception:  # noqa: BLE001 - an unreadable grant is never authority
            return False
        envelope = record.get("envelope")
        if record.get("verdict") != "SUPPORTED" or not isinstance(envelope, Mapping):
            return False
        if envelope.get("grant_fingerprint") != fingerprint:
            return False
        if envelope.get("revoked") or envelope.get("privacy_qualified") is not True:
            return False
        submissions = envelope.get("submissions")
        if submissions is not None and sid not in submissions:
            return False
        now = str(record.get("now") or "")
        return str(envelope.get("granted_at") or "") <= now <= str(
            envelope.get("expires_at") or ""
        )

    return check


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def prepare_read_prompt(
    submission_id: Any,
    *,
    env: Any = None,
    repo: Any = None,
    source_ref: Any = None,
    route_qualification: Any = None,
    clock: Any = None,
    enabled: bool = False,
) -> dict:
    """One bounded advisory READ prompt for a verified native submission.

    Every gate above must hold first: the native admission, the deterministic
    eligible-READ intent, the exact source/HEAD, the independently supplied
    privacy/route proof, the protected standing grant (scope, ref and model),
    the unchanged persisted binding and the absence of any prior audit or
    completion. Only then is the B2a prompt requested, under the pseudonymous
    ``synthetic-`` alias and with a standing-authorization predicate that
    re-reads the protected grant. The default ``enabled = False`` yields
    ``HELD`` before any evidence is read. The result is advisory data: it never
    carries authority and never dispatches.
    """

    if not enabled:
        return _disabled()
    try:
        ctx = _resolve(
            submission_id, env=env, repo=repo, source_ref=source_ref,
            route_qualification=route_qualification, clock=clock,
        )
        _reject_prior_work(ctx)
        stage = dst.prepare_read_prompt(
            source_refs=[ctx["ref"]],
            route_qualification=ctx["qualification"],
            submission_id=ctx["alias"],
            submission_ids=[ctx["alias"]],
            task_id=ctx["task_id"],
            standing_authorization=_standing(ctx, env, clock),
            enabled=True,
        )
    except da.DecisionHeld as held:
        return _hold_envelope(held)
    except Exception as exc:  # noqa: BLE001 - an unreadable fact never rounds up
        return _held(
            "evidence_unreadable",
            f"the read-only decision evidence could not be established "
            f"({type(exc).__name__})",
        )
    return _envelope(
        stage["status"],
        stage["code"],
        stage["reason"],
        detail={
            "gate": "stage",
            "plan_id": ctx["plan_id"],
            "grant_fingerprint": ctx["grant"].get("grant_fingerprint"),
            "eligibility_fingerprint": ctx["eligibility"]["fingerprint"],
        },
        submission_id=ctx["sid"],
        alias=ctx["alias"],
        plan_id=ctx["plan_id"],
        task_id=ctx["task_id"],
        team_id=ctx["team_id"],
        route=stage.get("route"),
        data_class=stage.get("data_class"),
        source=ctx["source_view"],
        binding=ctx["binding"],
        prompt=stage.get("prompt"),
        stage=stage,
    )


def _reject_prior_work(ctx: Mapping[str, Any]) -> None:
    """No duplicate or already-completed work ever produces a new prompt."""

    persisted = ctx["persisted"]
    for intent in persisted["intents"]:
        if intent.get("status") == "COMPLETE":
            raise _Hold(
                "already_complete",
                "an independently queued READ intent for this exact binding is "
                "already complete; a duplicate prompt is never built",
            )
    audit = persisted["audit"]
    if audit.get("verdict") == "SUPPORTED":
        raise _Hold(
            "audit_already_started",
            "a validated independent READ audit already exists for this "
            "submission, so no second prompt is built for it",
            detail={"team_id": audit.get("team_id")},
        )


def _claim_evidence(run_id: str, execution_id: str, head: str,
                    env: Any) -> dict:
    """The released READ claim of exactly this task, or an explicit verdict.

    Read through ``connect_readonly`` (SQLite ``mode=ro``), so a write is
    impossible by construction. ``MISSING`` (no claim at all), ``ACTIVE`` (the
    work is still held), ``FOREIGN`` (the released claim is not this task's
    owned READ claim), ``HEAD_MISMATCH`` (released against another revision)
    and ``UNKNOWN`` (an unreadable ledger) all fail closed.
    """

    base = {"run_id": run_id, "work_key": execution_id, "claims": 0}
    try:
        ledger = product_config.registry_db_path(env)
    except Exception:  # noqa: BLE001 - an unresolvable ledger is UNKNOWN
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local Work Registry location could not be resolved"}
    if not os.path.isfile(ledger):
        return {**base, "verdict": "MISSING",
                "reason": "no Work Registry ledger carries this task's claim"}
    try:
        conn = registry.connect_readonly(ledger)
    except Exception:  # noqa: BLE001 - an unreadable ledger is UNKNOWN
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local Work Registry could not be read"}
    try:
        rows = conn.execute(
            "SELECT run_id, work_key, owner, mode, released_at, outcome, "
            "released_head FROM claims WHERE run_id=? ORDER BY claim_id",
            (run_id,),
        ).fetchall()
    except Exception:  # noqa: BLE001 - an unreadable claim set is UNKNOWN
        return {**base, "verdict": "UNKNOWN",
                "reason": "the local Work Registry claims could not be read"}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass
    claims = [dict(row) for row in rows]
    base["claims"] = len(claims)
    if not claims:
        return {**base, "verdict": "MISSING",
                "reason": "this team's run holds no Work Registry claim at all"}
    if any(claim.get("released_at") is None for claim in claims):
        return {**base, "verdict": "ACTIVE",
                "reason": "this team's run still holds an open Work Registry "
                          "claim, so the task is not a proven terminal result"}
    owned = [
        claim for claim in claims
        if claim.get("work_key") == execution_id
        and claim.get("owner") == product_task.OWNER
        and claim.get("mode") == registry.READ
        and claim.get("outcome") == "complete"
    ]
    if not owned:
        return {**base, "verdict": "FOREIGN",
                "reason": "no released claim for this run belongs to this task, "
                          "its expected owner and READ mode; a foreign or "
                          "forged claim is never accepted as native proof"}
    if not any(claim.get("released_head") == head for claim in owned):
        return {**base, "verdict": "HEAD_MISMATCH",
                "reason": "the released READ claim was closed against another "
                          "source revision than the bound HEAD"}
    return {**base, "verdict": "SUPPORTED", "reason": None,
            "owner": product_task.OWNER, "outcome": "complete"}


def _native_result(ctx: Mapping[str, Any], env: Any) -> dict:
    """The authoritative native READ task result proof, or one precise hold."""

    binding = ctx["binding"]
    sid = ctx["sid"]
    team_id = ctx["team_id"]
    task_id = ctx["task_id"]
    head = binding["source"]["head"]

    audit = ctx["persisted"]["audit"]
    if (audit.get("verdict") == "SUPPORTED"
            and audit.get("team_id") != team_id):
        raise _Hold(
            "audit_ambiguous",
            "a different team already claims this submission, so no single "
            "native result can be bound to it",
            detail={"team_id": audit.get("team_id")},
        )

    try:
        record = team._read(team_id, env)
    except team.TeamError as exc:
        raise _Hold(
            "native_team_unusable",
            f"the bound team checkpoint is present but not usable "
            f"({type(exc).__name__})",
        ) from exc
    if record is None:
        raise _Hold(
            "native_team_missing",
            "the derived team id resolves to no authoritative CAO team "
            "checkpoint, so no native result exists",
        )
    hold = intake_queue._bound_read_team_hold(record, binding, sid,
                                              citation=ctx["alias"])
    if hold is not None:
        raise _Hold(str(hold["code"]), str(hold["reason"]),
                    detail={"gate": "team_manifest"})

    tasks = {task.get("id"): task for task in record["spec"]["tasks"]}
    task = tasks.get(task_id)
    slot = record["slots"].get(task_id)
    if task is None or not isinstance(slot, Mapping):
        raise _Hold(
            "native_task_missing",
            "the bound task id is not part of the team's immutable manifest",
        )
    if task.get("mode") != "read" or task.get("write_scope"):
        raise _Hold(
            "not_read_only",
            "the bound task is not a READ task; a WRITE or reserved execution "
            "is never read as an advisory result",
        )
    if slot.get("status") != "complete" or slot.get("error"):
        raise _Hold(
            "native_task_incomplete",
            f"the bound READ task reports {slot.get('status')!r} with error "
            f"{slot.get('error')!r}; only a clean terminal completion is proof",
            detail={"slot_status": slot.get("status")},
        )
    if slot.get("head") != head:
        raise _Hold(
            "source_head_drift",
            "the completed task accepted a revision that is not the exact "
            "bound source HEAD, so its result is not this binding's evidence",
            detail={"bound_head": head, "task_head": slot.get("head")},
        )

    execution_id = slot.get("execution_id")
    prompt = slot.get("prompt")
    output = slot.get("output")
    if not isinstance(execution_id, str) or not isinstance(prompt, str):
        raise _Hold(
            "native_receipt_unusable",
            "the completed task carries no execution identity or original "
            "prompt, so its terminal receipt cannot be derived",
        )
    receipt = team._receipt_for(execution_id, prompt)
    if receipt in prompt:
        raise _Hold(
            "receipt_echoed",
            "the task's terminal receipt also appears in its own prompt, so it "
            "is an echo and is never accepted as proof",
        )
    if not isinstance(output, str) or not output:
        raise _Hold(
            "native_receipt_missing",
            "the completed task carries no authoritative terminal output",
        )
    lines = output.splitlines()
    pattern = re.compile(r"[ \t]{0,4}" + re.escape(receipt) + r"\Z")
    matched = [index for index, line in enumerate(lines) if pattern.fullmatch(line)]
    if not matched:
        raise _Hold(
            "native_receipt_missing",
            "the authoritative terminal output carries no exact task-bound "
            "terminal receipt",
        )
    last = next(
        (index for index in range(len(lines) - 1, -1, -1) if lines[index].strip()),
        None,
    )
    if len(matched) > 1 or matched[0] != last:
        raise _Hold(
            "receipt_ambiguous",
            "the terminal receipt is duplicated or is not the terminal line of "
            "the output, so the result is ambiguous and is never interpreted",
            detail={"occurrences": len(matched)},
        )

    run_id = "cao-team-" + team_id
    claim = _claim_evidence(run_id, execution_id, head, env)
    if claim["verdict"] != "SUPPORTED":
        codes = {
            "MISSING": "registry_claim_missing",
            "ACTIVE": "registry_claim_active",
            "FOREIGN": "registry_claim_foreign",
            "HEAD_MISMATCH": "registry_claim_head_mismatch",
            "UNKNOWN": "registry_unknown",
        }
        raise _Hold(
            codes.get(str(claim["verdict"]), "registry_unknown"),
            str(claim.get("reason") or "the Work Registry claim is not proven"),
            detail={"claim": claim},
        )
    conflict = intake_queue._registry_conflict(binding, env=env, registry_db=None)
    if conflict.get("verdict") == "UNKNOWN":
        raise _Hold(
            "registry_unknown",
            str(conflict.get("reason") or "the Work Registry could not be read"),
        )
    if conflict.get("conflict"):
        raise _Hold(
            "registry_conflict",
            str(conflict.get("reason")
                or "an independent active claim holds this scope"),
            detail={"against_run": conflict.get("against_run")},
        )

    return {
        "output": output,
        "receipt": receipt,
        "execution_id": execution_id,
        "claim": claim,
        "summary": {
            "team_id": team_id,
            "task_id": task_id,
            "slot_status": slot.get("status"),
            "head": head,
            "manifest": "read_only",
            "admission": "bound",
            "receipt_verified": True,
            "claim": "released_read_claim",
            "run_id": run_id,
        },
    }


def _context(ctx: Mapping[str, Any], native: Mapping[str, Any]) -> dict:
    """The independently supplied B2a evidence context (never model-asserted)."""

    ref = ctx["ref"]
    claim = native.get("claim") or {}

    def source_known(candidate: Any) -> bool:
        try:
            return isinstance(candidate, Mapping) and dict(candidate) == dict(ref)
        except Exception:  # noqa: BLE001 - a failed checker never rounds up
            return False

    return {
        "schema": da.CONTEXT_SCHEMA,
        "source_refs": [ref],
        "source_known": source_known,
        "route_qualification": ctx["qualification"],
        "registry_preflight": {
            "verdict": da.POSITIVE,
            "code": None,
            "reason": (
                "the bound team's own READ claim is released in the existing "
                f"Work Registry (verdict {claim.get('verdict')}) and no "
                "independent claim holds this scope"
            ),
        },
        "completed_work": None,
        "prior_receipt": None,
        "accepted_plan": None,
        # This projection is a review of native READ work: only a READ plan is
        # ever proposed, and a WRITE or reserved effect stays held.
        "judgment": {"kind": "review", "read_only": True},
    }


def validate_result(
    submission_id: Any,
    *,
    env: Any = None,
    repo: Any = None,
    source_ref: Any = None,
    route_qualification: Any = None,
    clock: Any = None,
    enabled: bool = False,
) -> dict:
    """One advisory validation of the native READ task result, or an explicit hold.

    The native proof is verified first -- the exact derived team, its fully
    READ admission-bound manifest, the exact task slot at the bound HEAD, the
    task's own terminal receipt in the authoritative terminal output, and the
    task's released, correctly owned READ claim -- and only then is the
    authoritative terminal output handed to ``decision_stage.validate_result``
    together with the independently supplied evidence context. The output is
    never caller-supplied and never rewritten, so no receipt can be invented.
    The default ``enabled = False`` yields ``HELD`` before any evidence is
    read, and the result is always advisory data: never authority, never a
    dispatch.
    """

    if not enabled:
        return _disabled()
    try:
        ctx = _resolve(
            submission_id, env=env, repo=repo, source_ref=source_ref,
            route_qualification=route_qualification, clock=clock,
        )
        native = _native_result(ctx, env)
        stage = dst.validate_result(
            output=native["output"],
            receipt=native["receipt"],
            task_id=ctx["task_id"],
            submission_id=ctx["alias"],
            submission_ids=[ctx["alias"]],
            context=_context(ctx, native),
            enabled=True,
        )
    except da.DecisionHeld as held:
        return _hold_envelope(held)
    except Exception as exc:  # noqa: BLE001 - an unreadable fact never rounds up
        return _held(
            "evidence_unreadable",
            f"the read-only native result evidence could not be established "
            f"({type(exc).__name__})",
        )
    # Batch B2c admits the exact native two-token receipt in decision_stage, so a
    # stable receipt_unverifiable is no longer a missing bridge. The envelope key
    # is retained for schema stability and is always None.
    return _envelope(
        stage["status"],
        stage["code"],
        stage["reason"],
        detail=dict(native["summary"]),
        submission_id=ctx["sid"],
        alias=ctx["alias"],
        plan_id=ctx["plan_id"],
        task_id=ctx["task_id"],
        team_id=ctx["team_id"],
        route=stage.get("route"),
        data_class=stage.get("data_class"),
        source=ctx["source_view"],
        binding=ctx["binding"],
        stage=stage,
        missing_interface=None,
    )
