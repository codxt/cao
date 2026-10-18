"""Batch B2d: the native, model-backed READ execution binding (opt-in, default off).

This is the narrow **adapter** between the already-landed Batches and the
existing CAO execution path. It adds no executor, timer, scheduler, loop, claim,
queue or manager of its own: it verifies the native READ intent and the
protected B8 standing grant through :mod:`cao.decision_intake`, re-reads the
**same** protected standing authority through its in-process adapter, asks the
B2b stage for the bounded pseudonymous alias prompt, and hands exactly one
alias-citing READ manifest -- carrying that exact tagged decision plan prompt --
to the **existing** CAO team owner API through the **same** governed B6
:func:`cao.intake_handoff.handoff_plan` path (serialized under the existing B7
queue dispatch lock). The existing daemon reconciles the team. The post-prompt
terminal receipt, the released READ Registry claim and the tagged result are then
validated by the existing B2b/B2c native ``decision_intake.validate_result``.

Load-bearing rules
------------------

* **Default off.** Every entry point takes ``enabled = False`` and returns an
  explicit ``HELD`` before any evidence is read: no team, task, plan, claim or
  prompt is created while the stage is disabled.
* **Trust comes from the protected grant only.** The B8 standing grant is
  re-read, scope-checked, source-checked, model-checked and privacy-qualified by
  :mod:`cao.decision_intake`, and is then re-read a second time through the
  protected in-process adapter, whose plan-digest-bound verdict is the *only*
  thing that can dispatch. An absent, refused, revoked, expired or wrong-scope
  grant holds, and nothing here infers trust from provider configuration, a
  caller dictionary, a flag, a citation, manifest text or a model assertion.
* **One quota, shared.** A READ execution consumes the same durable B8 quota
  the B6/B7 route consumes, counted across alias-citing and id-citing teams
  alike; an exhausted, incomplete or unreadable quota evidence holds.
* **Privacy comes from qualified evidence, not a model.** The route/data-class
  qualification is independently supplied (zero retention, provider-qualified,
  allowlisted); a missing or non-zero-retention route holds.
* **No private data in the model-visible prompt.** The manifest the owner API
  receives cites the one-way ``synthetic-`` alias of the verified binding, never
  the raw admission id; the title, the body and every credential are never
  rendered. The alias's private binding (admission, source/HEAD, plan) is proved
  separately from existing CAO state.
* **One executor, reused.** No team is created by any module here directly; the
  existing ``intake_handoff`` manifest/dispatch/persist path initializes and
  starts it and the existing daemon runs it. A restart or an ambiguous/foreign
  audit is refused rather than re-prompted, and no shadow team, ledger or
  scheduler is created.
* **Advisory only.** Every envelope is advisory data: ``authority`` is always
  ``none``, it never dispatches by itself and it never widens a scope. A
  model-produced WRITE or reserved effect is held by the B2a validator.

Separation and scope
--------------------
Standard library only. The module performs no egress, opens no shell, runs no
scheduler and touches no canonical state; every native read and every dispatch
goes through the existing B2b/B6 read-only and governed APIs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import (decision_agent as da, decision_intake as di, intake_authority,
               intake_handoff, intake_queue)

__all__ = [
    "EXECUTION_SCHEMA",
    "STARTED",
    "AUTHORIZED",
    "start_read_team",
    "validate_read_result",
]

#: The advisory execution-binding envelope. It is data, never authority.
EXECUTION_SCHEMA = "cao.decision.execution.v1"
#: The team was handed to the existing owner API and is running under the daemon.
STARTED = "STARTED"
#: A dry-run produced the authorized plan only; nothing was created.
AUTHORIZED = "AUTHORIZED"
#: Bounded reason text; anything longer is truncated rather than propagated.
MAX_REASON = 500


def _envelope(status: str, code: str | None, reason: Any, **extra: Any) -> dict:
    result: dict[str, Any] = {
        "schema": EXECUTION_SCHEMA,
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
        "prompt": None,
        "plan": None,
        "handoff": None,
        "stage": None,
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
        "the model-backed READ execution binding is disabled by default; no "
        "evidence is read, no prompt is built and no team is created",
    )


def _hold(kind: str, handoff: dict) -> dict:
    """Map one non-dispatch handoff outcome to a precise advisory hold."""

    plan = handoff.get("plan") or {}
    status = str(handoff.get("status") or "UNKNOWN")
    codes = {
        "ALREADY_STARTED": "already_started",
        "HOLD": "not_eligible_read",
        "REFUSED": "dispatch_refused",
    }
    reason = {
        "ALREADY_STARTED": "an already-validated READ audit is bound to this "
                           "submission, so no second prompt is built",
        "HOLD": "the request no longer qualifies as an eligible scoped READ",
        "REFUSED": "the governed handoff refused the execution",
    }.get(status, f"the governed handoff returned {status}")
    return _held(
        codes.get(status, "dispatch_unverified"), reason,
        detail={"stage": kind, "handoff_status": status,
                "refusals": list(plan.get("refusals") or [])},
    )


def _dispatch(
    ctx: Mapping[str, Any], prompt: str, authority: Any, *,
    env: Any, repo: Any, source_ref: Any, clock: Any, dry_run: bool,
) -> dict:
    """Hand the one verified READ plan to the existing B6 owner path.

    The dispatch is the **same single governed ``handoff_plan`` path** the B6/B7
    route uses, behind the **same** in-process operator-authority adapter that
    re-reads and re-binds the protected B8 grant to the exact derived plan digest
    at dispatch time. An actual dispatch is serialized under the **existing** B7
    queue dispatch kernel lock, so a caller cannot race a second READ team or a
    second quota unit past a concurrent pass; a dry run creates nothing and takes
    no lock.
    """

    def governed() -> dict:
        return intake_handoff.handoff_plan(
            ctx["sid"], repo=repo, source_ref=source_ref, env=env, clock=clock,
            operator_authority=authority, citation=ctx["alias"], prompt=prompt,
            dry_run=dry_run,
        )

    if dry_run:
        return governed()
    with intake_queue._queue_lock(intake_queue.queue_dir(env)):
        return governed()


def start_read_team(
    submission_id: Any,
    *,
    env: Any = None,
    repo: Any = None,
    source_ref: Any = None,
    route_qualification: Any = None,
    clock: Any = None,
    dry_run: bool = False,
    enabled: bool = False,
) -> dict:
    """Open one native, model-backed READ execution for a verified submission.

    Every gate of :func:`cao.decision_intake.prepare_read_prompt` must hold
    first (native admission, eligible-READ intent, exact source/HEAD,
    independently qualified zero-retention route, protected B8 standing grant,
    unchanged persisted binding, no prior audit). The **same protected B8
    standing authority is then re-read through its in-process adapter** and must
    still fingerprint-match that evidence, before the exact B2b alias prompt is
    handed, with the exact plan, to the existing CAO team owner API. Authority
    comes only from that adapter's plan-digest-bound verdict -- never from a
    caller dictionary, an ``enabled`` flag, a citation, a prompt or a model plan.
    ``dry_run`` returns the authorized plan only and creates nothing; the default
    ``enabled = False`` returns ``HELD`` before any evidence is read. The result
    is advisory data: it never carries authority.
    """

    if not enabled:
        return _disabled()
    try:
        ctx = di._resolve(
            submission_id, env=env, repo=repo, source_ref=source_ref,
            route_qualification=route_qualification, clock=clock,
        )
        prompt = di.prepare_read_prompt(
            submission_id, env=env, repo=repo, source_ref=source_ref,
            route_qualification=route_qualification, clock=clock, enabled=True,
        )
        if prompt.get("status") != "PROPOSED":
            return _held(
                str(prompt.get("code") or "prompt_held"),
                str(prompt.get("reason") or "the alias prompt is held"),
                detail={"stage": "prepare_read_prompt"},
            )
        authority = intake_authority.standing_authority(env=env, clock=clock)
        if authority.get("verdict") != "SUPPORTED" or authority.get(
                "adapter") is None:
            return _held(
                "standing_authority_unavailable",
                str(authority.get("reason")
                    or "no protected standing authority resolved"),
                detail={"authority_code": authority.get("code")},
            )
        envelope = authority.get("envelope") or {}
        if envelope.get("grant_fingerprint") != ctx["grant"].get(
                "grant_fingerprint"):
            return _held(
                "standing_authority_drift",
                "the protected standing authority changed between the verified "
                "B2b evidence and the dispatch, so nothing is granted on drifted "
                "authority",
            )
        if str(envelope.get("model") or "").split("#", 1)[0] != ctx["route"]:
            return _held(
                "route_not_granted",
                "the independently qualified route is not the protected standing "
                "authority's model",
            )
        handoff = _dispatch(
            ctx, prompt["prompt"], authority["adapter"], env=env, repo=repo,
            source_ref=source_ref, clock=clock, dry_run=dry_run,
        )
    except da.DecisionHeld as held:
        return _held(held.code, held.reason, detail=getattr(held, "detail", None))
    except intake_queue.QueueBusy:
        return _held(
            "dispatch_busy",
            "another governed dispatch holds the shared B7 queue dispatch lock; "
            "no second READ team is admitted",
        )
    except intake_queue.QueueError as exc:
        return _held(
            "queue_unavailable",
            f"the shared B7 queue dispatch lock is unavailable "
            f"({type(exc).__name__})",
        )
    except intake_handoff.HandoffError as exc:
        return _held(
            "handoff_refused",
            f"the governed handoff refused the READ execution "
            f"({type(exc).__name__})",
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable fact never rounds up
        return _held(
            "evidence_unreadable",
            f"the read-only execution evidence could not be established "
            f"({type(exc).__name__})",
        )

    plan = handoff.get("plan") or {}
    if plan.get("plan_id") != ctx["plan_id"]:
        return _held(
            "binding_drift",
            "the derived READ plan binds a different admission, source or "
            "qualification than the verified native evidence",
        )
    if handoff.get("status") not in ("AUTHORIZED", "ADMITTED", "DUPLICATE"):
        return _hold("handoff_plan", handoff)
    return _envelope(
        AUTHORIZED if dry_run else STARTED,
        None,
        None,
        detail={"plan_status": plan.get("status"),
                "persisted": bool(handoff.get("persisted")),
                "duplicate": bool(handoff.get("duplicate"))},
        submission_id=ctx["sid"],
        alias=ctx["alias"],
        plan_id=ctx["plan_id"],
        task_id=ctx["task_id"],
        team_id=ctx["team_id"],
        route=ctx["route"],
        data_class=ctx["data_class"],
        source=ctx["source_view"],
        prompt=prompt.get("prompt"),
        plan={"schema": plan.get("schema"), "plan_id": plan.get("plan_id"),
              "status": plan.get("status"),
              "binding": plan.get("binding")},
        handoff={"status": handoff.get("status"),
                 "persisted": handoff.get("persisted"),
                 "duplicate": handoff.get("duplicate")},
        dispatch=not dry_run,
    )


def validate_read_result(
    submission_id: Any,
    *,
    env: Any = None,
    repo: Any = None,
    source_ref: Any = None,
    route_qualification: Any = None,
    clock: Any = None,
    enabled: bool = False,
) -> dict:
    """Validate the native post-prompt READ result through B2b/B2c, or hold.

    The native proof -- the exact derived team, its fully READ admission-bound
    manifest, the exact task slot at the bound HEAD, the task's own terminal
    receipt and its released, correctly owned READ claim -- is verified first,
    and only then is the authoritative output handed to the existing
    ``decision_intake.validate_result``. The alias citation proves the manifest
    without the private admission id. The result is advisory ``PROPOSED`` or
    ``HELD``: never authority, never a dispatch.
    """

    if not enabled:
        return _disabled()
    result = di.validate_result(
        submission_id, env=env, repo=repo, source_ref=source_ref,
        route_qualification=route_qualification, clock=clock, enabled=True,
    )
    return _envelope(
        result.get("status"),
        result.get("code"),
        result.get("reason"),
        detail=result.get("detail"),
        submission_id=result.get("submission_id"),
        alias=result.get("alias"),
        plan_id=result.get("plan_id"),
        task_id=result.get("task_id"),
        team_id=result.get("team_id"),
        route=result.get("route"),
        data_class=result.get("data_class"),
        source=result.get("source"),
        stage=result,
    )
