"""Batch B2d: the native, model-backed READ execution binding (opt-in, default off).

This is the narrow **adapter** between the already-landed Batches and the
existing CAO execution path. It adds no executor, timer, scheduler, loop, claim,
queue or manager of its own: it verifies the native READ intent and the
protected B8 standing grant through :mod:`cao.decision_intake`, asks the B2b
stage for the bounded pseudonymous alias prompt, hands exactly one alias-citing
READ manifest to the **existing** CAO team owner API through
:func:`cao.intake_handoff.dispatch_read_plan`, and lets the existing daemon
reconcile the team. The post-prompt terminal receipt, the released READ Registry
claim and the tagged result are then validated by the existing B2b/B2c native
``decision_intake.validate_result``.

Load-bearing rules
------------------

* **Default off.** Every entry point takes ``enabled = False`` and returns an
  explicit ``HELD`` before any evidence is read: no team, task, plan, claim or
  prompt is created while the stage is disabled.
* **Trust comes from the protected grant only.** The B8 standing grant is
  re-read, scope-checked, source-checked, model-checked and privacy-qualified by
  :mod:`cao.decision_intake`; an absent, refused, revoked, expired or
  wrong-scope grant holds. Nothing here infers trust from provider
  configuration, a caller flag, manifest text or a model assertion.
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

from typing import Any

from . import decision_agent as da, decision_intake as di, intake_handoff

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


def _grant(ctx: Any) -> dict:
    """The verified B6-shaped grant of the protected B8 standing envelope.

    The envelope has already been re-read, scope/source/model-checked and
    privacy-qualified by :mod:`cao.decision_intake`, so this is a translation of
    verified facts, never a caller-asserted authority.
    """

    envelope = ctx["grant"]
    paths = (envelope.get("scope") or {}).get("paths")
    return {
        "operator": str(envelope.get("operator") or ""),
        "owner": str(ctx["owner"]),
        "role": str(envelope.get("role") or ""),
        "model": str(envelope.get("model") or ""),
        "repo": str(ctx["source"]["repo"]),
        "paths": list(paths or []),
    }


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
    unchanged persisted binding, no prior audit). Then the B2b alias prompt is
    built and one alias-citing READ manifest is handed to the existing CAO team
    owner API. ``dry_run`` returns the authorized plan only and creates nothing;
    the default ``enabled = False`` returns ``HELD`` before any evidence is read.
    The result is advisory data: it never carries authority.
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
        handoff = intake_handoff.dispatch_read_plan(
            ctx["sid"], grant=_grant(ctx), citation=ctx["alias"], env=env,
            repo=repo, source_ref=source_ref, dry_run=dry_run, clock=clock,
        )
    except da.DecisionHeld as held:
        return _held(held.code, held.reason, detail=getattr(held, "detail", None))
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
        return _hold("dispatch_read_plan", handoff)
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
