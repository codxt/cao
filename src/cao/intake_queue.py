"""Durable, opt-in Intake execution queue reconciled by the existing daemon (B7).

B7 is the durable **queue** the earlier Intake phases deliberately lack. B6 can
derive one gated READ handoff plan, but a plan that cannot be dispatched yet is
not remembered, so an operator must re-derive it by hand and nothing replays it
after a crash. B7 persists bounded, prequalified READ **intents** and reconciles
them -- once per **existing** CAO daemon tick -- into the **existing** CAO team
owner API. It adds no scheduler, no service, no database, no second ledger, no
Registry, no coordinator, no credential and no network egress.

Load-bearing rules this module enforces:

* **The daemon tick owns reconciliation.** Nothing here starts a loop, a timer
  or a thread. ``tick`` performs exactly one bounded pass and returns; the
  existing daemon calls it.
* **A queue intent is never its own approval.** An intent records an immutable
  binding, never authority. The only path that creates new work is an
  **in-process** authenticated operator-authority adapter (the B6 contract); a
  CLI flag, an environment value or a JSON file is caller-controlled and is
  never proof. Without such an adapter a prequalified intent is held with the
  precise actionable integration it is missing.
* **An already-approved team may re-verify and advance.** A persisted, already
  approved immutable CAO team bound to an intent may move PAUSED -> RUNNING
  only after its approval/spec digest, its Intake binding, its exact source HEAD
  and its dependencies are all re-validated from authoritative evidence. The
  revalidation is delegated to ``team.set_desired``, which recomputes the
  confirmation; a forged manifest or approval string is never trusted.
* **WRITE and every consequential effect stay held.** Only a fully READ
  ``cao.team.v1`` manifest for the bound submission is ever started; WRITE, new
  corrective batches, PR/CI/merge/deploy, credentials, permission changes and
  canonical acceptance are never dispatched here.
* **Fail closed.** A corrupt private record, a drifted source, a changed Intake
  binding, an unmet or missing dependency, a dependency cycle, an unreadable
  Work Registry or a conflicting Registry claim holds. An unapproved, stale or
  conflicted item blocks only itself; independent eligible items still advance.
* **Isolation, priority and a bounded budget.** Items are ordered by priority,
  then age, then id; at most ``ADVANCES_PER_TICK`` items advance per pass and
  held items never consume that budget. Two concurrent passes are serialized by
  a kernel lock; the loser reports ``LOCKED`` and mutates nothing.

Standard library only; no third-party dependency, no network and no credential.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
from collections.abc import Mapping
from typing import Any

try:  # POSIX kernel lock; absence fails closed rather than acting unlocked.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms only
    fcntl = None  # type: ignore[assignment]

from . import intake_handoff, product_config, product_state, registry, state, team
from .evidence import fingerprint, utc_now

__all__ = [
    "RESULT_SCHEMA",
    "INTENT_SCHEMA",
    "RECEIPT_SCHEMA",
    "TICK_SCHEMA",
    "INTENT_STATUSES",
    "TERMINAL_STATUSES",
    "ADVANCE",
    "HOLD",
    "NONE",
    "COMPLETE",
    "BLOCK",
    "MAX_ITEMS",
    "MAX_DEPENDS",
    "MAX_PRIORITY",
    "DEFAULT_PRIORITY",
    "ADVANCES_PER_TICK",
    "QUEUE_DIRNAME",
    "QueueError",
    "QueueInvalid",
    "QueueUnavailable",
    "QueueRefused",
    "QueueStateError",
    "QueueBusy",
    "queue_dir",
    "intent_path",
    "enqueue",
    "read_intent",
    "list_intents",
    "tick",
]

#: The enqueue result envelope.
RESULT_SCHEMA = "cao.intake.queue.result.v1"
#: The immutable binding plus its mutable, derived decision receipt.
INTENT_SCHEMA = "cao.intake.queue.intent.v1"
#: The deterministic decision/exception receipt persisted with each intent.
RECEIPT_SCHEMA = "cao.intake.queue.receipt.v1"
#: The read-only report of one bounded reconciliation pass.
TICK_SCHEMA = "cao.intake.queue.tick.v1"

#: Every intent status. A terminal status needs no further advance.
INTENT_STATUSES = ("PENDING", "STARTED", "HELD", "COMPLETE", "BLOCKED")
TERMINAL_STATUSES = ("COMPLETE", "BLOCKED")

#: The decision vocabulary for one reconciled item.
ADVANCE = "ADVANCE"
HOLD = "HOLD"
NONE = "NONE"
COMPLETE = "COMPLETE"
BLOCK = "BLOCK"

#: Bounds. The queue is deliberately small and every one is enforced.
MAX_ITEMS = 64
MAX_DEPENDS = 16
MAX_PRIORITY = 1000
DEFAULT_PRIORITY = 100
ADVANCES_PER_TICK = 1

#: The private queue store directory name under the XDG CAO state root.
QUEUE_DIRNAME = "intake-queue"

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEAD = re.compile(r"[0-9a-f]{40}\Z")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_IDENT = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")

#: The exact missing integration a held prequalified intent reports. This build
#: ships no authenticated host connector, so the daemon always passes ``None``.
_REQUIRED_INTEGRATION = (
    "supply an in-process adapter through the library 'operator_authority' "
    "argument that (1) authenticates the operator on a host-owned channel "
    "outside argv/env/JSON, (2) returns a grant bound to this intent's exact "
    "plan digest with read_only=true, and (3) names the operator, the scoped "
    "repository, the role, the model and the provider privacy qualification; "
    "the daemon never constructs it and this build ships none"
)


class QueueError(Exception):
    """Base class for the B7 Intake execution queue."""

    code = "INTAKE_QUEUE_ERROR"


class QueueInvalid(QueueError):
    """A request or an option is malformed; nothing was attempted."""

    code = "INTAKE_QUEUE_INVALID"


class QueueUnavailable(QueueError):
    """A required capability or fact is explicitly not offered."""

    code = "INTAKE_QUEUE_UNAVAILABLE"


class QueueRefused(QueueError):
    """A bound or scope check denied the enqueue."""

    code = "INTAKE_QUEUE_REFUSED"


class QueueStateError(QueueError):
    """Existing private state is present but not usable."""

    code = "INTAKE_QUEUE_STATE"


class QueueBusy(QueueError):
    """Another pass holds the queue; the caller mutates nothing."""

    code = "INTAKE_QUEUE_BUSY"


# ---------------------------------------------------------------------------
# Small strict validators
# ---------------------------------------------------------------------------


def _record_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise QueueInvalid(f"{field} must be 64 lowercase hex characters")
    return value


def _stamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _STAMP.fullmatch(value):
        raise QueueInvalid(f"{field} must be a UTC timestamp")
    return value


def _bounded_priority(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_PRIORITY:
        raise QueueInvalid(f"priority must be an integer within [0, {MAX_PRIORITY}]")
    return value


def _dependencies(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise QueueInvalid("depends_on must be a list of intent ids")
    deps: list[str] = []
    for entry in value:
        deps.append(_record_id(entry, "dependency id"))
    if len(deps) > MAX_DEPENDS:
        raise QueueInvalid(f"at most {MAX_DEPENDS} dependencies are allowed")
    if len(set(deps)) != len(deps):
        raise QueueInvalid("dependencies must be unique")
    return deps


# ---------------------------------------------------------------------------
# The private store (the existing CAO durable-state primitive)
# ---------------------------------------------------------------------------


def queue_dir(env: Mapping[str, str] | None = None) -> str:
    """The private queue store under the XDG CAO state root.

    The queue is always CAO-owned state under ``$XDG_STATE_HOME/cao``; the
    opt-in is the daemon's ``intake_queue`` flag, so the store location is never
    a caller-supplied path and no foreign directory is ever created or chmodded.
    """

    return os.path.join(product_state.state_dir(env), QUEUE_DIRNAME)


def intent_path(intent_id: str, env: Mapping[str, str] | None = None) -> str:
    iid = _record_id(intent_id, "intent id")
    return os.path.join(queue_dir(env), iid + ".json")


def _ensure_private_dir(directory: str) -> None:
    """Create/verify the ``0700`` queue store, failing closed on a symlink."""

    if os.path.islink(directory):
        raise QueueUnavailable("the queue store cannot be a symlink")
    if not os.path.isdir(directory):
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise QueueUnavailable(
                f"the queue store could not be created ({type(exc).__name__})"
            ) from exc
    info = os.stat(directory, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise QueueUnavailable("the queue store is not an owned directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(directory, 0o700)


# ---------------------------------------------------------------------------
# Intent construction, integrity and persistence
# ---------------------------------------------------------------------------


def _intent_digest(record: Mapping[str, Any]) -> str:
    """The content digest of one intent record (over everything but itself)."""

    return fingerprint({key: value for key, value in record.items() if key != "digest"})


def _receipt_digest(receipt: Mapping[str, Any]) -> str:
    """The deterministic digest of a receipt, excluding only its capture time."""

    return fingerprint(
        {key: value for key, value in receipt.items()
         if key not in ("digest", "observed_at")}
    )


def _verify_binding(binding: Any) -> dict[str, Any]:
    if not isinstance(binding, Mapping):
        raise QueueStateError("stored queue intent binding is malformed")
    submission_id = binding.get("submission_id")
    if not isinstance(submission_id, str) or not _HEX64.fullmatch(submission_id):
        raise QueueStateError("stored queue intent names no valid submission")
    if binding.get("read_only") is not True:
        raise QueueStateError("stored queue intent is not read-only")
    source = binding.get("source")
    if not isinstance(source, Mapping):
        raise QueueStateError("stored queue intent has no source binding")
    repo = source.get("repo")
    if not isinstance(repo, str) or not os.path.isabs(repo) or os.path.normpath(repo) != repo:
        raise QueueStateError("stored queue intent source is not a normalized checkout")
    head = source.get("head")
    if not isinstance(head, str) or not _HEAD.fullmatch(head):
        raise QueueStateError("stored queue intent source has no exact HEAD")
    for field in ("team_id", "task_id", "initiative"):
        if not isinstance(binding.get(field), str) or not _IDENT.fullmatch(binding[field]):
            raise QueueStateError(f"stored queue intent {field} is malformed")
    return dict(binding)


def _verify_intent(data: Any, expected_id: str) -> dict[str, Any]:
    """Strictly re-verify one stored intent; every defect fails closed."""

    if not isinstance(data, Mapping):
        raise QueueStateError("stored queue intent is not an object")
    if data.get("schema") != INTENT_SCHEMA:
        raise QueueStateError("stored queue intent has an unexpected schema")
    intent_id = data.get("intent_id")
    if intent_id != expected_id or not isinstance(intent_id, str) or not _HEX64.fullmatch(intent_id):
        raise QueueStateError("stored queue intent id is malformed")
    binding = _verify_binding(data.get("binding"))
    if intake_handoff.plan_id_for(binding) != intent_id:
        raise QueueStateError("stored queue intent does not match its own binding")
    _bounded_priority(data.get("priority"))
    deps = _dependencies(data.get("depends_on"))
    if intent_id in deps:
        raise QueueStateError("stored queue intent depends on itself")
    _stamp(data.get("created_at"), "stored intent created_at")
    if data.get("status") not in INTENT_STATUSES:
        raise QueueStateError("stored queue intent status is malformed")
    receipt = data.get("receipt")
    if receipt is not None:
        if not isinstance(receipt, Mapping) or receipt.get("schema") != RECEIPT_SCHEMA:
            raise QueueStateError("stored queue intent receipt is malformed")
    digest = data.get("digest")
    if not isinstance(digest, str) or digest != _intent_digest(data):
        raise QueueStateError("stored queue intent digest does not match its content")
    return dict(data)


def _write_intent(record: Mapping[str, Any], env: Mapping[str, str] | None) -> dict[str, Any]:
    directory = queue_dir(env)
    _ensure_private_dir(directory)
    try:
        return state.write_state(intent_path(record["intent_id"], env), record)
    except state.StateError as exc:
        raise QueueStateError(
            f"the queue intent could not be persisted ({type(exc).__name__})"
        ) from exc


def read_intent(intent_id: str, env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """Read one full intent record, or ``None``; a corrupt record raises."""

    iid = _record_id(intent_id, "intent id")
    try:
        data = state.read_state(intent_path(iid, env))
    except state.StateError as exc:
        raise QueueStateError(
            f"the stored queue intent could not be read ({type(exc).__name__})"
        ) from exc
    if data is None:
        return None
    return _verify_intent(data, iid)


def list_intents(env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Every stored intent, ordered by priority, then age, then id.

    A malformed stored record raises rather than being skipped, and a store
    larger than :data:`MAX_ITEMS` fails closed rather than being truncated.
    """

    directory = queue_dir(env)
    if not os.path.isdir(directory):
        return []
    records: list[dict[str, Any]] = []
    for name in sorted(os.listdir(directory)):
        if name.startswith(".") or not name.endswith(".json"):
            continue
        iid = name[: -len(".json")]
        if not _HEX64.fullmatch(iid):
            raise QueueStateError("the queue store holds an unexpected file")
        try:
            data = state.read_state(os.path.join(directory, name))
        except state.StateError as exc:
            raise QueueStateError(
                f"the stored queue intent could not be read ({type(exc).__name__})"
            ) from exc
        if data is None:
            continue
        records.append(_verify_intent(data, iid))
        if len(records) > MAX_ITEMS:
            raise QueueStateError("the queue holds more than its bounded size")
    records.sort(key=lambda item: (item["priority"], item["created_at"], item["intent_id"]))
    return records


# ---------------------------------------------------------------------------
# Enqueue: derive one prequalified READ intent (no work, no authority)
# ---------------------------------------------------------------------------


def enqueue(
    submission_id: str,
    *,
    repo: str,
    source_ref: str | None = None,
    priority: int = DEFAULT_PRIORITY,
    depends_on: Any = (),
    dry_run: bool = False,
    env: Mapping[str, str] | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Queue one prequalified, source-bound READ intent, or report why not.

    The intent is derived from the B6 handoff through ``intake_handoff``: a
    request that is not an eligible READ, that is already covered by a linked
    READ audit, or that binds no exact source HEAD is reported truthfully and
    **nothing is persisted**. Enqueueing never creates a team, an agent or a
    Registry claim and never accepts authority.
    """

    sid = _record_id(submission_id, "submission id")
    if not isinstance(repo, str) or not os.path.isabs(repo) or os.path.normpath(repo) != repo:
        raise QueueInvalid("repo must be a normalized absolute checkout path")
    if source_ref is not None and not isinstance(source_ref, str):
        raise QueueInvalid("source_ref must be a string or omitted")
    priority = _bounded_priority(priority)
    deps = _dependencies(depends_on)

    preflight = intake_handoff.handoff_plan(
        sid, repo=repo, source_ref=source_ref, dry_run=True, env=env, clock=clock
    )
    status = preflight.get("status")
    plan = preflight.get("plan") or {}
    result: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "submission_id": sid,
        "persisted": False,
        "intent_id": None,
        "intent": None,
        "status": status,
        "reason": None,
        "holds": list((plan.get("eligibility") or {}).get("holds") or []),
        "refusals": list(plan.get("refusals") or []),
        "authority": "none",
        "dispatch": False,
        "egress": "none",
    }
    if status == "ALREADY_STARTED":
        result["reason"] = (
            "an independent READ audit is already linked to this submission; the "
            "existing daemon reconciles it and no second team is queued"
        )
        return result
    if status != "PLAN_ONLY":
        result["reason"] = (
            "only an eligible READ handoff can be queued; the derived handoff is "
            f"{status}"
        )
        return result

    binding_raw = plan.get("binding")
    if not isinstance(binding_raw, Mapping):
        result["status"] = "HOLD"
        result["reason"] = "the derived handoff carries no bindable identity"
        return result
    source_raw = binding_raw.get("source")
    if (not isinstance(source_raw, Mapping)
            or source_raw.get("verdict") != "SUPPORTED"
            or not source_raw.get("head")):
        result["status"] = "HOLD"
        result["holds"] = list(result["holds"]) + [
            {"code": "source_head_binding",
             "reason": "the intent binds no exact, readable source ref/HEAD"}
        ]
        result["reason"] = "the request binds no exact source HEAD"
        return result
    binding = _verify_binding(binding_raw)

    intent_id = intake_handoff.plan_id_for(binding)
    if intent_id in deps:
        raise QueueInvalid("an intent cannot depend on itself")
    now = utc_now(clock)
    record: dict[str, Any] = {
        "schema": INTENT_SCHEMA,
        "intent_id": intent_id,
        "status": "PENDING",
        "priority": priority,
        "depends_on": list(deps),
        "binding": binding,
        "approval": None,
        "receipt": None,
        "created_at": now,
        "updated_at": now,
    }
    record["digest"] = _intent_digest(record)
    _verify_intent(record, intent_id)  # a self-check; a defect never persists
    result["intent_id"] = intent_id

    if dry_run:
        result["status"] = "DRY_RUN"
        result["intent"] = record
        result["reason"] = "dry run: the intent was validated and not persisted"
        return result

    existing = read_intent(intent_id, env)
    if existing is not None:
        result.update(
            {"status": "DUPLICATE", "persisted": True, "intent": existing,
             "reason": "an identical intent already exists"}
        )
        return result
    if len(list_intents(env)) >= MAX_ITEMS:
        raise QueueRefused("the queue is full; no further intent is admitted")
    stored = _write_intent(record, env)
    result.update(
        {"status": "QUEUED", "persisted": True, "intent": stored,
         "reason": "the READ intent is queued and awaits the existing daemon"}
    )
    return result


# ---------------------------------------------------------------------------
# Read-only Work Registry check (existing ledger; never a second one)
# ---------------------------------------------------------------------------


def _registry_conflict(
    binding: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None,
    registry_db: str | None,
) -> dict[str, Any]:
    """Whether an independent active claim conflicts with this intent's scope.

    The Registry is opened through ``connect_readonly`` (SQLite ``mode=ro``), so
    a write is impossible by construction. An absent ledger is genuinely no
    conflict; an unreadable one is ``UNKNOWN`` and holds rather than advancing.
    """

    try:
        ledger = registry_db or product_config.registry_db_path(env)
    except Exception:  # noqa: BLE001 - an unresolvable location is UNKNOWN
        return {"verdict": "UNKNOWN", "conflict": False,
                "reason": "the local Work Registry location could not be resolved"}
    if not os.path.isfile(ledger):
        return {"verdict": "SUPPORTED", "conflict": False, "reason": None}
    try:
        conn = registry.connect_readonly(ledger)
    except Exception:  # noqa: BLE001 - a local read failure is UNKNOWN
        return {"verdict": "UNKNOWN", "conflict": False,
                "reason": "the local Work Registry could not be read"}
    try:
        claims = registry.list_active_claims(conn)
    except Exception:  # noqa: BLE001 - a local read failure is UNKNOWN
        return {"verdict": "UNKNOWN", "conflict": False,
                "reason": "the local Work Registry could not be read"}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - closing is best-effort
            pass
    own_run = "cao-team-" + str(binding.get("team_id"))
    bound_repo = (binding.get("source") or {}).get("repo")
    objective = " ".join(
        part for part in (
            str(binding.get("initiative") or ""),
            str(binding.get("team_id") or ""),
            str(binding.get("task_id") or ""),
        ) if part
    )
    tokens = registry.objective_tokens({"objective": objective})
    for claim in claims[:64]:
        if not isinstance(claim, Mapping) or claim.get("run_id") == own_run:
            continue
        claim_repo = claim.get("repo")
        same_repo = (
            isinstance(claim_repo, str) and isinstance(bound_repo, str)
            and claim_repo and bound_repo
            and os.path.normpath(claim_repo) == os.path.normpath(bound_repo)
        )
        score = registry.lexical_similarity(tokens, registry.objective_tokens(dict(claim)))
        if same_repo or score >= registry.OVERLAP_THRESHOLD:
            return {
                "verdict": "SUPPORTED",
                "conflict": True,
                "against_run": str(claim.get("run_id") or "")[:64],
                "against_work": str(claim.get("work_key") or "")[:64],
                "same_repo": same_repo,
                "score": round(float(score), 4),
                "reason": "an independent active Registry claim holds this "
                          "source/scope, so the Work Registry is honored",
            }
    return {"verdict": "SUPPORTED", "conflict": False, "reason": None}


# ---------------------------------------------------------------------------
# The one-pass reconciliation owned by the existing daemon
# ---------------------------------------------------------------------------


def _hold(code: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"decision": HOLD, "status": "HELD", "code": code, "reason": reason, **extra}


def _is_read_manifest_for(spec: Any, submission_id: str) -> bool:
    """Whether ``spec`` is a fully READ manifest that cites the submission.

    Mirrors B6's link validation: every task must be READ with no write scope,
    and the immutable manifest itself (initiative, prompts, role instructions)
    must cite the submission id. A caller-supplied field is never consulted.
    """

    if not isinstance(spec, Mapping):
        return False
    tasks = spec.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return False
    for task in tasks:
        if not isinstance(task, Mapping):
            return False
        if task.get("mode") != "read" or task.get("write_scope"):
            return False
    texts: list[str] = [str(spec.get("initiative") or "")]
    for task in tasks:
        if isinstance(task, Mapping):
            texts.append(str(task.get("prompt") or ""))
    roles = spec.get("roles")
    if isinstance(roles, Mapping):
        for role in roles.values():
            if isinstance(role, Mapping):
                texts.append(str(role.get("instruction") or ""))
    return any(submission_id in text for text in texts)


def _approval_evidence(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The persisted approval digest of a started team, or ``None`` (legacy)."""

    admission = record.get("admission")
    if not isinstance(admission, Mapping):
        return None
    return {
        "method": str(admission.get("method") or "")[:64] or None,
        "spec_sha256": admission.get("spec_sha256"),
        "routes_sha256": admission.get("routes_sha256"),
    }


def _cycle_members(by_id: Mapping[str, Mapping[str, Any]]) -> set[str]:
    """Every intent that participates in a dependency cycle."""

    color: dict[str, int] = {iid: 0 for iid in by_id}
    members: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        color[node] = 1
        for dep in by_id[node]["depends_on"]:
            if dep not in by_id:
                continue
            if color[dep] == 1:
                members.add(dep)
                if dep in stack:
                    members.update(stack[stack.index(dep):])
            elif color[dep] == 0:
                visit(dep, stack + [dep])
        color[node] = 2

    for iid in by_id:
        if color[iid] == 0:
            visit(iid, [iid])
    return members


def _gates(
    intent: Mapping[str, Any],
    by_id: Mapping[str, Mapping[str, Any]],
    *,
    env: Mapping[str, str] | None,
    clock: Any,
    registry_db: str | None,
) -> dict[str, Any] | None:
    """Every pre-advance gate, or ``None`` when the intent may advance.

    Each gate fails closed and blocks only the item being reconciled.
    """

    binding = intent["binding"]
    for dep in intent["depends_on"]:
        dep_intent = by_id.get(dep)
        if dep_intent is None:
            return _hold("dependency_missing",
                         f"dependency {dep[:12]} is not in the queue")
        if dep_intent["status"] != "COMPLETE":
            return _hold("dependency_unmet",
                         f"dependency {dep[:12]} is {dep_intent['status']}")

    source = binding["source"]
    try:
        preflight = intake_handoff.handoff_plan(
            binding["submission_id"], repo=source.get("repo"),
            source_ref=source.get("ref"), dry_run=True, env=env, clock=clock,
        )
    except intake_handoff.HandoffError as exc:
        return _hold("source_unverified",
                     f"the bound source could not be re-read ({type(exc).__name__})")
    plan = preflight.get("plan") or {}
    if (plan.get("eligibility") or {}).get("verdict") != "ELIGIBLE_READ":
        return _hold("eligibility_not_read",
                     "the request no longer qualifies as an eligible READ")
    current = plan.get("binding") or {}
    if (current.get("source") or {}).get("head") != source.get("head"):
        return _hold("source_head_drift",
                     "the bound source HEAD moved after the intent was queued")
    if current != binding:
        return _hold("intake_binding_changed",
                     "the Intake/triage binding changed after the intent was queued")

    conflict = _registry_conflict(binding, env=env, registry_db=registry_db)
    if conflict["verdict"] == "UNKNOWN":
        return _hold("registry_unavailable", conflict["reason"])
    if conflict.get("conflict"):
        return _hold("registry_conflict", conflict["reason"])
    return None


def _advance(
    intent: Mapping[str, Any],
    by_id: Mapping[str, Mapping[str, Any]],
    *,
    env: Mapping[str, str] | None,
    clock: Any,
    registry_db: str | None,
    operator_authority: Any,
    budget_left: int,
) -> dict[str, Any]:
    """Reconcile exactly one intent into the existing CAO team owner API."""

    def deferred() -> dict[str, Any]:
        return {"decision": NONE, "status": intent["status"], "code": "budget",
                "reason": "the per-tick advance budget is spent"}

    binding = intent["binding"]
    submission_id = binding["submission_id"]
    team_id = binding["team_id"]
    source = binding["source"]

    try:
        record = team._read(team_id, env)
    except team.TeamError as exc:
        return _hold("team_unusable",
                     f"the bound team checkpoint is not usable ({type(exc).__name__})")

    if record is not None:
        if not _is_read_manifest_for(record.get("spec"), submission_id):
            return _hold("not_read_only",
                         "the bound team manifest is not a fully READ audit of "
                         "this submission and is never started by the queue")
        try:
            summary = team.status(team_id, env=env)
        except team.TeamError as exc:
            return _hold("team_unusable",
                         f"the bound team status could not be read ({type(exc).__name__})")
        overall = summary.get("status")
        if overall == "COMPLETE":
            return {"decision": COMPLETE, "status": "COMPLETE", "code": None,
                    "reason": "the bound team reached COMPLETE",
                    "team_id": team_id}
        if overall in ("BLOCKED", "CORRUPT"):
            return {"decision": BLOCK, "status": "BLOCKED", "code": "team_blocked",
                    "reason": f"the bound team reports {overall}", "team_id": team_id}
        if record.get("desired") == "running" or overall == "RUNNING":
            return {"decision": NONE, "status": "STARTED", "code": None,
                    "reason": "the bound team is already running and is reconciled "
                              "by the existing daemon",
                    "team_id": team_id,
                    "approval": _approval_evidence(record)}
        gate = _gates(intent, by_id, env=env, clock=clock, registry_db=registry_db)
        if gate is not None:
            return gate
        if budget_left <= 0:
            return deferred()
        try:
            team.set_desired(team_id, "running", env=env)
        except team.TeamAdmissionError as exc:
            return _hold("approval_unverified",
                         "the team's original approval/spec digest did not "
                         f"re-verify: {exc}")
        except team.TeamError as exc:
            return _hold("team_unusable",
                         f"the team owner API refused to start the bound team: {exc}")
        return {"decision": ADVANCE, "status": "STARTED", "code": None,
                "reason": "the already-approved immutable team was revalidated "
                          "and started",
                "team_id": team_id,
                "approval": _approval_evidence(record)}

    gate = _gates(intent, by_id, env=env, clock=clock, registry_db=registry_db)
    if gate is not None:
        return gate
    if operator_authority is None:
        return _hold(
            "authenticated_operator_authority",
            "no in-process authenticated operator authority is available, so a "
            "prequalified READ intent is never dispatched",
            required_integration=_REQUIRED_INTEGRATION,
        )
    if budget_left <= 0:
        return deferred()
    try:
        dispatched = intake_handoff.handoff_plan(
            submission_id, repo=source.get("repo"), source_ref=source.get("ref"),
            env=env, operator_authority=operator_authority, clock=clock,
        )
    except intake_handoff.HandoffError as exc:
        return _hold("dispatch_refused",
                     f"the governed handoff refused the dispatch "
                     f"({type(exc).__name__})")
    if dispatched.get("status") not in ("ADMITTED", "DUPLICATE"):
        return _hold("dispatch_refused",
                     f"the governed handoff returned {dispatched.get('status')}")
    plan = dispatched.get("plan") or {}
    return {"decision": ADVANCE, "status": "STARTED", "code": None,
            "reason": "an in-process authenticated operator authority admitted "
                      "exactly one scoped READ execution",
            "team_id": team_id,
            "approval": plan.get("grant")}


def _apply(
    intent: Mapping[str, Any],
    decision: Mapping[str, Any],
    *,
    now: str,
    env: Mapping[str, str] | None,
) -> tuple[dict[str, Any], bool]:
    """Persist the derived receipt and status, and report whether it changed."""

    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "decision": decision["decision"],
        "status": decision["status"],
        "code": decision.get("code"),
        "reason": decision.get("reason"),
        "team_id": decision.get("team_id"),
        "approval": decision.get("approval"),
        "required_integration": decision.get("required_integration"),
    }
    digest = _receipt_digest(receipt)
    existing = intent.get("receipt") or {}
    observed = existing.get("observed_at") if existing.get("digest") == digest else now
    receipt["observed_at"] = observed if isinstance(observed, str) else now
    receipt["digest"] = digest

    if intent["status"] == decision["status"] and existing.get("digest") == digest:
        return dict(intent), False
    record = dict(intent)
    record["status"] = decision["status"]
    record["receipt"] = receipt
    record["updated_at"] = now
    record["digest"] = _intent_digest(record)
    return _write_intent(record, env), True


@contextlib.contextmanager
def _queue_lock(directory: str):
    """Serialize queue passes with a non-blocking kernel lock."""

    if fcntl is None:
        raise QueueUnavailable("POSIX kernel lock unavailable; refusing to reconcile")
    _ensure_private_dir(directory)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(os.path.join(directory, ".lock"), flags, 0o600)
    except OSError as exc:
        raise QueueUnavailable("the queue lock is unavailable") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise QueueStateError("the queue lock is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise QueueBusy("another queue pass holds the lock") from exc
        yield
    finally:
        os.close(fd)


def _exception(code: str, *, subject: str, detail: str | None, head: str | None) -> dict[str, Any]:
    """One bounded exception row, compatible with the daemon's metrics report."""

    return {
        "code": code,
        "source": "intake_queue",
        "subject": subject[:64],
        "detail": detail[:200] if isinstance(detail, str) else None,
        "observed": None,
        "head": head if isinstance(head, str) else None,
    }


def tick(
    *,
    env: Mapping[str, str] | None = None,
    clock: Any = None,
    registry_db: str | None = None,
    operator_authority: Any = None,
    budget: int | None = None,
) -> dict[str, Any]:
    """Run exactly one bounded reconciliation pass over the queue.

    The existing daemon calls this each tick. It starts, ticks and schedules
    nothing else; it advances at most ``budget`` eligible items, holds each
    blocked item in place and mutates no Registry row and no canonical state.
    """

    directory = queue_dir(env)
    advances = ADVANCES_PER_TICK if budget is None else budget
    if isinstance(advances, bool) or not isinstance(advances, int) or advances < 0:
        raise QueueInvalid("budget must be a non-negative integer")
    try:
        with _queue_lock(directory):
            return _tick_locked(env=env, clock=clock, registry_db=registry_db,
                                 operator_authority=operator_authority, budget=advances)
    except QueueBusy:
        return {"schema": TICK_SCHEMA, "status": "LOCKED", "directory": directory,
                "items": [], "advanced": 0, "effects": [], "exceptions": []}


def _tick_locked(
    *,
    env: Mapping[str, str] | None,
    clock: Any,
    registry_db: str | None,
    operator_authority: Any,
    budget: int,
) -> dict[str, Any]:
    directory = queue_dir(env)
    intents = list_intents(env)
    if not intents:
        return {"schema": TICK_SCHEMA, "status": "IDLE", "directory": directory,
                "items": [], "advanced": 0, "effects": [], "exceptions": []}

    now = utc_now(clock)
    by_id = {intent["intent_id"]: intent for intent in intents}
    cycles = _cycle_members(by_id)
    items: list[dict[str, Any]] = []
    effects: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    used = 0

    for intent in intents:
        intent_id = intent["intent_id"]
        if intent_id in cycles:
            decision = _hold("dependency_cycle",
                             "the intent participates in a dependency cycle")
        elif intent["status"] in TERMINAL_STATUSES:
            decision = {"decision": NONE, "status": intent["status"], "code": None,
                        "reason": "the intent is terminal"}
        else:
            decision = _advance(intent, by_id, env=env, clock=clock,
                                registry_db=registry_db,
                                operator_authority=operator_authority,
                                budget_left=budget - used)
            if decision["decision"] == ADVANCE:
                used += 1

        _updated, changed = _apply(intent, decision, now=now, env=env)
        head = (intent["binding"].get("source") or {}).get("head")
        item = {
            "intent_id": intent_id,
            "priority": intent["priority"],
            "status": decision["status"],
            "decision": decision["decision"],
            "code": decision.get("code"),
            "reason": decision.get("reason"),
            "team_id": intent["binding"].get("team_id"),
            "submission_id": intent["binding"].get("submission_id"),
            "approval": decision.get("approval"),
        }
        items.append(item)
        effects.append({
            "action": "intake_queue_" + decision["decision"].lower(),
            "intent_id": intent_id,
            "status": decision["status"],
            "code": decision.get("code"),
            "changed": changed,
        })
        if decision["decision"] == HOLD:
            exceptions.append(_exception(
                str(decision.get("code") or "HELD"),
                subject=intent_id,
                detail=decision.get("reason"),
                head=head,
            ))

    return {"schema": TICK_SCHEMA, "status": "RUNNING", "directory": directory,
            "items": items, "advanced": used, "effects": effects,
            "exceptions": exceptions}
