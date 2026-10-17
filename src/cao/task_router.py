"""The one discoverable "send to CAO" request entrypoint: a deterministic router.

A caller sends exactly one bounded request document. CAO decides *one* finite
route and reports it truthfully, so the caller never has to choose between the
Intake API and the Team Execution API.

The decision vocabulary is deliberately small and closed:

``BACKLOG``
    The request is recorded for later. It is admitted through the existing
    Intake gate (one durable, idempotent record) and nothing else happens.
``INVESTIGATE``
    The request needs a human decision or an investigation before any grounded
    implementation position exists. It is still admitted durably, and it is held
    with an actionable next step rather than guessed at.
``IMPLEMENT``
    The request clearly expresses implementation intent, so it becomes a visible
    **execution candidate**. It is admitted durably *and* routed toward the
    existing governed Team Execution path. It is only ever *started* when a
    separately qualified, exact team manifest and actual existing operator
    authorization are both available.

An Intake admission is **never** execution consent. A work verb in the request
text is **never** operator approval. The two mechanical routes are disjoint:

* a plain request (or a ``cao.intake.request.v1`` document) is admitted through
  the existing Intake gate, which persists one durable record; and
* an already-shaped ``cao.team.v1`` manifest is routed to the existing CAO Team
  Execution contract, reported read-only with the exact governed next commands
  (and, only with the operator's explicit ``--execute`` and an *existing*
  qualification, advanced through the exact same owner API ``cao team init`` and
  ``cao team start`` already use).

The router itself never fabricates an approval, a manifest, a scope or an
authority. It raises no privilege, bypasses no Work Registry rule, dispatches no
worker, writes no Registry row, deletes nothing, auto-merges, pushes or deploys
nothing, and creates no second store. A document it cannot classify with
certainty is refused, never guessed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from . import (intake, intake_handoff, intake_triage, product_config, team,
               team_approval)

__all__ = [
    "main",
    "build_parser",
    "classify",
    "route",
    "SCHEMA",
    "ROUTE_INTAKE",
    "ROUTE_EXECUTION",
    "ROUTE_HOLD",
    "DECISION_BACKLOG",
    "DECISION_INVESTIGATE",
    "DECISION_IMPLEMENT",
    "DECISIONS",
    "STATUS_ADMITTED",
    "STATUS_PROPOSED",
    "STATUS_EXECUTING",
    "STATUS_ROUTED",
    "STATUS_REFUSED",
    "STATUS_DUPLICATE",
    "GATE_EXACT_MANIFEST",
    "GATE_OPERATOR_APPROVAL",
    "GATE_HUMAN_REVIEW",
    "GATE_CONSEQUENTIAL_EFFECT",
    "GATE_REPOSITORY",
    "EXIT_OK",
    "EXIT_USAGE",
    "EXIT_UNAVAILABLE",
]

SCHEMA = "cao.request.v1"

ROUTE_INTAKE = "INTAKE"
ROUTE_EXECUTION = "EXECUTION"
ROUTE_HOLD = "HOLD"

DECISION_BACKLOG = "BACKLOG"
DECISION_INVESTIGATE = "INVESTIGATE"
DECISION_IMPLEMENT = "IMPLEMENT"
DECISIONS = (DECISION_BACKLOG, DECISION_INVESTIGATE, DECISION_IMPLEMENT)

STATUS_ADMITTED = "ADMITTED"
#: Execution was requested but is not started; the request is held with a
#: concrete, actionable next step (the operator's approval, an exact manifest, a
#: human review, ...). ``PROPOSED`` is the finite status for every such hold.
STATUS_PROPOSED = "PROPOSED"
#: Execution was actually started or is already running, proved by team
#: evidence. Reported only when a team checkpoint backs it.
STATUS_EXECUTING = "EXECUTING"
STATUS_ROUTED = "ROUTED"
STATUS_REFUSED = "REFUSED"
STATUS_DUPLICATE = "DUPLICATE"

#: The finite set of gates that hold an execution candidate. A gate is a fact,
#: never an authority: the router reports it and names the smallest next step.
GATE_EXACT_MANIFEST = "exact_team_manifest"
GATE_OPERATOR_APPROVAL = "operator_approval"
GATE_HUMAN_REVIEW = "human_review"
GATE_CONSEQUENTIAL_EFFECT = "consequential_effect"
GATE_REPOSITORY = "repository_scope_absent"

#: The largest request document this entrypoint will read. A team manifest may
#: carry up to MAX_TASKS long prompts, so it is bounded above an Intake
#: submission but still finite; the bound is fixed, never derived from input.
MAX_REQUEST_BYTES = 1024 * 1024

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3

_ROUTED_OUTCOMES = frozenset({STATUS_ADMITTED, STATUS_DUPLICATE, STATUS_ROUTED,
                              STATUS_PROPOSED, STATUS_EXECUTING})

#: An explicit "for later" declaration. A request that asks to be deferred stays
#: backlog even when it also contains a work verb.
_LATER = re.compile(
    r"\b(later|backlog|someday|defer|deferred|postpone|postponed|not now|"
    r"not yet|no rush|for later|queue for|triage later|roadmap)\b",
    re.IGNORECASE,
)

#: The reserved categories that turn a request into a held consequential one.
_CONSEQUENTIAL_VERDICTS = ("REQUESTED", "AMBIGUOUS")

#: A leading prohibition is an imperative limitation, not a question opener: B3's
#: question predicate reads a leading "do" as interrogative, so "do not implement
#: X" would otherwise be held as a question instead of read as a negated
#: directive.
_DIRECTIVE_NEGATION = re.compile(
    r"^(?:do not|don't|never|no|not|without)\b", re.IGNORECASE)

#: Common positive action verbs the shared B3 work predicate predates. They are
#: matched here, task-router-locally, rather than by silently broadening
#: ``intake_triage._WORK_SHAPED`` for its other callers: an explicit "execute
#: now" or "continue the batch" is implementation intent, not a backlog note.
_WORK_EXTRA = re.compile(
    r"\b(execute|executes|executing|executed|run|runs|running|ran|continue|"
    r"continues|continuing|continued|resume|resumes|resuming|resumed|finish|"
    r"finishes|finishing|finished|complete|completes|completing|completed|"
    r"proceed|proceeds|proceeding|proceeded)\b",
    re.IGNORECASE,
)

#: Imperative review/investigation verbs. A request to look at, audit or
#: investigate something is a genuine hold for a human decision: neither a plain
#: backlog note nor, on its own, an implementation directive.
_REVIEW_SHAPED = re.compile(
    r"\b(review|reviews|reviewing|reviewed|audit|audits|auditing|audited|"
    r"inspect|inspects|inspecting|inspected|investigate|investigates|"
    r"investigating|investigated|analyze|analyzes|analyzing|analyzed|analyse|"
    r"analyses|analysing|analysed|assess|assesses|assessing|assessed|diagnose|"
    r"diagnoses|diagnosing|diagnosed|compare|compares|comparing|compared|"
    r"research|researches|researching|researched|check|checks|checking|"
    r"checked)\b",
    re.IGNORECASE,
)

#: A quoted span is a mention, never a directive: a caller quoting an example or
#: a policy string does not thereby authorize the quoted verb. Backticks and
#: straight double quotes are stripped before the local verb predicates run;
#: single quotes are left alone so contractions ("don't") keep their negation.
_QUOTED = re.compile(r"`[^`]*`|\"[^\"]*\"")


def _unquote(body: str) -> str:
    return _QUOTED.sub(" ", body)


def _emit(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False))


def _execution(*, requested: bool, started: bool, gate: str | None,
               needs: Sequence[str] = (), next_steps: Sequence[str] = ()
               ) -> dict[str, Any]:
    """The one execution block, used by every truthful outcome.

    ``requested`` and ``started`` are kept separate on purpose: a caller can see
    that execution was *asked for* while nothing has been *started*.
    """

    return {
        "requested": bool(requested),
        "started": bool(started),
        "gate": gate,
        "needs": list(needs),
        "next": list(next_steps),
    }


def _payload(status: str, decision: str | None, route: str | None,
             execution: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "status": status,
        "decision": decision,
        "route": route,
        "authority": "none",
        "dispatch": False,
        "execution": dict(execution),
    }
    payload.update(extra)
    return payload


def _refusal(code: str, message: str, *,
             status: str = STATUS_REFUSED) -> dict[str, Any]:
    payload = _payload(status, None, None,
                       _execution(requested=False, started=False, gate=None))
    payload["error"] = {"code": code, "verdict": status, "message": message}
    return payload


def classify(document: Any) -> str | None:
    """The single route for a request document, or ``None`` when unroutable.

    The decision depends only on the document's own declared shape, never on a
    caller flag, environment value or prior admission, so the same request is
    always routed the same way.
    """

    if not isinstance(document, Mapping):
        return None
    schema = document.get("schema")
    if schema == team.SPEC_SCHEMA:
        return ROUTE_EXECUTION
    if schema == intake.REQUEST_SCHEMA:
        return ROUTE_INTAKE
    if schema is None and "item_id" in document and "body" in document:
        # A plain ordinary request that did not name the Intake schema is still
        # unambiguously an admission: it carries the two required fields and no
        # manifest identity.
        return ROUTE_INTAKE
    return None


# ---------------------------------------------------------------------------
# The deterministic decision for opaque request text
# ---------------------------------------------------------------------------


def _unnegated(pattern: re.Pattern[str], body: str) -> bool:
    """Whether ``pattern`` matches outside a negating directive.

    Negation is scoped exactly as the existing B6 classifier scopes it -- a
    contrastive coordinator ("do not merge, but implement the fix") ends an
    earlier negation -- so an explicit request after a contrast is still seen as
    positive, while a plain negated directive is not.
    """

    for clause in intake_handoff._clauses(body):
        for match in pattern.finditer(clause):
            scope = intake_handoff._negation_scope_start(clause, match.start())
            scoped = clause[scope:match.start()]
            if not scoped.strip():
                return True
            if not (intake_handoff._NEG_START.search(scoped)
                    or intake_handoff._NEG_ANY.search(scoped)
                    or intake_handoff._NEG_LOCAL.search(scoped)):
                return True
    return False


def _positive_work(body: str) -> bool:
    """Whether a work verb appears outside a negating directive.

    ``intake_triage._work_shaped`` alone would read "do not implement X" as an
    implementation directive, and it predates several common action verbs
    ("execute", "run", "continue", "resume", "finish", "complete", "proceed").
    Negation is scoped exactly as the existing B6 classifier scopes it, and the
    local additions are consulted in the same task-router-local way so the shared
    advisory predicate is never silently broadened. A quoted span is a mention,
    not a directive.
    """

    prose = _unquote(body)
    return (_unnegated(intake_triage._WORK_SHAPED, prose)
            or _unnegated(_WORK_EXTRA, prose))


def _imperative_review(body: str) -> bool:
    """Whether an imperative review/investigation verb appears, un-negated.

    A request to review, audit or investigate something is a genuine hold for a
    human decision: it is neither a plain backlog note nor, on its own, an
    implementation directive. It is consulted only *after* explicit positive work
    has been ruled out, so a mixed "review then implement the fix" stays
    IMPLEMENT. A quoted span is a mention, not a directive.
    """

    return _unnegated(_REVIEW_SHAPED, _unquote(body))


def _question(body: str) -> bool:
    """Whether the text is a genuine question rather than an imperative.

    Reuses the existing B3 question predicate, but a leading prohibition
    ("do not ...", "never ...") is an imperative limitation, not an
    interrogative: B3 reads a leading "do" as a question opener, so it is
    excluded here rather than turning "do not implement X" into a hold.
    """

    return (intake_triage._question_shaped(body)
            and not _DIRECTIVE_NEGATION.match(body.strip()))


def _classify_text(body: str) -> tuple[str, dict[str, Any]]:
    """The finite decision for one plain-text request body.

    First match wins, on a documented deterministic rule table (no model, no
    hidden state). Text is opaque data: it is inspected, never executed. The
    shape predicates are reused from the existing B3 advisory triage so a request
    is classified the same way an operator's triage would read it.
    """

    try:
        verdicts = intake_handoff.classify_effects(body)
    except intake_handoff.HandoffError:
        # B6 could not classify the text. Fail closed: an unreadable classifier
        # may hide a consequential effect, so the request is held for a human
        # rather than silently recorded (or executed) as plain work.
        return DECISION_INVESTIGATE, {"gate": GATE_CONSEQUENTIAL_EFFECT,
                                      "effects_unknown": True}
    effects = {category: verdict for category, verdict in verdicts.items()
               if verdict in _CONSEQUENTIAL_VERDICTS}
    if effects:
        return DECISION_INVESTIGATE, {"gate": GATE_CONSEQUENTIAL_EFFECT,
                                      "effects": effects}
    if _LATER.search(body):
        return DECISION_BACKLOG, {"gate": None}
    risk = intake_triage._risk_flags(body)
    if risk:
        return DECISION_INVESTIGATE, {"gate": GATE_HUMAN_REVIEW,
                                      "risk_flags": list(risk)}
    # A question-shaped request is held for investigation even when it contains a
    # work verb: the intent is not a clear implementation directive, and guessing
    # "yes, implement it" from a question is exactly what this router must not do.
    if _question(body):
        return DECISION_INVESTIGATE, {"gate": GATE_HUMAN_REVIEW}
    # Only a work verb outside a negating directive is implementation intent: a
    # negative directive ("do not implement X") is recorded, never executed.
    if _positive_work(body):
        return DECISION_IMPLEMENT, {"gate": GATE_EXACT_MANIFEST}
    # An imperative review/investigation request ("review the changes",
    # "investigate the failure") is a hold for a human decision, not a plain
    # backlog note. It is checked after positive work so a mixed request that
    # names explicit work ("review then implement the fix") stays IMPLEMENT.
    if _imperative_review(body):
        return DECISION_INVESTIGATE, {"gate": GATE_HUMAN_REVIEW}
    return DECISION_BACKLOG, {"gate": None}


def _triage_command(submission_id: str) -> str:
    return f"cao intake triage --id {submission_id}"


def _route_text(document: Mapping[str, Any], *, env: Mapping[str, str] | None,
                dry_run: bool, clock: Any) -> dict[str, Any]:
    """Decide and durably admit one ordinary request.

    The durable Intake admission is the one persisted effect and always happens
    (idempotently); the decision then makes an implementation-intent request
    *visible* as an execution candidate instead of burying it in the backlog. The
    request text is never treated as approval and nothing is started here.
    """

    if document.get("schema") is None:
        document = {**document, "schema": intake.REQUEST_SCHEMA}
    # Normalize first (no store on a dry run) so the decision is made on exactly
    # the redacted, bounded body the durable record will carry.
    record = intake.normalize_submission(document, clock=clock)
    decision, detail = _classify_text(record["body"])
    if dry_run:
        admission = {
            "status": STATUS_ADMITTED,
            "duplicate": False,
            "submission": intake.public_view(record),
        }
    else:
        admission = intake.submit(document, env=env, clock=clock)
    submission_id = admission["submission"]["submission_id"]
    duplicate = admission["status"] == STATUS_DUPLICATE
    status = STATUS_DUPLICATE if duplicate else STATUS_ADMITTED

    if decision == DECISION_IMPLEMENT:
        execution = _execution(
            requested=True, started=False, gate=GATE_EXACT_MANIFEST,
            needs=("exact_team_manifest", "operator_approval"),
            next_steps=(_triage_command(submission_id),),
        )
        if not duplicate:
            status = STATUS_PROPOSED
        return _payload(status, decision, ROUTE_EXECUTION, execution,
                        dry_run=bool(dry_run), source="intake",
                        duplicate=duplicate,
                        submission=admission["submission"])
    if decision == DECISION_INVESTIGATE:
        gate = detail.get("gate") or GATE_HUMAN_REVIEW
        needs = ["human_review"]
        if gate == GATE_CONSEQUENTIAL_EFFECT:
            needs = ["human_review", "consequential_effect"]
        execution = _execution(
            requested=False, started=False, gate=gate, needs=needs,
            next_steps=(_triage_command(submission_id),),
        )
        if not duplicate:
            status = STATUS_PROPOSED
        return _payload(status, decision, ROUTE_HOLD, execution,
                        dry_run=bool(dry_run), source="intake",
                        duplicate=duplicate,
                        submission=admission["submission"])
    # BACKLOG: recorded for later; no execution was requested.
    execution = _execution(
        requested=False, started=False, gate=None,
        next_steps=(_triage_command(submission_id),),
    )
    return _payload(status, DECISION_BACKLOG, ROUTE_INTAKE, execution,
                    dry_run=bool(dry_run), source="intake",
                    duplicate=duplicate,
                    submission=admission["submission"])


# ---------------------------------------------------------------------------
# The governed Team Execution route
# ---------------------------------------------------------------------------


def _quote(reference: str | None) -> str | None:
    """A shell-safe rendering of a manifest path, or ``None`` when absent.

    An absent path yields no command rather than an unquoted placeholder that
    reads like a runnable command with a literal ``<manifest.json>`` argument.
    """

    if reference is None:
        return None
    return shlex.quote(os.fspath(reference))


def _governed_commands(source: str | None, mode: str,
                       team_id: str) -> list[str]:
    """The exact existing commands the accountable operator must run.

    Paths are shell-quoted so a checkout path with spaces or metacharacters
    cannot make the printed command ambiguous or unsafe. The commands are
    ``cao team plan``/``init``/``start`` verbatim -- the router adds no authority
    flag and never an ``--approved`` on the operator's behalf.
    """

    reference = _quote(source)
    if reference is None:
        return []
    commands = [f"cao team plan --file {reference}"]
    if mode == "chat":
        commands.append(f"cao team init --file {reference} --approved")
    else:
        commands.append(f"cao team init --file {reference}")
    commands.append(f"cao team start --id {team_id}")
    return commands


def _team_evidence(summary: Mapping[str, Any]) -> dict[str, Any]:
    """The minimal, public-safe team evidence that backs an EXECUTING status."""

    evidence: dict[str, Any] = {
        "team_id": summary.get("team_id"),
        "desired": summary.get("desired"),
        "status": summary.get("status"),
        "tasks": len(summary.get("tasks") or {}),
    }
    if summary.get("initiative") is not None:
        evidence["initiative"] = summary["initiative"]
    proof = summary.get("approval")
    if isinstance(proof, Mapping):
        evidence["approval"] = {"method": proof.get("method"),
                                "spec_sha256": proof.get("spec_sha256")}
    return evidence


def _team_running(summary: Mapping[str, Any]) -> bool:
    """Whether a dispatched worker slot is actually running.

    ``desired == "running"`` is intent, not execution, and an unstarted
    ``PENDING`` team is not an agent: only a task slot the daemon put into
    ``running`` proves an agent actually started. An early/transient idle -- a
    worker that reported idle before a settled result -- leaves its slot running,
    so a started agent is still reported as started.
    """

    tasks = summary.get("tasks")
    if not isinstance(tasks, Mapping):
        return False
    return any(isinstance(slot, Mapping) and slot.get("status") == "running"
               for slot in tasks.values())


def _waiting_next_steps(team_id: str, summary: Mapping[str, Any], *,
                        blocked: bool, commands: Sequence[str]) -> list[str]:
    """The smallest exact next step when a team exists but is not running yet.

    An unapproved chat team needs the operator's own approval commands; a paused
    team needs ``cao team start``; a team whose desired state is already running
    but that has dispatched no worker only needs the existing daemon's reconcile
    (a manual ``cao team tick``).
    """

    if blocked:
        return list(commands)
    if summary.get("desired") == "running":
        return [f"cao team tick --id {team_id}"]
    return [f"cao team start --id {team_id}"]


def _missing_repositories(spec: Mapping[str, Any]) -> list[str]:
    """The task repositories that are not readable directories right now.

    Only consulted when the operator explicitly asks to execute: an exact manifest
    that names an absent checkout is held, never started against a path that
    cannot exist.
    """

    missing: list[str] = []
    for task in spec["tasks"]:
        repo = task["repo"]
        if not os.path.isdir(repo) and repo not in missing:
            missing.append(repo)
    return missing


def _route_execution(spec: Any, *, env: Mapping[str, str] | None, config: Any,
                     source: str | None, execute: bool, dry_run: bool
                     ) -> dict[str, Any]:
    """Route one ``cao.team.v1`` manifest to the governed Team Execution path.

    Without ``--execute`` this is a read-only report that also reconciles an
    already-existing team (so a second identical request never looks like a new
    start). With ``--execute`` the request is only ever advanced through the
    existing owner API (``team.initialize``/``team.set_desired``) when the
    existing operator authorization is already present: chat mode needs a saved
    admission, and the text itself is never approval.
    """

    try:
        selected = team.validate_spec(spec)
    except team.TeamError as exc:
        return _refusal(getattr(exc, "code", None) or "TEAM_INVALID", str(exc))
    resolved = config if config is not None else product_config.load_config()
    if resolved is None:
        return _refusal(
            "PRODUCT_CONFIG_UNAVAILABLE",
            "no local CAO product configuration is installed, so the team's "
            "resolved routes cannot be independently qualified; run setup.sh "
            "and 'cao doctor' first",
            status="UNAVAILABLE",
        )
    try:
        bound = team._dispatchable_routes(resolved, selected)
    except (team.TeamError, product_config.ProductConfigError) as exc:
        return _refusal(getattr(exc, "code", None) or "TEAM_INVALID", str(exc))
    denied = sorted(
        f"{task_id}/{phase}"
        for task_id, phases in bound["allowed"].items()
        for phase, permitted in phases.items() if not permitted
    )
    if denied:
        return _refusal(
            "TEAM_MODEL_NOT_ALLOWED",
            "resolved routes are not independently authorized by the existing "
            "Team Execution policy: " + ", ".join(denied),
        )
    try:
        mode = team_approval.policy(resolved)["mode"]
    except team_approval.ApprovalError as exc:
        return _refusal(getattr(exc, "code", None) or "TEAM_INVALID", str(exc))

    team_id = selected["team_id"]
    commands = _governed_commands(source, mode, team_id)
    authorization = {
        "authority_source": "team_execution",
        "approval_mode": mode,
        "independent_authorization_required": mode == "chat",
    }
    try:
        existing = team._read(team_id, env)
    except team.TeamError as exc:
        return _refusal(getattr(exc, "code", None) or "TEAM_INVALID", str(exc))
    if existing is not None and existing["spec_hash"] != team.fingerprint(selected):
        return _refusal(
            "TEAM_IDENTITY_CONFLICT",
            "an existing team already uses this id with a different immutable "
            "manifest; the router never replaces or merges it",
        )

    if not execute:
        # Read-only report. Reconcile an already-existing team so a repeat
        # request reports what is true instead of proposing a duplicate start.
        if existing is None:
            execution = _execution(
                requested=True, started=False, gate=GATE_OPERATOR_APPROVAL
                if mode == "chat" else None,
                needs=("operator_approval",) if mode == "chat" else (),
                next_steps=commands,
            )
            return _payload(
                STATUS_ROUTED, DECISION_IMPLEMENT, ROUTE_EXECUTION, execution,
                dry_run=bool(dry_run), source="team_execution",
                team_id=team_id, initiative=selected.get("initiative"),
                spec_sha256=team.fingerprint(selected),
                authorization=authorization, next_commands=commands,
            )
        summary = team.status(team_id, env=env)
        started = _team_running(summary)
        blocked = mode == "chat" and existing.get("admission") is None
        next_steps = ([] if started
                      else _waiting_next_steps(team_id, summary,
                                               blocked=blocked,
                                               commands=commands))
        execution = _execution(
            requested=True, started=started,
            gate=GATE_OPERATOR_APPROVAL if blocked and not started else None,
            needs=("operator_approval",) if blocked and not started else (),
            next_steps=next_steps,
        )
        return _payload(
            STATUS_EXECUTING if started else STATUS_PROPOSED, DECISION_IMPLEMENT,
            ROUTE_EXECUTION, execution,
            dry_run=bool(dry_run), source="team_execution",
            team_id=team_id, initiative=selected.get("initiative"),
            spec_sha256=team.fingerprint(selected),
            authorization=authorization,
            next_commands=next_steps,
            team=_team_evidence(summary),
        )

    # --execute: only route into the owner path where authorization already
    # exists. In chat mode that is a saved admission; the router never records
    # one, so an unapproved or absent team is held with the exact next commands.
    approved_existing = existing is not None and existing.get("admission") is not None
    if mode == "chat" and not approved_existing:
        execution = _execution(
            requested=True, started=False, gate=GATE_OPERATOR_APPROVAL,
            needs=("operator_approval",), next_steps=commands,
        )
        return _payload(
            STATUS_PROPOSED, DECISION_IMPLEMENT, ROUTE_EXECUTION, execution,
            dry_run=bool(dry_run), source="team_execution",
            team_id=team_id, initiative=selected.get("initiative"),
            spec_sha256=team.fingerprint(selected),
            authorization=authorization, next_commands=commands,
        )
    missing = _missing_repositories(selected)
    if missing:
        execution = _execution(
            requested=True, started=False, gate=GATE_REPOSITORY,
            needs=("readable_repository",), next_steps=commands,
        )
        return _payload(
            STATUS_PROPOSED, DECISION_IMPLEMENT, ROUTE_EXECUTION, execution,
            dry_run=bool(dry_run), source="team_execution",
            team_id=team_id, initiative=selected.get("initiative"),
            spec_sha256=team.fingerprint(selected),
            authorization=authorization, next_commands=commands,
        )
    if dry_run:
        execution = _execution(requested=True, started=False,
                               gate=None, next_steps=commands)
        return _payload(
            STATUS_PROPOSED, DECISION_IMPLEMENT, ROUTE_EXECUTION, execution,
            dry_run=True, source="team_execution",
            team_id=team_id, initiative=selected.get("initiative"),
            spec_sha256=team.fingerprint(selected),
            authorization=authorization, next_commands=commands,
        )
    try:
        # The exact existing owner API, with no new authority flag: initialize is
        # idempotent on an identical manifest, set_desired re-verifies the
        # admission itself, and neither dispatches a worker nor writes a claim.
        team.initialize(selected, env=env, config=resolved, approved=False)
        summary = team.set_desired(team_id, "running", env=env)
    except team.TeamAdmissionError as exc:
        execution = _execution(
            requested=True, started=False, gate=GATE_OPERATOR_APPROVAL,
            needs=("operator_approval",), next_steps=commands,
        )
        payload = _payload(
            STATUS_PROPOSED, DECISION_IMPLEMENT, ROUTE_EXECUTION, execution,
            dry_run=False, source="team_execution",
            team_id=team_id, initiative=selected.get("initiative"),
            spec_sha256=team.fingerprint(selected),
            authorization=authorization, next_commands=commands,
        )
        payload["error"] = {"code": getattr(exc, "code", None) or
                            "TEAM_APPROVAL_REQUIRED",
                            "verdict": STATUS_PROPOSED, "message": str(exc)}
        return payload
    except (team.TeamError, product_config.ProductConfigError) as exc:
        return _refusal(getattr(exc, "code", None) or "TEAM_INVALID", str(exc))
    # ``--execute`` reuses the exact owner API ``cao team init`` / ``cao team
    # start`` already use, but setting the desired state dispatches no worker: it
    # is intent, not an agent. Only a slot the daemon actually put into
    # ``running`` is reported as started, so a just-advanced team is PROPOSED
    # (waiting for the existing daemon's reconcile), never falsely EXECUTING.
    started = _team_running(summary)
    next_steps = ([] if started
                  else _waiting_next_steps(team_id, summary, blocked=False,
                                           commands=commands))
    return _payload(
        STATUS_EXECUTING if started else STATUS_PROPOSED, DECISION_IMPLEMENT,
        ROUTE_EXECUTION,
        _execution(requested=True, started=started, gate=None,
                   next_steps=next_steps),
        dry_run=False, source="team_execution",
        team_id=team_id, initiative=selected.get("initiative"),
        spec_sha256=team.fingerprint(selected),
        authorization=authorization, next_commands=next_steps,
        team=_team_evidence(summary),
    )


def route(document: Any, *, env: Mapping[str, str] | None = None,
          config: Any = None, source: str | None = None,
          execute: bool = False, dry_run: bool = False,
          clock: Any = None) -> dict[str, Any]:
    """Route one request document deterministically and fail closed.

    The two mechanical routes are disjoint: an Intake request is admitted (and
    classified), a ``cao.team.v1`` manifest is reported to the governed Team
    Execution path. Nothing is dispatched by either route; a worker starts only
    later, through the existing daemon's own tick over an authorized team.
    """

    kind = classify(document)
    if kind == ROUTE_INTAKE:
        if execute:
            # Execution needs an exact manifest; a plain request can be an
            # execution *candidate*, but the flag is never silently ignored.
            return _refusal(
                "EXECUTION_REQUIRES_MANIFEST",
                "a plain request cannot be executed directly; provide an exact "
                "cao.team.v1 manifest with --file",
            )
        try:
            return _route_text(document, env=env, dry_run=dry_run, clock=clock)
        except intake.IntakeUnavailable as exc:
            return _refusal(getattr(exc, "code", None) or "INTAKE_UNAVAILABLE",
                            str(exc), status="UNAVAILABLE")
        except intake.IntakeError as exc:
            return _refusal(getattr(exc, "code", None) or "INTAKE_INVALID",
                            str(exc))
    if kind == ROUTE_EXECUTION:
        return _route_execution(document, env=env, config=config, source=source,
                                execute=execute, dry_run=dry_run)
    return _refusal(
        "UNKNOWN_REQUEST",
        "the request is neither a valid Intake request nor a cao.team.v1 "
        "manifest, so it cannot be routed without guessing",
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("request has a duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_token: str) -> Any:
    raise ValueError("request carries a non-finite JSON number")


def _load_document(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read one bounded request document with strict, fail-closed parsing.

    The final path component is opened ``O_NOFOLLOW`` and ``fstat``-checked as a
    regular file, so a symlink or a device is refused. Duplicate JSON keys and
    non-finite numbers are refused, and the document must be a JSON object.
    """

    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    fd = os.open(os.fspath(path), flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("request input must be a regular file")
        if info.st_size > MAX_REQUEST_BYTES:
            raise ValueError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            raw = stream.read(MAX_REQUEST_BYTES + 1)
    finally:
        if fd != -1:
            try:
                os.close(fd)
            except OSError:
                pass
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError(f"request exceeds {MAX_REQUEST_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("request is not valid UTF-8") from exc
    try:
        document = json.loads(text, object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
    except ValueError as exc:
        raise ValueError("request is not valid strict JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("request must be a JSON object")
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cao request",
        description=(
            "Send one request to CAO. CAO decides BACKLOG (recorded for later), "
            "INVESTIGATE or IMPLEMENT, routes to the existing Intake or governed "
            "Team Execution path, and dispatches nothing."
        ),
    )
    parser.add_argument("--file", metavar="PATH",
                        help="one JSON request document (Intake request or team manifest)")
    parser.add_argument("--item-id", metavar="ID",
                        help="stable item id for a plain-text request")
    parser.add_argument("--text", metavar="TEXT",
                        help="plain-text request body")
    parser.add_argument("--execute", action="store_true",
                        help=("route an exact qualified manifest into the existing "
                              "Team Execution owner path; it never records approval "
                              "and only advances a team whose authorization exists"))
    parser.add_argument("--dry-run", action="store_true",
                        help="classify and validate; admit nothing and start nothing")
    parser.add_argument("--json", action="store_true",
                        help="accepted for compatibility; output is always JSON")
    return parser


def _error(code: str, message: str) -> int:
    _emit({"schema": SCHEMA, "status": "UNAVAILABLE", "command": "request",
           "error": {"code": code, "verdict": "UNAVAILABLE", "message": message}})
    return EXIT_UNAVAILABLE


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv or ()))
    except SystemExit as exc:
        return int(exc.code)
    if args.file and (args.item_id or args.text):
        return _error("USAGE", "--file cannot be combined with --item-id/--text")
    if args.dry_run and args.execute:
        return _error("USAGE", "--dry-run cannot be combined with --execute")
    if args.execute and not args.file:
        return _error("USAGE", "--execute requires an exact --file team manifest")
    if args.file:
        try:
            document = _load_document(args.file)
        except (OSError, UnicodeError, ValueError) as exc:
            return _error("REQUEST_UNREADABLE", f"{type(exc).__name__}: {exc}")
        source = args.file
    elif args.item_id and args.text:
        document = {"schema": intake.REQUEST_SCHEMA,
                    "item_id": args.item_id, "body": args.text}
        source = None
    else:
        return _error("USAGE", "request requires --file, or --item-id and --text")
    result = route(document, source=source, execute=bool(args.execute),
                   dry_run=bool(args.dry_run))
    _emit(result)
    return EXIT_OK if result.get("status") in _ROUTED_OUTCOMES else EXIT_UNAVAILABLE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
