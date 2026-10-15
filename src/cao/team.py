"""Durable specialist-team orchestration over the existing CAO task/Registry.

Team state tracks dependencies, handoffs, bounded review/correction and roles.
Chat approval records the operator-declared team decision; it does not
authenticate a person or grant publication or deployment authority.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from . import (agent_profiles, evidence, product_config, product_driver,
               product_state, product_task, registry, state, team_approval,
               team_recovery)

SPEC_SCHEMA = "cao.team.v1"
STATE_SCHEMA = "cao.team.state.v1"
_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_HEAD = re.compile(r"[0-9a-f]{40}\Z")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_PROGRESS = re.compile(r"(sha256:[0-9a-f]{64})?\Z")
MAX_TASKS = 64
MAX_WRITERS = 2
MAX_PARALLEL = 8
#: A task whose durable record reached one of these can no longer change, so it
#: can never be reported as stalled.
_TERMINAL_TASK_STATUSES = ("complete", "stopped")


class TeamError(ValueError):
    """An unsafe team contract, state or operation."""
    code = "TEAM_INVALID"


class TeamAdmissionError(TeamError):
    """The operator's independent approval is absent or not verifiable."""
    code = "TEAM_APPROVAL_REQUIRED"


def _fields(value: object, allowed: set[str], required: set[str], label: str) -> dict:
    if type(value) is not dict or set(value) - allowed or required - set(value):
        raise TeamError(f"{label}: missing/unknown fields")
    return value


def _ident(value: object) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise TeamError("team/task/role ID must be a bounded lowercase identifier")
    return value


def _number(value: object, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise TeamError(f"integer must be within [{low},{high}]")
    return value


def _string(value: object, limit: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise TeamError("text is absent or too long")
    return value


def validate_spec(value: Any) -> dict[str, Any]:
    raw = _fields(value, {"schema", "team_id", "initiative", "supersedes",
                          "change_reason", "roles", "tasks", "max_writers", "max_parallel"},
                  {"schema", "team_id", "roles", "tasks"}, "team")
    if raw["schema"] != SPEC_SCHEMA:
        raise TeamError("unsupported team specification")
    _ident(raw["team_id"])
    if "initiative" in raw:
        _ident(raw["initiative"])
    if "supersedes" in raw:
        _ident(raw["supersedes"])
        if raw["supersedes"] == raw["team_id"] or not raw.get("initiative"):
            raise TeamError("successor must name a distinct prior team and initiative")
        if ("change_reason" not in raw or type(raw["change_reason"]) is not str
                or not 20 <= len(raw["change_reason"].strip()) <= 1000):
            raise TeamError("team successor requires a substantive change reason")
    elif "change_reason" in raw:
        raise TeamError("change reason requires an explicit predecessor")
    writers = _number(raw.get("max_writers", 2), 0, MAX_WRITERS)
    parallel = _number(raw.get("max_parallel", 4), 1, MAX_PARALLEL)
    roles = raw["roles"]
    if type(roles) is not dict or not 1 <= len(roles) <= 16:
        raise TeamError("roles must be a bounded object")
    for name, role in roles.items():
        _ident(name)
        _fields(role, {"instruction", "model", "agent_profile"}, {"instruction"}, "role")
        if "agent_profile" in role:
            _ident(role["agent_profile"])
            if "model" in role:
                raise TeamError("agent_profile and inline model cannot be combined")
        _string(role["instruction"], 2000)
        if "model" in role:
            model = _string(role["model"], 200)
            if (not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._:@+/#-]{0,150}",
                    model) or ".." in model or "//" in model):
                raise TeamError("role model must be an exact safe provider/model ID")
    if type(raw["tasks"]) is not list or not 1 <= len(raw["tasks"]) <= MAX_TASKS:
        raise TeamError("tasks must be a bounded array")
    tasks = {}
    for t in raw["tasks"]:
        _fields(t, {"id", "role", "repo", "prompt", "mode", "write_scope",
                    "depends_on", "reviewer", "max_corrections"},
                {"id", "role", "repo", "prompt", "mode"}, "task")
        key = _ident(t["id"])
        if key in tasks or type(t["role"]) is not str or t["role"] not in roles:
            raise TeamError("duplicate task ID or unknown role")
        repo = _string(t["repo"], 1024)
        if not os.path.isabs(repo) or os.path.normpath(repo) != repo:
            raise TeamError("repository must be a normalized absolute checkout path")
        _string(t["prompt"], 12000)
        if t["mode"] not in ("read", "write"):
            raise TeamError("mode must be read or write")
        scope = t.get("write_scope", [])
        if type(scope) is not list or len(scope) > 128:
            raise TeamError("write_scope must be a bounded list")
        scope = product_task.normalize_scope(scope)
        if bool(scope) != (t["mode"] == "write"):
            raise TeamError("WRITE requires scope; READ must not carry scope")
        deps = t.get("depends_on", [])
        if (type(deps) is not list or len(deps) > MAX_TASKS
                or any(type(d) is not str or not _ID.fullmatch(d) for d in deps)
                or len(deps) != len(set(deps))):
            raise TeamError("dependencies must be unique task IDs")
        review = t.get("reviewer")
        if review is not None and (type(review) is not str or review not in roles
                                   or review == t["role"] or t["mode"] != "write"):
            raise TeamError("reviewer must be a distinct declared role on a WRITE task")
        corrections = _number(t.get("max_corrections", 0), 0, 3)
        if corrections and review is None:
            raise TeamError("corrections require a reviewer")
        tasks[key] = t
    if writers == 0 and any(t["mode"] == "write" for t in tasks.values()):
        raise TeamError("WRITE tasks require nonzero writer capacity")
    visiting, visited = set(), set()

    def visit(name: str) -> None:
        if name in visiting:
            raise TeamError("dependency cycle")
        if name in visited:
            return
        visiting.add(name)
        for dep in tasks[name].get("depends_on", []):
            if dep not in tasks:
                raise TeamError("unknown dependency")
            visit(dep)
        visiting.remove(name)
        visited.add(name)
    for name in tasks:
        visit(name)
    normalized = copy.deepcopy(raw)
    normalized["max_writers"] = writers
    normalized["max_parallel"] = parallel
    for task in normalized["tasks"]:
        task.setdefault("write_scope", [])
        task.setdefault("depends_on", [])
        task.setdefault("reviewer", None)
        task.setdefault("max_corrections", 0)
    return normalized


def fingerprint(spec: Mapping[str, Any]) -> str:
    payload = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def team_dir(env: Mapping[str, str] | None = None) -> str:
    return os.path.join(product_state.state_dir(env), "teams")


def team_path(team_id: str, env: Mapping[str, str] | None = None) -> str:
    return os.path.join(team_dir(env), _ident(team_id) + ".json")


@contextlib.contextmanager
def dispatch_lock(env: Mapping[str, str] | None = None):
    """A process-shared dispatch mutex; never substitutes for a Registry claim."""
    directory = team_dir(env)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(directory, ".dispatch.lock"),
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TeamError("another CAO team dispatch holds the lock") from exc
        yield
    finally:
        os.close(fd)


def _read(team_id: str, env: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    record = state.read_state(team_path(team_id, env))
    if record is None:
        return None
    if record.get("schema") != STATE_SCHEMA or record.get("team_id") != team_id:
        raise TeamError("team checkpoint identity/schema mismatch")
    spec = validate_spec(record.get("spec"))
    if spec != record["spec"] or fingerprint(spec) != record.get("spec_hash"):
        raise TeamError("team manifest does not match immutable checkpoint")
    slots = record.get("slots")
    if type(slots) is not dict or set(slots) != {t["id"] for t in spec["tasks"]}:
        raise TeamError("malformed task checkpoint set")
    if record.get("desired") not in ("paused", "running"):
        raise TeamError("malformed team desired state")
    if type(record.get("tick_count")) is not int or record["tick_count"] < 0:
        raise TeamError("malformed team tick counter")
    expected_fields = {"phase", "round", "status", "execution_id",
                       "prompt", "head", "feedback", "output", "error"}
    liveness_fields = {"started_at", "progress_at", "progress_key"}
    pin_fields = {"pinned_head", "pinned_branch"}
    for task in spec["tasks"]:
        slot = slots[task["id"]]
        if type(slot) is not dict:
            raise TeamError("malformed team task checkpoint")
        phase, current = slot.get("phase"), slot.get("status")
        round_no = slot.get("round")
        retry_no = slot.get("retry_no", 0)
        if (not expected_fields.issubset(slot)
                or set(slot) - expected_fields - liveness_fields - pin_fields - {"retry_no"}
                or type(retry_no) is not int or not 0 <= retry_no <= 1
                # Only a READ execution may be retried. A review is always run
                # as a READ execution, so a review-phase retry is bounded by
                # the reviewer's own durable READ task and claim instead.
                or (retry_no and phase not in ("work", "review"))
                or (retry_no and phase == "work" and task["mode"] != "read")
                or phase not in ("work", "review", "correction")
                or current not in ("pending", "running", "complete", "blocked", "unknown")
                or type(round_no) is not int or not 0 <= round_no <= task["max_corrections"]):
            raise TeamError("malformed team task checkpoint")
        if ((phase == "work" and round_no != 0)
                or (phase == "correction" and (round_no == 0 or not task["reviewer"]))
                or (phase == "review" and not task["reviewer"])):
            raise TeamError("invalid team task phase or correction round")
        head = slot.get("head")
        if head is not None and (type(head) is not str or not _HEAD.fullmatch(head)):
            raise TeamError("malformed team task revision")
        pinned_head = slot.get("pinned_head")
        if (pinned_head is not None
                and (type(pinned_head) is not str or not _HEAD.fullmatch(pinned_head))):
            raise TeamError("malformed team task revision pin")
        pinned_branch = slot.get("pinned_branch")
        if (pinned_branch is not None
                and (type(pinned_branch) is not str or not 0 < len(pinned_branch) <= 200)):
            raise TeamError("malformed team task branch pin")
        if phase in ("review", "correction") and head is None:
            raise TeamError("review/correction lacks accepted source revision")
        if current == "complete" and (
                head is None or (task["reviewer"] is not None and phase != "review")):
            raise TeamError("completed task lacks accepted review or source revision")
        if (slot.get("feedback") is not None and
                (type(slot["feedback"]) is not str or len(slot["feedback"]) > 5000)):
            raise TeamError("malformed review feedback")
        if (slot.get("output") is not None and
                (type(slot["output"]) is not str or len(slot["output"]) > 5000)):
            raise TeamError("malformed task output")
        if slot.get("error") is not None and type(slot["error"]) is not str:
            raise TeamError("malformed task error")
        for field in ("started_at", "progress_at"):
            if slot.get(field) is not None and not _STAMP.fullmatch(slot[field]):
                raise TeamError("malformed team task liveness timestamp")
        key = slot.get("progress_key")
        if key is not None and (type(key) is not str or not _PROGRESS.fullmatch(key)):
            raise TeamError("malformed team task progress evidence")
        if current == "pending":
            if slot["execution_id"] is not None or slot["prompt"] is not None:
                raise TeamError("pending task unexpectedly has a launched execution")
        elif slot["execution_id"] is not None:
            identity = _execution_id(team_id, task["id"], phase, round_no, retry_no)
            if slot["execution_id"] != identity:
                raise TeamError("stored execution identity does not match team task")
            if type(slot["prompt"]) is not str or not 0 < len(slot["prompt"]) <= 48000:
                raise TeamError("started task lacks its original bounded prompt")
        elif current in ("running", "complete"):
            raise TeamError("active/completed task has no execution identity")
    _validate_recovery(record.get("recovery"))
    return record


def _validate_recovery(value: Any) -> None:
    """Validate the bounded per-team recovery receipt, or refuse it."""

    if value is None:
        return
    if (type(value) is not dict
            or value.get("schema") != team_recovery.SCHEMA
            or type(value.get("decisions")) is not dict
            or type(value.get("resumed")) is not dict
            or len(value["decisions"]) > team_recovery.MAX_DECISIONS
            or len(value["resumed"]) > team_recovery.MAX_DECISIONS * 8):
        raise TeamError("malformed team recovery receipt")


def _save(record: dict[str, Any], env: Mapping[str, str] | None) -> None:
    state.write_state(team_path(record["team_id"], env), record)


def summary(record: Mapping[str, Any]) -> dict[str, Any]:
    slots = {name: {"status": slot["status"], "phase": slot["phase"],
                    "round": slot["round"], "execution_id": slot["execution_id"],
                    "head": slot["head"], "error": slot["error"],
                    "retry_no": slot.get("retry_no", 0),
                    "started_at": slot.get("started_at"),
                    "progress_at": slot.get("progress_at")}
             for name, slot in record["slots"].items()}
    statuses = [x["status"] for x in slots.values()]
    overall = ("COMPLETE" if all(s == "complete" for s in statuses)
               else "BLOCKED" if any(s in ("blocked", "unknown") for s in statuses)
               else "RUNNING" if any(s == "running" for s in statuses)
               else "PAUSED" if record["desired"] == "paused" else "PENDING")
    result = {"schema": "cao.team.status.v1", "team_id": record["team_id"],
              "desired": record["desired"], "status": overall,
              "tick_count": record["tick_count"], "tasks": slots}
    if record["spec"].get("initiative"):
        result["initiative"] = record["spec"]["initiative"]
        result["supersedes"] = record["spec"].get("supersedes")
    if record.get("admission"):
        proof = record["admission"]
        result["approval"] = {"method": proof.get("method"),
                              "spec_sha256": proof.get("spec_sha256"),
                              "routes_sha256": proof.get("routes_sha256")}
    recovery = record.get("recovery")
    if isinstance(recovery, Mapping) and recovery.get("decisions"):
        result["recovery"] = {
            name: {"classification": decision.get("classification"),
                   "decision": decision.get("decision"),
                   "reason": decision.get("reason"),
                   "next_action": decision.get("next_action"),
                   "fingerprint": decision.get("fingerprint"),
                   "at": decision.get("at")}
            for name, decision in recovery["decisions"].items()
            if isinstance(decision, Mapping)
        }
    return result


def _admission_config(env: Mapping[str, str] | None,
                      supplied: product_config.ProductConfig | None
                      ) -> product_config.ProductConfig | None:
    return supplied if supplied is not None else product_config.load_config(
        product_config.config_path(env))


def _effective_routes(config: product_config.ProductConfig,
                      spec: Mapping[str, Any]) -> dict[str, Any]:
    """The exact model/provider selection each dispatchable execution would use.

    The operator's chat confirmation binds these resolved values instead of the
    raw configuration document: a role launched through an agent profile never
    reads the base provider at all, so binding that document would refuse a team
    over configuration none of its agents use, while a role model or the profile
    a role selects still changes the binding.
    """
    routes: dict[str, Any] = {}
    for task in spec["tasks"]:
        entry: dict[str, Any] = {}
        for phase in ("work", "review"):
            if phase == "review" and task.get("reviewer") is None:
                continue
            resolved = _role_config(config, spec, task, phase)
            defaults = product_config.task_defaults(resolved)
            provider = resolved.provider
            entry[phase] = {
                "agent_kind": defaults.agent_kind,
                "agent_args": list(defaults.agent_args),
                "provider": (None if provider is None else {
                    "cli": provider.cli,
                    "id": provider.provider_id,
                    "model": provider.model,
                }),
            }
        routes[task["id"]] = entry
    return routes


def _new_dispatch_possible(record: Mapping[str, Any]) -> bool:
    """Whether the team could still start work it has not started yet."""

    return any(slot["status"] == "pending" for slot in record["slots"].values())


def _phase_mode(task: Mapping[str, Any], phase: str) -> str:
    """The effective task mode one phase dispatches (a review is always READ)."""

    return "read" if phase == "review" or task["mode"] == "read" else "write"


def _verify_model_policy(config: product_config.ProductConfig | None,
                         spec: Mapping[str, Any], *,
                         bound: Mapping[str, Any] | None = None) -> None:
    """Deny any NEW dispatch whose resolved route the model allowlist forbids.

    Called on plan, initialization, start and every new dispatch intent. An
    absent or unconfigured allowlist preserves existing behavior. Only routes
    that have not started are gated: an already-issued execution keeps its exact
    identity, prompt and claim and is still reconciled and released. Every
    refusal is explicit; there is no silent fallback. A caller that already
    resolved the boundary passes it in so one decision costs one resolution.
    """

    if config is None:
        return
    policy = product_config.team_model_policy(config)
    if policy is None:
        return
    try:
        bound = (bound if bound is not None
                 else _dispatchable_routes(config, spec))
    except (TeamError, product_config.ProductConfigError) as exc:
        raise TeamAdmissionError(str(exc)) from exc
    for task_id, entry in bound["allowed"].items():
        for phase, permitted in entry.items():
            if not permitted:
                # The exact route is reported with its real refusal code (a
                # model that is not listed, an allowlisted model id reached
                # through a foreign runtime identity, a read-only runtime
                # reached for WRITE, or a non-zero-data-retention provider)
                # and never with a generic claim.
                reason = product_config.model_policy_error(
                    bound["routes"][task_id][phase], policy,
                    mode=bound["modes"][task_id][phase])
                raise TeamAdmissionError(
                    f"team route {task_id}/{phase}: "
                    f"{reason or 'TEAM_MODEL_NOT_ALLOWED'}")


def _dispatchable_routes(config: product_config.ProductConfig,
                         spec: Mapping[str, Any]) -> dict[str, Any]:
    """The effective routes PLUS the operator's model-allowlist decision.

    The allowlist verdict is a separate parallel map: it never mutates the
    resolved route objects, so the confirmation digests admission binds stay
    exactly the effective selection the plan reports. ``team_cli._plan`` and
    every dispatch gate consume this one structure.
    """

    routes = _effective_routes(config, spec)
    policy = product_config.team_model_policy(config)
    allowed: dict[str, dict[str, bool]] = {}
    modes: dict[str, dict[str, str]] = {}
    tasks = {task["id"]: task for task in spec["tasks"]}
    for task_id, entry in routes.items():
        allowed[task_id] = {}
        modes[task_id] = {}
        for phase, route in entry.items():
            mode = _phase_mode(tasks[task_id], phase)
            modes[task_id][phase] = mode
            allowed[task_id][phase] = (
                product_config.model_policy_error(route, policy, mode=mode)
                is None)
    return {"routes": routes, "allowed": allowed, "modes": modes}


def _verify_admission(record: Mapping[str, Any],
                      config: product_config.ProductConfig | None, *,
                      force: bool = False) -> None:
    """Re-verify a saved confirmation before any NEW dispatch.

    Called before a pending slot starts, and on crash recovery before the
    first effect of an intent-only RUNNING slot (using force=True). A confirmation only ever gates work that has not
    started: an already-running task keeps its exact execution identity,
    prompt and claim, so a refused admission never raises out of the tick —
    it holds the pending slot with ``TEAM_ADMISSION_CHANGED`` and leaves every
    running task to be reconciled and released. An unrelated configuration
    change therefore stops the team from starting anything new instead of
    freezing what already runs. The operator's model allowlist is part of the
    same decision, so it is re-verified from the identical resolved routes.
    """
    admission = record.get("admission")
    if admission is None or (not force and not _new_dispatch_possible(record)):
        return
    if config is None:
        raise TeamAdmissionError(
            "the approved team's configuration is unavailable; refusing new dispatch"
        )
    try:
        bound = _dispatchable_routes(config, record["spec"])
    except (TeamError, product_config.ProductConfigError) as exc:
        raise TeamAdmissionError(str(exc)) from exc
    try:
        team_approval.verify_saved(
            record["spec"], config, admission, bound["routes"])
    except team_approval.ApprovalError as exc:
        raise TeamAdmissionError(str(exc)) from exc
    _verify_model_policy(config, record["spec"], bound=bound)


def _check_initiative_lineage(spec: Mapping[str, Any],
                              env: Mapping[str, str] | None) -> None:
    """One team per initiative; a successor needs a safe, explicit handoff."""
    initiative = spec.get("initiative")
    if not initiative:
        raise TeamError("operator-approved team requires an initiative")
    directory = team_dir(env)
    prior: dict[str, dict[str, Any]] = {}
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json") or not _ID.fullmatch(name[:-5]):
            continue
        record = _read(name[:-5], env)
        if record and record["spec"].get("initiative") == initiative:
            prior[record["team_id"]] = record
    previous_id = spec.get("supersedes")
    if not prior:
        if previous_id:
            raise TeamError("successor references an unknown initiative team")
        return
    successors = {r["spec"].get("supersedes") for r in prior.values()}
    terminal = [r for n, r in prior.items() if n not in successors]
    if len(terminal) != 1 or previous_id != terminal[0]["team_id"]:
        raise TeamError("initiative already has a team; approve exact predecessor")
    old = terminal[0]
    current = summary(old)
    dispatched = any(slot.get("execution_id") for slot in old["slots"].values())
    # A paused team that never dispatched anything owns no work at all, so an
    # explicitly approved successor may replace its unused manifest. The
    # successor gets its own id, admission, run, execution identities and
    # claims; the predecessor's manifest and record stay immutable on disk and
    # the same manifest is still re-approved in place rather than replaced.
    undispatched = (old["desired"] == "paused" and not dispatched
                    and current["status"] == "PAUSED")
    if (old["desired"] != "paused"
            or any(s["status"] == "running" for s in old["slots"].values())):
        raise TeamError("predecessor must be paused and terminal before replacement")
    if not undispatched and current["status"] not in ("COMPLETE", "BLOCKED"):
        raise TeamError("predecessor must be paused and terminal before replacement")
    # A blocked task can retain an execution ID after a failed receipt. Its
    # claim must be independently closed before a new team is admitted.
    db = product_config.registry_db_path(env)
    try:
        conn = registry.connect_readonly(db)
        try:
            active = conn.execute(
                "SELECT COUNT(*) FROM claims WHERE run_id=? AND released_at IS NULL",
                ("cao-team-" + previous_id,)).fetchone()[0]
        finally:
            conn.close()
    except (OSError, sqlite3.Error, registry.RegistryError) as exc:
        raise TeamError("predecessor Registry evidence unavailable") from exc
    if active:
        raise TeamError("predecessor still owns active Registry claims")
    if undispatched and _team_claim_count(env, previous_id):
        # A team that never dispatched a task must have no claim of any
        # outcome. Anything else is dispatched work wearing a cleared slot.
        raise TeamError("predecessor holds Registry claims without dispatched work")


def _team_claim_count(env: Mapping[str, str] | None, team_id: str) -> int:
    """Claims the shared Registry holds for this team's run id.

    A team that never dispatched anything holds no claim. An absent ledger
    therefore answers zero; an unreadable one fails closed rather than being
    read as zero.
    """
    path = product_config.registry_db_path(env)
    if not os.path.isfile(path):
        return 0
    try:
        conn = registry.connect_readonly(path)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM claims WHERE run_id=?",
                ("cao-team-" + team_id,),
            ).fetchone()[0]
        finally:
            conn.close()
    except (OSError, sqlite3.Error, registry.RegistryError) as exc:
        raise TeamError("team Registry evidence unavailable") from exc


def _confirm_undispatched_team(spec: Mapping[str, Any],
                               config: product_config.ProductConfig,
                               record: dict[str, Any],
                               env: Mapping[str, str] | None) -> None:
    """Attach the operator's confirmation to a team that never started work.

    A team recorded before chat approval was required is re-confirmed on its
    identical immutable manifest, so an unused manifest never forces a successor
    team. Only a team with no execution identity at all and no Registry claim
    qualifies: a team that already dispatched keeps its existing explicit
    recovery paths instead of being silently re-approved.
    """
    if any(slot.get("execution_id") for slot in record["slots"].values()):
        raise TeamError("team already dispatched work; reapproval is refused")
    if _team_claim_count(env, spec["team_id"]):
        raise TeamError("team holds Registry claims; reapproval is refused")
    _verify_model_policy(config, spec)
    try:
        record["admission"] = team_approval.confirmation(
            spec, config, _effective_routes(config, spec)
        )
    except team_approval.ApprovalError as exc:
        raise TeamAdmissionError(str(exc)) from exc
    _save(record, env)


def initialize(spec: Any, *, env: Mapping[str, str] | None = None,
               config: product_config.ProductConfig | None = None,
               approved: bool = False) -> dict[str, Any]:
    selected = validate_spec(spec)
    resolved = _admission_config(env, config)
    mode = team_approval.policy(resolved)["mode"]
    _verify_model_policy(resolved, selected)
    with dispatch_lock(env):
        old = _read(selected["team_id"], env)
        if old is not None:
            if old["spec_hash"] != fingerprint(selected):
                raise TeamError("team ID already exists with different immutable manifest")
            if (mode == "chat" and approved is True
                    and old.get("admission") is None and resolved is not None):
                _confirm_undispatched_team(selected, resolved, old, env)
                return summary(_required(selected["team_id"], env))
            return summary(old)
        admission = None
        if mode == "chat":
            if approved is not True:
                raise TeamAdmissionError("confirm the team in chat before --approved")
            _check_initiative_lineage(selected, env)
            try:
                admission = team_approval.confirmation(
                    selected, resolved, _effective_routes(resolved, selected)
                )
            except team_approval.ApprovalError as exc:
                raise TeamAdmissionError(str(exc)) from exc
        elif approved:
            raise TeamError("--approved requires chat approval mode")
        record = {
            "schema": STATE_SCHEMA, "team_id": selected["team_id"],
            "spec": selected, "spec_hash": fingerprint(selected),
            "desired": "paused", "tick_count": 0,
            "slots": {t["id"]: {"phase": "work", "round": 0, "status": "pending",
                                "execution_id": None, "prompt": None, "head": None,
                                "feedback": None, "output": None, "error": None,
                                "started_at": None, "progress_at": None,
                                "progress_key": None, "pinned_head": None,
                                "pinned_branch": None}
                      for t in selected["tasks"]},
        }
        if admission is not None:
            record["admission"] = admission
        _save(record, env)
        return summary(record)


def _required(team_id: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    value = _read(_ident(team_id), env)
    if value is None:
        raise TeamError("team does not exist; initialize it first")
    return value


def set_desired(team_id: str, desired: str, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    if desired not in ("paused", "running"):
        raise TeamError("desired must be paused or running")
    with dispatch_lock(env):
        record = _required(team_id, env)
        if desired == "running" and record["desired"] != "running":
            config = _admission_config(env, None)
            if record.get("admission") is not None:
                _verify_admission(record, config)
            else:
                # An unapproved team, or an unconfigured allowlist, must still
                # refuse a disallowed route before any new dispatch intent.
                _verify_model_policy(config, record["spec"])
                if team_approval.policy(config)["mode"] == "chat":
                    raise TeamAdmissionError(
                        "existing unapproved team cannot be newly started")
        record["desired"] = desired
        _save(record, env)
        return summary(record)


def resume(team_id: str, task_id: str, *,
           env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Explicitly retry only the original UNKNOWN task execution identity.

    Never starts a replacement agent or rewrites an issued prompt. Call only
    after independent inspection of runtime/Registry and the task checkpoint.
    """
    with dispatch_lock(env):
        record = _required(team_id, env)
        _resume_unknown_slot(record, _ident(task_id))
        _save(record, env)
        return summary(record)


def _resume_unknown_slot(record: dict[str, Any], task_id: str) -> None:
    """Flip only the original UNKNOWN execution identity back to RUNNING.

    The execution id, original prompt and any claim are all preserved; this
    never derives a new identity, so the next reconciliation resumes the same
    durable task instead of dispatching a replacement worker.
    """
    slot = record["slots"].get(task_id)
    if (slot is None or slot["status"] != "unknown"
            or not slot.get("execution_id") or not slot.get("prompt")):
        raise TeamError("only a checkpointed UNKNOWN task can be resumed")
    slot["status"] = "running"
    slot["error"] = None


def retry_read(team_id: str, task_id: str, *,
               env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Manually admit one new READ attempt after an invalid terminal receipt.

    The original agent must first be stopped through CAO's normal ownership
    gate. The original task, output and claim remain durable for inspection.
    This does not retry WRITE, reuse a stopped agent, or dispatch new work.

    A READ review attempt -- including a review of a correction round -- is
    eligible on the same terms. Such a retry still reviews the SAME accepted
    source revision, so the accepted HEAD, the work handoff and the review
    feedback are the historical evidence the new attempt reviews, while the
    failed attempt's receipt, nonce and claim stay durable on their own record
    and the new attempt gets a new execution identity and its own copy-only
    terminal nonce.
    """
    with dispatch_lock(env):
        record = _required(team_id, env)
        slot = record["slots"].get(_ident(task_id))
        task = next((t for t in record["spec"]["tasks"] if t["id"] == task_id), None)
        if (record["desired"] != "paused" or task is None or slot is None
                or slot["phase"] not in ("work", "review")
                # A work attempt may only retry on a READ task. A review is
                # dispatched as a READ execution even when its own manifest
                # task writes, and that execution's durable record, claim and
                # pane handback are verified below before anything is admitted.
                or (slot["phase"] == "work" and task["mode"] != "read")
                or slot["status"] != "unknown"
                or slot.get("error") not in (
                    "TEAM_RESULT_UNVERIFIED", "TEAM_DURABLE_RESULT_UNVERIFIED")
                or slot.get("retry_no", 0) != 0
                or not slot.get("execution_id")):
            raise TeamError("retry requires a paused UNKNOWN READ with a bad receipt")
        old_id = slot["execution_id"]
        old = product_state.read_task(old_id, env)
        claim = (old or {}).get("claim") or {}
        cp = (old or {}).get("checkpoint") or {}
        pane = cp.get("pane_id")
        if (not old or old.get("task_id") != old_id
                or old.get("run_id") != "cao-team-" + team_id
                or old.get("work_key") != old_id
                or old.get("mode") != "READ"
                or old.get("scope") != []
                or os.path.realpath(old.get("repo") or "") != os.path.realpath(task["repo"])
                or old.get("status") != "stopped"
                or type(pane) is not str or not pane
                or old.get("pane_closed") != pane
                or claim.get("status") != "RELEASED"
                or claim.get("released") is not True
                or claim.get("outcome") != "abandon"
                or type(claim.get("claim_id")) is not int):
            raise TeamError("prior READ task lacks verified stop and pane closure")
        db = product_config.registry_db_path(env)
        try:
            conn = registry.connect_readonly(db)
            try:
                row = conn.execute(
                    "SELECT run_id, work_key, owner, mode, released_at, outcome "
                    "FROM claims WHERE claim_id=?",
                    (claim["claim_id"],)).fetchone()
            finally:
                conn.close()
        except (OSError, sqlite3.Error, registry.RegistryError) as exc:
            raise TeamError("shared Registry release cannot be verified") from exc
        if (row is None or row["run_id"] != "cao-team-" + team_id
                or row["work_key"] != old_id
                or row["owner"] != product_task.OWNER
                or row["mode"] != registry.READ
                or not row["released_at"] or row["outcome"] != "abandon"):
            raise TeamError("shared Registry has no matching released READ claim")
        # Preserve the old task. New attempts get a distinct execution ID
        # and a new copy-only terminal nonce on the next dispatched prompt.
        if slot["phase"] == "review":
            # A review retry reviews the SAME accepted source revision, so the
            # accepted HEAD, the work handoff and the review feedback all stay:
            # clearing them would silently retarget the review at another HEAD.
            slot.update(status="pending", execution_id=None, prompt=None,
                        error=None, retry_no=1)
        else:
            slot.update(status="pending", execution_id=None, prompt=None, head=None,
                        feedback=None, output=None, error=None, retry_no=1)
        # The retry is a NEW execution identity: it must pin the checkout anew
        # instead of inheriting the failed attempt's revision pin.
        slot["pinned_head"] = None
        slot["pinned_branch"] = None
        _save(record, env)
        return summary(record)


def status(team_id: str, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    return summary(_required(team_id, env))


def list_teams(*, env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    directory = team_dir(env)
    if not os.path.isdir(directory):
        return []
    output = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json") or not _ID.fullmatch(name[:-5]):
            continue
        ident = name[:-5]
        try:
            output.append(status(ident, env=env))
        except Exception as exc:
            # Deliberate per-team fault containment: even a malformed object
            # or unexpected decoder bug cannot stop independent teams.
            output.append({"schema": "cao.team.status.v1",
                           "team_id": ident, "desired": "paused",
                           "status": "CORRUPT", "tasks": {},
                           "error": _error_code(getattr(exc, "code", None),
                                                type(exc).__name__)})
    return output


def _git_head(repo: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and _HEAD.fullmatch(value) else None


def _git_branch(repo: str) -> str | None:
    """The checkout's current branch name, or ``None`` when unverifiable."""

    try:
        result = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    if result.returncode != 0 or not 0 < len(value) <= 200 or "\n" in value:
        return None
    return value


def _active_writes(db: str) -> tuple[int, set[str]]:
    try:
        c = registry.connect_readonly(db)
        try:
            rows = c.execute("SELECT mode, worktree FROM claims "
                             "WHERE released_at IS NULL AND mode IN ('WRITE','EXCLUSIVE')").fetchall()
        finally:
            c.close()
    except (OSError, ValueError, sqlite3.Error, registry.RegistryError) as exc:
        raise TeamError("authoritative Registry occupancy unavailable") from exc
    # EXCLUSIVE excludes even readers and all other writers.
    if any(row["mode"] == "EXCLUSIVE" for row in rows):
        raise TeamError("EXCLUSIVE Registry authority blocks team dispatch")
    return len(rows), {os.path.realpath(r["worktree"]) for r in rows if r["worktree"]}


def _execution_id(team_id: str, task_id: str, phase: str, round_no: int,
                  retry_no: int = 0) -> str:
    # Preserve the original task identity exactly. A bounded manual READ retry
    # derives a new digest and therefore can never adopt the old agent/claim.
    fields = [team_id, task_id] if retry_no == 0 else [team_id, task_id, retry_no]
    pair = json.dumps(fields, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(pair.encode("utf-8")).hexdigest()[:20]
    return f"ct-{team_id}-{task_id}-{phase}-{round_no}-{digest}"


def _result_tag(execution_id: str) -> str:
    """Deterministic terminal receipt value *absent* from the sent prompt."""
    nonce = hashlib.sha256(
        ("cao.team.result\0" + execution_id).encode("utf-8")
    ).hexdigest()[:16]
    return nonce[::-1]


def _result_challenge(execution_id: str) -> str:
    return hashlib.sha256(
        ("cao.team.result\0" + execution_id).encode("utf-8")
    ).hexdigest()[:16]


def _receipt_for(execution_id: str, prompt: str) -> str:
    """Use copy-only nonce in new prompts; preserve legacy in-flight receipts."""
    nonce = _result_challenge(execution_id)
    if re.search(r"(?m)^CAO_TEAM_NONCE_V2: " + re.escape(nonce) + r"$", prompt):
        return "CAO_TEAM_RESULT: " + nonce
    # Durable prompts issued by earlier releases required reversing the nonce.
    # Never reinterpret a checkpointed old agent's completion contract.
    return "CAO_TEAM_RESULT: " + _result_tag(execution_id)


def _receipt_present(output: Any, receipt: str) -> bool:
    """Match one exact receipt line tolerating bounded terminal indentation.

    A terminal may render an otherwise exact standalone line indented by up
    to four spaces or tabs. The receipt token and the rest of the line must
    still match exactly, so a changed nonce, appended text, or a prompt echo
    is never accepted.
    """
    if not isinstance(output, str):
        return False
    return bool(re.search(r"(?m)^[ \t]{0,4}" + re.escape(receipt) + r"$", output))


def _bounded_tail_verified(output: Any, bounds: Any, receipt: str) -> bool:
    """Admit a bounded capture only when it provably kept the terminal proof.

    A runtime read is a scrolling window, so a long review answer can be stored
    as its terminal suffix. That suffix is admissible only when the durable
    task record itself states that it kept the tail, states exactly how much of
    the beginning it dropped, and the stored text is exactly that captured
    window -- and the task's own completion nonce is still inside it. A
    head-only capture, an unsized window or a document of unknown provenance
    therefore can never be read as a verified review, and the exact verdict and
    reviewed HEAD are still parsed out of the retained text afterwards.
    """

    if not isinstance(output, str) or not isinstance(bounds, Mapping):
        return False
    if (bounds.get("kept") != "tail"
            or type(bounds.get("limit")) is not int
            or type(bounds.get("captured_chars")) is not int
            or type(bounds.get("dropped_chars")) is not int
            or bounds["limit"] <= 0 or bounds["dropped_chars"] <= 0
            or bounds["captured_chars"] != bounds["limit"]
            or bounds["captured_chars"] != len(output)):
        return False
    return _receipt_present(output, receipt)


def _observation_admissible(entry: Any) -> bool:
    """Whether one durable observation proves a post-prompt terminal capture.

    A product task retains every bounded window of a scrolling pane with the
    prompt and terminal provenance current when it was read. Only a window this
    task's prompt was already accepted for, captured while the runtime reported
    a terminal state, is admissible; a pre-prompt, unsized or provenance-unknown
    window can never be read as terminal proof, so an arbitrary document that
    merely happens to contain a matching line is refused.
    """

    if not isinstance(entry, Mapping):
        return False
    return entry.get("prompt_ack") == "accepted" and entry.get("terminal") is True


def _window_self_consistent(text: Any, bounds: Any, receipt: str) -> bool:
    """Whether text is exactly the bounded window its own bounds claim."""

    if not isinstance(text, str) or not isinstance(bounds, Mapping):
        return False
    if bounds.get("kept") == "tail":
        return _bounded_tail_verified(text, bounds, receipt)
    return (bounds.get("kept") == "all"
            and bounds.get("dropped_chars") == 0
            and type(bounds.get("captured_chars")) is int
            and bounds["captured_chars"] == len(text)
            and _receipt_present(text, receipt))


def _verified_result_window(payload: Mapping[str, Any],
                            receipt: str) -> dict[str, Any] | None:
    """The durable bounded window that carries this task's exact receipt.

    A longer earlier read can omit the final receipt while a shorter later one
    carries it, so the primary retained window alone is not authoritative. The
    task's own bounded observations are evaluated in order; each is admitted
    only when its recorded provenance proves a post-prompt terminal capture,
    its text is exactly the window its own bounds describe, and this task's
    exact task-bound receipt line is present. A window belonging to another
    task, a wrong or echoed nonce, a truncated window of unknown provenance and
    a fabricated size are all refused. The primary window remains a fallback
    for records written before observations were retained.
    """

    observations = payload.get("output_observations")
    if isinstance(observations, list):
        for entry in observations:
            if not _observation_admissible(entry):
                continue
            text = entry.get("output")
            bounds = entry.get("bounds")
            if not _window_self_consistent(text, bounds, receipt):
                continue
            return {"output": text,
                    "truncated": bounds.get("kept") == "tail",
                    "bounds": dict(bounds)}
    text = payload.get("output")
    if _receipt_present(text, receipt):
        return {"output": text,
                "truncated": bool(payload.get("output_truncated")),
                "bounds": payload.get("output_bounds")}
    return None


def _prompt(task: Mapping[str, Any], spec: Mapping[str, Any],
            slot: Mapping[str, Any], slots: Mapping[str, Any]) -> str:
    role_id = task["reviewer"] if slot["phase"] == "review" else task["role"]
    lines = ["You are a CAO specialist. Follow repository authority and exact scope.",
             "Role: " + spec["roles"][role_id]["instruction"]]
    if slot["phase"] == "review":
        lines += ["Review the actual repository against Git HEAD " + str(slot["head"]) + ".",
                  "Your verdict is advisory, never publication or deployment authority.",
                  "Give findings and exactly one verdict line:",
                  "CAO_REVIEW_VERDICT: APPROVED or CAO_REVIEW_VERDICT: CHANGES_REQUESTED",
                  "and exactly one line CAO_REVIEWED_HEAD: " + str(slot["head"]),
                  "Previous output (untrusted):\n" + str(slot.get("output") or "")[:5000]]
    else:
        lines.append(task["prompt"])
        if slot["phase"] == "correction":
            lines.append("Address the bounded review findings and commit the scoped correction.\n"
                         + str(slot.get("feedback") or "")[:5000])
        for dep in task["depends_on"]:
            previous = slots[dep]
            lines.append("Dependency " + dep + " accepted at " + str(previous.get("head"))
                         + "; untrusted handoff:\n" + str(previous.get("output") or "")[:5000])
    ident = _execution_id(
        spec["team_id"], task["id"], slot["phase"], slot["round"],
        slot.get("retry_no", 0))
    challenge = _result_challenge(ident)
    lines.append(
        "Completion evidence: only after completing the assigned task, write "
        "one standalone final line beginning with CAO_TEAM_RESULT: followed "
        "by one space and the 16-character nonce shown below, copied EXACTLY "
        "as written, with no reversal or calculation. The completed result "
        "line is deliberately absent from this prompt, to reject prompt echo. "
        "If the task is blocked, cannot run, or needs human permission, "
        "describe the blocker and DO NOT emit a result line.\n"
        "CAO_TEAM_NONCE_V2: " + challenge
    )
    result = "\n\n".join(lines)
    if len(result) > 48000:
        raise TeamError("assembled task prompt is too long")
    return result


def _role_config(base: product_config.ProductConfig, spec: Mapping[str, Any],
                 task: Mapping[str, Any], phase: str) -> product_config.ProductConfig:
    role_id = task["reviewer"] if phase == "review" else task["role"]
    role = spec["roles"][role_id]
    raw = copy.deepcopy(base.as_dict())
    if raw.get("task") is not None and type(raw["task"]) is not dict:
        raise TeamError("product task configuration is not a mapping")
    raw["task"] = dict(raw.get("task") or {})
    if "agent_profile" in role:
        try:
            mode = "read" if phase == "review" or task["mode"] == "read" else "write"
            kind, argv, provider_id = agent_profiles.resolve_profile(
                raw.get("agent_profiles"), role["agent_profile"], mode)
        except agent_profiles.AgentProfileError as exc:
            raise TeamError(str(exc)) from exc
        if raw["task"].get("agent_args") is not None:
            raise TeamError("agent profile conflicts with default custom agent_args")
        raw["task"]["agent_kind"] = kind
        raw["task"]["agent_args"] = list(argv)
        raw["provider"] = {"cli": kind}
        if provider_id is not None:
            raw["provider"]["id"] = provider_id
    elif "model" in role:
        provider = dict(raw.get("provider") or {})
        if provider.get("cli") != "opencode":
            raise TeamError("per-role model requires configured OpenCode provider")
        if (raw.get("task") or {}).get("agent_kind") not in (None, "opencode"):
            raise TeamError("role model conflicts with configured agent kind")
        if "agent_args" in raw["task"]:
            raise TeamError("role model cannot silently override advanced agent_args")
        provider["model"] = role["model"]
        raw["provider"] = provider
    raw.setdefault("task", {})["run_id"] = "cao-team-" + spec["team_id"]
    return product_config.validate_config(raw)


def _error_code(value: Any, fallback: str) -> str:
    if type(value) is str and 0 < len(value) <= 128 and value.isprintable():
        return value
    return fallback


def _review_verdict(output: Any, expected: str) -> str | None:
    if not isinstance(output, str):
        return None
    verdicts = re.findall(r"^CAO_REVIEW_VERDICT: (APPROVED|CHANGES_REQUESTED)$", output, re.M)
    heads = re.findall(r"^CAO_REVIEWED_HEAD: ([0-9a-f]{40})$", output, re.M)
    return verdicts[0] if len(verdicts) == 1 and heads == [expected] else None


def _prepare_verified_correction(
    spec: Mapping[str, Any], task: Mapping[str, Any], slot: Mapping[str, Any],
    *, driver: Any, env: Mapping[str, str] | None, db: str,
) -> None:
    """Disposition ONLY a verified finished intermediate commit as historical.

    A task with an explicitly configured reviewer and correction budget has
    authorized a second bounded WRITE, but not permission to ignore unknown
    changes. Verify the original CAO claim, clean exact commit, scope and
    current runtime occupancy before recording a fingerprint-bound disposition.
    The ordinary Registry preflight/claim still determines whether correction
    may start; this method itself never grants a WRITE claim.
    """
    if slot["phase"] != "correction" or not task["reviewer"]:
        raise TeamError("historical continuation requires an approved correction phase")
    # A team manifest may list exactly the same write paths in a different
    # order, while the task record and the Registry row both store the
    # canonically sorted form. Lineage verification therefore compares the
    # normalized path list, so ordering can never break an exact match and a
    # genuinely different scope still fails the content comparison.
    scope = product_task.normalize_scope(task["write_scope"])
    round_no = slot["round"]
    previous_phase = "work" if round_no == 1 else "correction"
    previous_round = round_no - 1
    old_id = _execution_id(spec["team_id"], task["id"], previous_phase, previous_round)
    old = product_state.read_task(old_id, env)
    old_claim = (old or {}).get("claim") or {}
    accepted_head = slot["head"]
    prior_pane = (old or {}).get("checkpoint") or {}
    prior_pane_id = prior_pane.get("pane_id")
    if (old is None or old.get("status") != "complete"
            or old.get("mode") != registry.WRITE
            or os.path.realpath(old.get("repo") or "") != os.path.realpath(task["repo"])
            or old.get("scope") != scope
            or old_claim.get("released") is not True
            or old_claim.get("released_head") != accepted_head
            or type(old_claim.get("claim_id")) is not int
            or type(prior_pane_id) is not str or not prior_pane_id.strip()
            or old.get("pane_closed") != prior_pane_id):
        raise TeamError("prior intermediate WRITE lacks verified task and pane handback")
    if _git_head(task["repo"]) != accepted_head:
        raise TeamError("intermediate WRITE source HEAD changed")
    try:
        tree = subprocess.run(
            ["git", "-C", task["repo"], "status", "--porcelain=v1",
             "--untracked-files=all"], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TeamError("intermediate checkout state unavailable") from exc
    if tree.returncode != 0 or tree.stdout or tree.stderr:
        raise TeamError("intermediate checkout has unverified or dirty state")

    from . import product_daemon
    live = product_daemon.to_runtime_observation(
        product_daemon.collect_occupancy(driver))
    conn = registry.connect(db)
    try:
        c = conn.execute(
            "SELECT c.run_id,c.work_key,c.owner,c.mode,c.outcome,c.released_at,"
            "c.released_head,w.repo,w.paths "
            "FROM claims c JOIN works w ON c.run_id=w.run_id "
            "AND c.work_key=w.work_key AND c.issue=w.issue "
            "WHERE c.claim_id=?",
            (old_claim["claim_id"],),
        ).fetchone()
        if (c is None or c["run_id"] != "cao-team-" + spec["team_id"]
                or c["work_key"] != old_id or c["owner"] != product_task.OWNER
                or c["mode"] != registry.WRITE or c["outcome"] != "complete"
                or not c["released_at"] or c["released_head"] != accepted_head
                or c["repo"] != registry.git.normalize_repo(task["repo"])
                or c["paths"].split("\n") != scope):
            raise TeamError("intermediate claim lineage does not match exact Registry row")
        evidence = registry.gather_evidence(task["repo"], live, task["repo"])
        report = registry.reconcile(conn, evidence)
        if not report.get("ok") or not live.available:
            raise TeamError("intermediate live Registry/runtime evidence unavailable")
        matches = [
            f for f in report["findings"]
            if f["kind"] == registry.DISCOVERED_UNREGISTERED
            and os.path.realpath(f["worktree"]) == os.path.realpath(task["repo"])
        ]
        if not matches:
            # The remote may have caught up with the intermediate commit.
            # In this case ordinary Registry preflight requires no waiver.
            return
        if len(matches) != 1:
            raise TeamError("intermediate unregistered finding is ambiguous")
        finding = matches[0]
        # Bind all unmerged history to the ORIGINAL accepted team baseline.
        # If there was foreign source ahead of origin when this team began,
        # never silently call that other work historical.
        first_id = _execution_id(spec["team_id"], task["id"], "work", 0)
        first = product_state.read_task(first_id, env)
        first_claim = (first or {}).get("claim") or {}
        first_base = first_claim.get("base_head")
        if (first is None or first.get("status") != "complete"
                or type(first_base) is not str or not _HEAD.fullmatch(first_base)):
            raise TeamError("initial team work baseline is unverified")
        diff = registry.git.probe_committed_diff(
            task["repo"], str(report.get("baseline") or "")
        )
        if not diff.get("ok") or diff.get("base") != first_base:
            raise TeamError("committed history predates the first team claim")
        # Each preceding correction must start exactly at its predecessor
        # release. A foreign commit inserted between rounds breaks lineage.
        previous_release = first_claim.get("released_head")
        for index in range(1, round_no - 1):
            earlier_id = _execution_id(spec["team_id"], task["id"], "correction", index)
            earlier = product_state.read_task(earlier_id, env)
            earlier_claim = (earlier or {}).get("claim") or {}
            earlier_pane = (earlier or {}).get("checkpoint") or {}
            if (earlier is None or earlier.get("status") != "complete"
                    or earlier_claim.get("base_head") != previous_release
                    or earlier_claim.get("released") is not True
                    or type(earlier_claim.get("released_head")) is not str
                    or not _HEAD.fullmatch(earlier_claim["released_head"])
                    or earlier.get("scope") != scope
                    or type(earlier_pane.get("pane_id")) is not str
                    or not earlier_pane["pane_id"]
                    or earlier.get("pane_closed") != earlier_pane["pane_id"]):
                raise TeamError("previous team correction round has foreign lineage")
            previous_release = earlier_claim["released_head"]
        expected_claim_base = first_base if round_no == 1 else previous_release
        if (old_claim.get("base_head") != expected_claim_base
                or old_claim.get("released_head") != accepted_head):
            raise TeamError("current intermediate commit has unproven ancestry")
        changed = finding.get("committed") or []
        if (finding.get("head") != accepted_head or finding.get("live")
                or finding.get("unverifiable") or finding.get("committed_unknown")
                or finding.get("scope_unknown") or finding.get("changes")
                or finding.get("status_codes") or not changed
                or not all(
                    any(x == path or x.startswith(path.rstrip("/") + "/") for path in scope)
                    for x in changed
                )):
            raise TeamError("intermediate finding includes live, unknown or out-of-scope work")
        existing = registry.find_disposition(conn, "cao-team-" + spec["team_id"], finding)
        if existing is not None:
            if existing["disposition"] != registry.DISPOSITION_HISTORICAL:
                raise TeamError("intermediate finding has a competing disposition")
            return
        registry.record_disposition(
            conn, "cao-team-" + spec["team_id"], finding,
            registry.DISPOSITION_HISTORICAL,
            authority="cao-automated-team:" + spec["team_id"],
            rationale="automated historical acceptance of an exact released "
                      "intermediate WRITE under the team manifest correction budget: "
                      + str(old_claim["claim_id"]) + " at " + accepted_head,
            expected_fingerprint=finding["fingerprint"],
        )
    except (sqlite3.Error, json.JSONDecodeError, registry.RegistryError) as exc:
        raise TeamError("intermediate continuation evidence could not be verified") from exc
    finally:
        conn.close()


def _progress_key(backing: Any) -> str:
    """A durable fingerprint of what one task execution observably produced.

    Only durable state counts: the task's own status, its bounded captured
    output, every retained observation window, whether its CAO-owned pane was
    closed and what was last observed. A bookkeeping timestamp is deliberately
    excluded, so a tick that merely rewrites a record is never mistaken for the
    agent making progress, while newly observed evidence still is.
    """

    if not isinstance(backing, Mapping):
        return ""
    observation = backing.get("observation")
    windows = backing.get("output_observations")
    observed = (
        [entry.get("output") for entry in windows if isinstance(entry, Mapping)]
        if isinstance(windows, list) else []
    )
    return evidence.fingerprint([
        str(backing.get("status") or ""),
        backing.get("output") if isinstance(backing.get("output"), str) else "",
        observed,
        str(backing.get("pane_closed") or ""),
        (str(observation.get("status") or "")
         if isinstance(observation, Mapping) else ""),
    ])


def _worker_unreachable(driver: Any, backing: Any) -> bool:
    """Whether the durable record proves this task's worker can no longer act.

    A long authorized computation is not a stall: a worker the runtime still
    reports as present keeps its bound even while it produces no new output,
    however long model thinking takes. Only a worker that is absent, or whose
    liveness cannot be read at all, leaves the task unable to progress.
    Nothing is guessed from a name alone.
    """

    checkpoint = (backing or {}).get("checkpoint") or {}
    agent = checkpoint.get("agent_name")
    if not isinstance(agent, str) or not agent:
        return False
    if driver is None:
        return True
    try:
        driver.agent_get(agent)
    except (product_driver.DriverNotFound, product_driver.DriverError):
        return True
    return False


def _age_ms(stamp: str, now: str) -> int:
    """Milliseconds between two CAO UTC second-resolution stamps."""

    start = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    end = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ")
    return int((end - start).total_seconds() * 1000)


def _stall_reason(backing: Any, slot: Mapping[str, Any], *,
                  config: product_config.ProductConfig, driver: Any,
                  clock: Callable[[], Any] | None) -> str | None:
    """Report a team task that can no longer make observable progress.

    Nothing here releases a claim, replaces an agent or re-prompts one: the
    exact existing execution identity, prompt and claim are all retained and
    reported UNKNOWN with an actionable reason for an operator to inspect and
    resume through the existing recovery commands.
    """

    if not isinstance(backing, Mapping):
        return None
    if str(backing.get("status") or "") in _TERMINAL_TASK_STATUSES:
        return None
    progress = slot.get("progress_at") or slot.get("started_at")
    if not isinstance(progress, str) or not _STAMP.fullmatch(progress):
        return None
    if _age_ms(progress, evidence.utc_now(clock)) < product_config.team_stall_timeout_ms(
            config):
        return None
    if not _worker_unreachable(driver, backing):
        return None
    return "TEAM_TASK_STALLED"


def _recovery_claim_row(db: str, claim_id: int) -> dict[str, Any] | None:
    """The normalized shared-Registry row for one durable claim id."""

    try:
        conn = registry.connect_readonly(db)
        try:
            row = conn.execute(
                "SELECT claim_id, run_id, work_key, owner, mode, released_at, outcome "
                "FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
        finally:
            conn.close()
    except (OSError, sqlite3.Error, registry.RegistryError) as exc:
        raise TeamError("shared Registry claim evidence unavailable") from exc
    if row is None:
        return None
    return {
        "claim_id": row["claim_id"],
        "run_id": row["run_id"],
        "work_key": row["work_key"],
        "owner": row["owner"],
        "mode": row["mode"],
        "released": row["released_at"] is not None,
        "released_at": row["released_at"],
        "outcome": row["outcome"],
    }


def _foreign_recovery_write_claim(db: str, *, claim_id: int,
                                  run_id: str, work_key: str, owner: str,
                                  claim: Mapping[str, Any] | None,
                                  repo: str) -> bool:
    """Inspect individual open rows; only the exact original WRITE is self.

    A path-set subtraction would hide a second, foreign claim on the same
    checkout. This is a read-only diagnostic and never authorizes WRITE
    recovery or claim release.
    """
    try:
        conn = registry.connect_readonly(db)
        try:
            rows = conn.execute(
                "SELECT claim_id, run_id, work_key, owner, mode, branch, worktree "
                "FROM claims WHERE released_at IS NULL "
                "AND mode IN ('WRITE','EXCLUSIVE')"
            ).fetchall()
        finally:
            conn.close()
    except (OSError, ValueError, sqlite3.Error, registry.RegistryError) as exc:
        raise TeamError("authoritative Registry recovery occupancy unavailable") from exc

    checkout = os.path.realpath(repo)
    expected_branch = claim.get("base_branch") if isinstance(claim, Mapping) else None
    original_verified = (
        isinstance(claim, Mapping)
        and type(claim.get("claim_id")) is int
        and claim["claim_id"] == claim_id
        and claim.get("mode") == "WRITE"
        and isinstance(expected_branch, str)
        and bool(expected_branch)
    )
    for row in rows:
        if row["mode"] == "EXCLUSIVE":
            return True
        worktree = row["worktree"]
        if not isinstance(worktree, str) or not worktree:
            raise TeamError("open Registry writer has no verifiable checkout")
        if (original_verified
                and row["claim_id"] == claim_id
                and row["run_id"] == run_id
                and row["work_key"] == work_key
                and row["owner"] == owner
                and row["mode"] == "WRITE"
                and row["branch"] == expected_branch
                and os.path.realpath(worktree) == checkout):
            continue
        if os.path.realpath(worktree) == checkout:
            return True
    return False


def _recovery_evidence(team_id: str, task: Mapping[str, Any],
                       slot: Mapping[str, Any], *, driver: Any, db: str,
                       head_reader: Callable[[str], str | None] | None,
                       branch_reader: Callable[[str], str | None] | None,
                       occupancy: Callable[[str], tuple[int, set[str]]] | None,
                       env: Mapping[str, str] | None,
                       clock: Callable[[], Any] | None,
                       prior_resumes: int) -> dict[str, Any]:
    """Read one bounded, read-only evidence snapshot for a single team slot.

    Only durable task state, the read-only shared Registry and an optional
    runtime observation are read here. Nothing is started, prompted, claimed,
    released or written; the caller decides from the returned snapshot.
    """

    execution_id = slot.get("execution_id")
    prompt = slot.get("prompt")
    now = evidence.utc_now(clock)
    if isinstance(execution_id, str) and execution_id:
        record = product_state.read_task(execution_id, env)
    else:
        record = None
    checkpoint = (record or {}).get("checkpoint") or {}
    durable_claim = (record or {}).get("claim")
    if not isinstance(durable_claim, Mapping):
        durable_claim = None
    mode = "READ" if slot["phase"] == "review" else str(task["mode"]).upper()
    snapshot: dict[str, Any] = {
        "task_id": task["id"],
        "mode": mode,
        "phase": slot["phase"],
        "status": slot["status"],
        "error": slot.get("error"),
        "execution_id": execution_id if isinstance(execution_id, str) else None,
        "prompt_present": isinstance(prompt, str) and bool(prompt),
        "head": slot.get("head"),
        "pinned_head": slot.get("pinned_head"),
        "pinned_branch": slot.get("pinned_branch"),
        "observed_head": None,
        "observed_branch": None,
        "durable_claim": dict(durable_claim) if durable_claim else None,
        "registry_claim": None,
        "registry_unreadable": False,
        "checkout_conflict": False,
        "worker_present": False,
        "runtime_available": False,
        "agent_checkpoint": False,
        "receipt": "UNKNOWN",
        "claim_id": None,
        "prior_resumes": prior_resumes,
        "expected_owner": product_task.OWNER,
        "expected_run_id": "cao-team-" + team_id,
        "expected_work_key": execution_id if isinstance(execution_id, str) else None,
        "now": now,
    }
    agent = checkpoint.get("agent_name")
    pane = checkpoint.get("pane_id")
    snapshot["agent_checkpoint"] = bool(
        isinstance(agent, str) and agent
        and isinstance(pane, str) and pane
        and checkpoint.get("agent_started") is True
        and checkpoint.get("prompt_ack") == "accepted")
    if (isinstance(durable_claim, Mapping)
            and isinstance(durable_claim.get("claim_id"), int)
            and not isinstance(durable_claim.get("claim_id"), bool)):
        snapshot["claim_id"] = durable_claim["claim_id"]

    # Qualified runtime availability: the exact original agent is resolvable.
    if driver is not None and isinstance(agent, str) and agent:
        try:
            driver.agent_get(agent)
        except product_driver.DriverNotFound:
            snapshot["runtime_available"] = True
        except product_driver.DriverError:
            snapshot["runtime_available"] = False
        else:
            snapshot["runtime_available"] = True
            snapshot["worker_present"] = True

    # A read execution is only ever resumed against an unchanged pinned
    # revision. Without a pin the checkout cannot be proven unchanged, so the
    # snapshot honestly reports it as unverifiable and the decision holds.
    if slot.get("pinned_head") is not None or slot.get("pinned_branch") is not None:
        snapshot["observed_head"] = head_reader(task["repo"]) if head_reader else None
        snapshot["observed_branch"] = (
            branch_reader(task["repo"]) if branch_reader else None)

    # The durable claim's live row and any conflicting writer, read only when a
    # durable claim actually exists.
    if snapshot["claim_id"] is not None:
        try:
            snapshot["registry_claim"] = _recovery_claim_row(db, snapshot["claim_id"])
        except TeamError:
            snapshot["registry_unreadable"] = True
        if mode == "WRITE":
            try:
                snapshot["checkout_conflict"] = _foreign_recovery_write_claim(
                    db, claim_id=snapshot["claim_id"],
                    run_id=snapshot["expected_run_id"],
                    work_key=snapshot["expected_work_key"],
                    owner=snapshot["expected_owner"],
                    claim=durable_claim, repo=task["repo"])
            except TeamError:
                snapshot["registry_unreadable"] = True
        elif occupancy is not None:
            try:
                _, working = occupancy(db)
            except TeamError:
                snapshot["registry_unreadable"] = True
            else:
                snapshot["checkout_conflict"] = (
                    os.path.realpath(task["repo"]) in working)

    if isinstance(execution_id, str) and isinstance(prompt, str) and prompt:
        output = (record or {}).get("output")
        if _receipt_present(output, _receipt_for(execution_id, prompt)):
            snapshot["receipt"] = "VERIFIED"
        elif isinstance(output, str) and output:
            snapshot["receipt"] = "MISSING"
    return snapshot


def _recovery_state(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("recovery")
    if not isinstance(value, dict):
        value = {"schema": team_recovery.SCHEMA, "decisions": {}, "resumed": {}}
        record["recovery"] = value
    return value


def _apply_recovery(team_id: str, record: dict[str, Any], *,
                    driver: Any, db: str,
                    head_reader: Callable[[str], str | None] | None,
                    branch_reader: Callable[[str], str | None] | None,
                    occupancy: Callable[[str], tuple[int, set[str]]] | None,
                    env: Mapping[str, str] | None,
                    clock: Callable[[], Any] | None) -> list[dict[str, Any]]:
    """Classify every UNKNOWN slot and apply only a proven READ resume.

    The decision receipt is written back into the existing team checkpoint,
    idempotently: unchanged evidence keeps its original fingerprint and stamp
    instead of rewriting the record. At most one bounded resume is admitted per
    original execution identity; a WRITE or any uncertain slot only holds.
    """

    spec, slots = record["spec"], record["slots"]
    state = _recovery_state(record)
    decisions = state["decisions"]
    resumed = state["resumed"]
    report: list[dict[str, Any]] = []
    for task in spec["tasks"]:
        slot = slots[task["id"]]
        if slot["status"] != "unknown":
            continue
        execution_id = slot.get("execution_id")
        prior = 0
        if isinstance(execution_id, str):
            prior = resumed.get(execution_id)
            prior = prior if isinstance(prior, int) and not isinstance(prior, bool) else 0
        try:
            snapshot = _recovery_evidence(
                team_id, task, slot, driver=driver, db=db, head_reader=head_reader,
                branch_reader=branch_reader, occupancy=occupancy, env=env,
                clock=clock, prior_resumes=prior)
            decision = team_recovery.classify(snapshot)
        except (TeamError, state.StateError, OSError, ValueError, TypeError) as exc:
            decision = {
                "schema": team_recovery.SCHEMA, "task_id": task["id"],
                "classification": team_recovery.REGISTRY_CONFLICT,
                "decision": team_recovery.HOLD, "auto_resume": False,
                "reason": _error_code(getattr(exc, "code", None), type(exc).__name__),
                "next_action": "operator_inspection", "execution_id": execution_id,
                "provenance": {"task_id": task["id"], "timestamp": evidence.utc_now(clock)},
            }
        digest = team_recovery.fingerprint(decision)
        existing = decisions.get(task["id"])
        if (isinstance(existing, Mapping)
                and existing.get("fingerprint") == digest
                and isinstance(existing.get("at"), str)
                and all(
                    existing.get(key) == value
                    if key != "provenance" else (
                        isinstance(existing.get(key), Mapping)
                        and {k: v for k, v in existing.get(key, {}).items()
                             if k != "timestamp"}
                        == {k: v for k, v in value.items() if k != "timestamp"})
                    for key, value in decision.items())):
            # A saved receipt preserves its timestamp, not execution authority.
            # Only the capture timestamp may advance between observations; any
            # other drift means the evidence changed and the receipt restamps.
            stored = existing
        else:
            stored = dict(decision)
            stored["fingerprint"] = digest
            stored["at"] = evidence.utc_now(clock)
            decisions[task["id"]] = stored
        report.append({
            "task_id": task["id"],
            "classification": stored.get("classification"),
            "decision": stored.get("decision"),
            "auto_resume": bool(stored.get("auto_resume")),
            "reason": stored.get("reason"),
            "next_action": stored.get("next_action"),
            "execution_id": stored.get("execution_id"),
        })
        if (decision.get("auto_resume") is True
                and decision.get("decision") == team_recovery.RESUME_READ
                and (slot["phase"] == "review" or task["mode"] == "read")):
            # Only the freshly classified READ execution may be resumed.
            _resume_unknown_slot(record, task["id"])
            slot["progress_at"] = evidence.utc_now(clock)
            if isinstance(execution_id, str):
                resumed[execution_id] = prior + 1
    record["recovery"] = {
        "schema": team_recovery.SCHEMA,
        "decisions": decisions,
        "resumed": resumed,
    }
    _save(record, env)
    return report


def _execute(*, execution_id: str, task: Mapping[str, Any], prompt: str,
             phase: str, config: product_config.ProductConfig, driver: Any,
             env: Mapping[str, str] | None, wait_ms: int, registry_db: str) -> dict[str, Any]:
    from . import product_daemon
    mode = "read" if phase == "review" else task["mode"]
    scope = task["write_scope"] if mode == "write" else []
    observation = None
    if mode == "write" and product_state.read_task(execution_id, env) is None:
        observation = product_daemon.to_runtime_observation(
            product_daemon.collect_occupancy(driver))
    # Start/reconcile the existing idempotent task without asking the generic
    # runner to infer "done" before substantive model output exists.
    payload = product_task.run_task(
        driver=driver, repo=task["repo"], prompt=prompt, task_id=execution_id,
        mode=mode, write_scope=scope, config=config, env=env,
        registry_db=registry_db, observation=observation, wait_ms=0)
    if payload.get("status") not in ("running", "claimed", "complete"):
        return payload

    if payload.get("status") != "complete":
        record = product_state.read_task(execution_id, env)
        checkpoint = (record or {}).get("checkpoint") or {}
        agent = checkpoint.get("agent_name")
        if (not isinstance(agent, str) or not agent
                or checkpoint.get("prompt_ack") != "accepted"):
            return dict(payload, status="unknown",
                        error={"code": "TEAM_PROMPT_NOT_VERIFIED"})
        try:
            waited = driver.agent_wait(
                agent, until=("done", "blocked"), timeout_ms=wait_ms
            )
        except product_driver.DriverError:
            return payload  # Unknown runtime state: never grant completion.
        observed = product_driver.wait_state(waited)
        if observed == "blocked":
            return dict(payload, status="blocked",
                        error={"code": "TEAM_AGENT_BLOCKED"})
        if observed != "done":
            return payload
        try:
            output = driver.agent_read(
                agent, lines=200, source="recent-unwrapped"
            )
        except product_driver.DriverError:
            return dict(payload, status="unknown",
                        error={"code": "TEAM_RESULT_UNREADABLE"})
        receipt = _receipt_for(execution_id, prompt)
        # The expected assembled result line never appears verbatim in the
        # prompt, so its echo cannot by itself impersonate model success.
        if not _receipt_present(output, receipt):
            return dict(payload, status="unknown",
                        error={"code": "TEAM_RESULT_UNVERIFIED"})

        # The actual task runner still owns exact claim release, commit/scope
        # verification and stop/recovery. A receipt is advisory execution evidence,
        # not approval to publish or deploy.
        payload = product_task.run_task(
            driver=driver, repo=task["repo"], prompt=prompt, task_id=execution_id,
            mode=mode, write_scope=scope, config=config, env=env,
            registry_db=registry_db, observation=None, wait_ms=wait_ms)
    if payload.get("status") == "complete":
        receipt = _receipt_for(execution_id, prompt)
        window = _verified_result_window(payload, receipt)
        if window is None:
            return dict(payload, status="unknown",
                        error={"code": "TEAM_DURABLE_RESULT_UNVERIFIED"})
        # Evaluate the handoff and any review verdict against the retained
        # window that actually carries this task's receipt, not merely the
        # longest retained capture.
        if window["output"] != payload.get("output"):
            payload = dict(payload, output=window["output"],
                           output_truncated=window["truncated"],
                           output_bounds=window["bounds"])
    if mode == "read" and payload.get("status") == "complete":
        record = product_state.read_task(execution_id, env)
        if record is None:
            return dict(payload, status="unknown",
                        error={"code": "TEAM_READ_TASK_RECORD_ABSENT"})
        # Team READ workers must be stopped before the next correction/work.
        # Reuse the same verified pane/workspace close path as WRITE tasks.
        closed, error = product_task._close_owned_pane(driver, record)
        if error is not None or not closed:
            return dict(payload, status="unknown",
                        error={"code": "TEAM_READ_PANE_NOT_CLOSED"})
        product_state.update_task(execution_id, {"pane_closed": closed}, env)
    return payload


def tick(team_id: str, *, config: product_config.ProductConfig,
         driver: Any, env: Mapping[str, str] | None = None,
         registry_db: str | None = None,
         execute: Callable[..., Mapping[str, Any]] | None = None,
         writer_snapshot: Callable[[str], tuple[int, set[str]]] | None = None,
         head_reader: Callable[[str], str | None] | None = None,
         branch_reader: Callable[[str], str | None] | None = None,
         clock: Callable[[], Any] | None = None) -> dict[str, Any]:
    """Advance ready agents at most once per durable execution identity."""
    db = registry_db or product_config.registry_db_path(env)
    runner, occupancy, head = execute or _execute, writer_snapshot or _active_writes, head_reader or _git_head
    branch = branch_reader or _git_branch
    with dispatch_lock(env):
        record = _required(team_id, env)
        if record["desired"] != "running":
            return summary(record)
        spec, slots = record["spec"], record["slots"]
        chat_mode = team_approval.policy(config)["mode"] == "chat"
        admission = record.get("admission")
        freeze_legacy_pending = chat_mode and admission is None
        record["tick_count"] += 1
        changes = 0
        # R3: classify every UNKNOWN slot and apply ONLY a proven READ resume.
        # This reuses the existing team resume + product_task reconciliation
        # under this same dispatch lock, so no second scheduler or ledger is
        # introduced. A refused or uncertain slot only records a decision.
        recovery_report = _apply_recovery(
            team_id, record, driver=driver, db=db,
            head_reader=head, branch_reader=branch, occupancy=occupancy,
            env=env, clock=clock)
        # Reconcile ALL existing executions before starting any new ones.
        # Once one sibling is UNKNOWN/BLOCKED, drain the remaining workers
        # without launching more independent work.
        halted = any(s["status"] in ("blocked", "unknown") for s in slots.values())
        running = [t for t in spec["tasks"] if slots[t["id"]]["status"] == "running"]
        pending = [t for t in spec["tasks"] if slots[t["id"]]["status"] == "pending"]
        # This team's own WRITE slots, counted from dispatch so a durable
        # intent-only slot (dispatched, but with no claim yet) still consumes
        # a writer slot. max_writers bounds THIS team; foreign Registry
        # writers are honoured separately as checkout conflicts below.
        reserved_floor = sum(
            slots[t["id"]]["status"] == "running"
            and slots[t["id"]]["phase"] != "review" and t["mode"] == "write"
            for t in spec["tasks"]
        )
        # The immutable manifest and resolved routes do not change within a
        # tick. Cache one admission decision for every candidate FIRST effect;
        # reconciliation of executions that already own claims remains allowed.
        admission_cache: bool | None = None

        def first_effect_allowed() -> bool:
            nonlocal admission_cache
            if admission_cache is None:
                if freeze_legacy_pending:
                    admission_cache = False
                else:
                    try:
                        # One boundary decision per tick: an existing admission
                        # re-verifies routes AND the model allowlist, while an
                        # admission-less (legacy) team checks the allowlist
                        # explicitly so a NEW dispatch is still denied.
                        if record.get("admission") is None:
                            _verify_model_policy(config, spec)
                        else:
                            _verify_admission(record, config, force=True)
                    except TeamAdmissionError:
                        admission_cache = False
                    else:
                        admission_cache = True
            return admission_cache

        for task in running + pending:
            try:
                slot = slots[task["id"]]
                if slot["status"] in ("complete", "blocked", "unknown"):
                    continue
                if slot["status"] == "pending":
                    if freeze_legacy_pending:
                        slot["error"] = "TEAM_APPROVAL_REQUIRED"
                        _save(record, env)
                        continue
                    if halted or not all(slots[d]["status"] == "complete" for d in task["depends_on"]):
                        continue
                    if sum(s["status"] == "running" for s in slots.values()) >= spec["max_parallel"]:
                        continue
                    same_checkout = os.path.realpath(task["repo"])
                    if slot["phase"] == "review":
                        # Never inspect a moving HEAD. Existing Registry writers
                        # include other CAO teams and non-team/manual claimers.
                        if any(
                            other["id"] != task["id"] and other["mode"] == "write"
                            and os.path.realpath(other["repo"]) == same_checkout
                            and slots[other["id"]]["status"] == "running"
                            and slots[other["id"]]["phase"] != "review"
                            for other in spec["tasks"]
                        ):
                            continue
                        try:
                            _, working = occupancy(db)
                        except TeamError:
                            slot["error"] = "REGISTRY_OCCUPANCY_UNKNOWN"
                            _save(record, env)
                            continue
                        if same_checkout in working:
                            continue
                    elif task["mode"] == "write":
                        if any(
                            other["id"] != task["id"]
                            and os.path.realpath(other["repo"]) == same_checkout
                            and slots[other["id"]]["phase"] == "review"
                            and slots[other["id"]]["status"] in ("pending", "running")
                            for other in spec["tasks"]
                        ):
                            continue
                        try:
                            _, working = occupancy(db)
                        except TeamError:
                            slot["error"] = "REGISTRY_OCCUPANCY_UNKNOWN"
                            _save(record, env)
                            continue
                        # Only this team's own WRITE slots consume its cap;
                        # foreign writers remain a checkout conflict.
                        if (reserved_floor >= spec["max_writers"]
                                or same_checkout in working):
                            continue
                    # Only a genuinely dispatchable task needs a routing
                    # check. A capacity-blocked slot keeps its earlier error.
                    if not first_effect_allowed():
                        slot["error"] = (
                            "TEAM_APPROVAL_REQUIRED" if freeze_legacy_pending
                            else "TEAM_ADMISSION_CHANGED"
                        )
                        _save(record, env)
                        continue
                    if task["mode"] == "write" and slot["phase"] != "review":
                        reserved_floor += 1
                    try:
                        proposed_id = _execution_id(
                            team_id, task["id"], slot["phase"], slot["round"],
                            slot.get("retry_no", 0))
                        proposed_prompt = _prompt(task, spec, slot, slots)
                    except TeamError as exc:
                        slot["status"] = "unknown"
                        slot["error"] = _error_code(getattr(exc, "code", None), "TEAM_PROMPT_INVALID")
                        halted = True
                        _save(record, env)
                        continue
                    slot["execution_id"] = proposed_id
                    slot["prompt"] = proposed_prompt
                    slot["status"] = "running"
                    slot["error"] = None
                    # Durable liveness bookkeeping starts with the dispatch
                    # itself: the bound measures real progress, not age.
                    stamp = evidence.utc_now(clock)
                    slot["started_at"] = stamp
                    slot["progress_at"] = stamp
                    slot["progress_key"] = None
                    # Pin the exact checkout revision for a READ execution. It
                    # is the only anchor a later bounded automatic resume can
                    # prove the checkout against; a WRITE never auto-resumes.
                    if slot["phase"] == "review" or task["mode"] == "read":
                        slot["pinned_head"] = head(task["repo"])
                        slot["pinned_branch"] = branch(task["repo"])
                    else:
                        slot["pinned_head"] = None
                        slot["pinned_branch"] = None
                    _save(record, env)  # effect intent persisted before worker start
                elif execute is None:
                    # An intent-only RUNNING slot is not an agent. After a
                    # crash the task may lack its first Registry claim; check
                    # the APPROVED effective route before any new claim, pane
                    # or prompt, including READ and review tasks. A task with
                    # a saved claim always reconciles under its original ID.
                    backing = product_state.read_task(slot["execution_id"], env)
                    if not backing or not (backing.get("claim") or {}).get("claim_id"):
                        if not first_effect_allowed():
                            slot["error"] = (
                                "TEAM_APPROVAL_REQUIRED" if freeze_legacy_pending
                                else "TEAM_ADMISSION_CHANGED"
                            )
                            _save(record, env)
                            continue
                        # Restored approval clears the prior hold; it does
                        # not create a replacement execution identity.
                        if slot.get("error") in (
                            "TEAM_ADMISSION_CHANGED", "TEAM_APPROVAL_REQUIRED"
                        ):
                            slot["error"] = None
                            _save(record, env)
                        if task["mode"] == "write" and slot["phase"] != "review":
                            try:
                                _, working = occupancy(db)
                            except TeamError:
                                slot["error"] = "REGISTRY_OCCUPANCY_UNKNOWN"
                                _save(record, env)
                                continue
                            # The RUNNING slot counted itself in this team's
                            # reserved floor, but has no claim yet. Exclude
                            # only that one intent so the team cap counts the
                            # team's other WRITE slots, not foreign writers.
                            if (reserved_floor - 1 >= spec["max_writers"]
                                    or os.path.realpath(task["repo"]) in working):
                                continue
                    if slot["status"] == "running":
                        # A task whose durable evidence stopped moving and whose
                        # own worker is gone is reported, never silently
                        # re-prompted and never released. An injected runner
                        # owns its own lifecycle, so this observes only the
                        # real runtime path.
                        stalled = _stall_reason(
                            product_state.read_task(slot["execution_id"], env),
                            slot, config=config, driver=driver, clock=clock)
                        if stalled is not None:
                            slot["status"], slot["error"] = "unknown", stalled
                            halted = True
                            _save(record, env)
                            continue
                try:
                    role = _role_config(config, spec, task, slot["phase"])
                    if (execute is None and slot["phase"] == "correction"
                            and product_state.read_task(slot["execution_id"], env) is None):
                        _prepare_verified_correction(
                            spec, task, slot, driver=driver, env=env, db=db,
                        )
                    result = runner(execution_id=slot["execution_id"], task=task,
                                    prompt=slot["prompt"], phase=slot["phase"],
                                    config=role, driver=driver, env=env,
                                    wait_ms=1000, registry_db=db)
                except Exception as exc:
                    # A single team's unexpected error must never starve its
                    # other already-running workers. BaseException remains fatal.
                    # Retain the original execution ID for explicit reconciliation.
                    slot["status"] = "unknown"
                    slot["error"] = _error_code(getattr(exc, "code", None), type(exc).__name__)
                    halted = True
                    _save(record, env)
                    continue
                if not isinstance(result, Mapping):
                    slot["status"], slot["error"] = "unknown", "TASK_PAYLOAD_INVALID"
                    halted = True
                    _save(record, env)
                    continue
                outcome = result.get("status")
                if (outcome == "complete" and slot.get("execution_id")
                        and isinstance(slot.get("prompt"), str)):
                    # A longer earlier read can omit the receipt a shorter
                    # later read carries. Before deciding this result, evaluate
                    # the task's own retained observation windows so the
                    # verified capture, not merely the longest one, is used.
                    receipt = _receipt_for(slot["execution_id"], slot["prompt"])
                    if not _receipt_present(result.get("output"), receipt):
                        rescue = _verified_result_window(result, receipt)
                        if rescue is not None:
                            result = {**result, "output": rescue["output"],
                                      "output_truncated": rescue["truncated"],
                                      "output_bounds": rescue["bounds"]}
                if outcome == "complete":
                    released = (result.get("claim") or {}).get("released_head")
                    if not isinstance(released, str) or not _HEAD.fullmatch(released) or not (result.get("claim") or {}).get("released"):
                        slot["status"], slot["error"] = "unknown", "TASK_RELEASE_UNVERIFIED"
                    elif slot["phase"] == "review":
                        text = result.get("output")
                        truncated = bool(result.get("output_truncated"))
                        # A bounded capture may decide a review ONLY when it
                        # provably kept the terminal suffix carrying the task's
                        # own nonce; the exact verdict and reviewed HEAD are
                        # still parsed from the retained text, and head drift is
                        # still refused below.
                        bounded = (not truncated or _bounded_tail_verified(
                            text, result.get("output_bounds"),
                            _receipt_for(slot["execution_id"], slot["prompt"])))
                        verdict = (_review_verdict(text, slot["head"])
                                   if bounded else None)
                        if head(task["repo"]) != slot["head"]:
                            slot["status"], slot["error"] = "blocked", "REVIEW_HEAD_DRIFT"
                        elif verdict == "APPROVED":
                            slot["status"], slot["error"] = "complete", None
                        elif verdict == "CHANGES_REQUESTED":
                            if slot["round"] >= task["max_corrections"]:
                                slot["status"], slot["error"] = "blocked", "REVIEW_BUDGET_EXHAUSTED"
                            else:
                                # A review may itself be the one bounded READ
                                # retry (retry_no=1). The new correction round
                                # is a distinct identity with its own budget, so
                                # retry_no resets to 0; otherwise the stored
                                # checkpoint would fail its own validation, which
                                # only admits retry_no=1 in work/review.
                                slot["phase"], slot["round"] = "correction", slot["round"] + 1
                                slot["status"] = "pending"
                                slot["retry_no"] = 0
                                # A clipped capture keeps its findings and
                                # verdict at the very end, so the correction
                                # receives the newest 5000 characters, never the
                                # oldest part of the retained window.
                                slot["feedback"] = str(result.get("output") or "")[-5000:]
                                slot["execution_id"], slot["prompt"] = None, None
                        else:
                            slot["status"], slot["error"] = "unknown", (
                                "REVIEW_VERDICT_UNVERIFIED" if bounded
                                else "REVIEW_OUTPUT_BOUND_UNVERIFIED")
                    else:
                        slot["head"] = released
                        # The terminal end of a clipped capture holds the
                        # findings a dependent or later review actually needs.
                        slot["output"] = str(result.get("output") or "")[-5000:]
                        slot["error"] = None
                        if task["reviewer"] is not None:
                            slot["phase"], slot["status"] = "review", "pending"
                            slot["execution_id"], slot["prompt"] = None, None
                        else:
                            slot["status"] = "complete"
                    changes += 1
                elif outcome in ("blocked", "refused"):
                    error = result.get("error")
                    code = error.get("code") if isinstance(error, Mapping) else None
                    slot["status"], slot["error"] = "blocked", _error_code(code, "TASK_REFUSED")
                elif outcome not in ("running", "claimed"):
                    error = result.get("error")
                    code = error.get("code") if isinstance(error, Mapping) else None
                    slot["status"], slot["error"] = "unknown", _error_code(code, "TASK_STATUS_UNVERIFIED")
                if slot["status"] == "running":
                    # Record only observable progress, so the liveness bound
                    # above can tell a long authorized computation from a task
                    # whose durable evidence has stopped moving.
                    key = _progress_key(
                        product_state.read_task(slot["execution_id"], env))
                    if key != slot.get("progress_key"):
                        slot["progress_key"] = key
                        slot["progress_at"] = evidence.utc_now(clock)
                if slot["status"] in ("blocked", "unknown"):
                    halted = True
                _save(record, env)
            except Exception as exc:
                # Fault isolation covers every task operation, including
                # malformed provider payloads, capacity probes and source
                # probes outside the narrower expected-error handler.
                slot = slots[task["id"]]
                slot["status"] = "unknown"
                slot["error"] = _error_code(getattr(exc, "code", None),
                                             type(exc).__name__)
                halted = True
                _save(record, env)
        return dict(summary(record), advanced=changes, recovery=recovery_report)


def tick_all(*, config: product_config.ProductConfig, driver: Any,
             env: Mapping[str, str] | None = None,
             registry_db: str | None = None,
             clock: Callable[[], Any] | None = None) -> list[dict[str, Any]]:
    """Daemon adapter; never creates teams or starts a paused team."""
    output = []
    for info in list_teams(env=env):
        if info["status"] == "CORRUPT":
            output.append(info)
            continue
        active = any(s["status"] == "running" for s in info["tasks"].values())
        # A running team that holds an UNKNOWN slot is still ticked so R3 can
        # classify it; a fully BLOCKED team without any UNKNOWN slot cannot
        # progress and is left untouched.
        recoverable = any(s["status"] == "unknown" for s in info["tasks"].values())
        if info["desired"] != "running" or (
                not active and not recoverable
                and info["status"] in ("COMPLETE", "BLOCKED")):
            continue
        try:
            output.append(tick(info["team_id"], config=config, driver=driver,
                               env=env, registry_db=registry_db, clock=clock))
        except Exception as exc:
            # One unhandled team failure cannot prevent another team from
            # reconciling its already-running tasks and releasing its claims.
            output.append({
                "schema": "cao.team.status.v1", "team_id": info["team_id"],
                "status": "UNKNOWN", "desired": info["desired"],
                "error": _error_code(getattr(exc, "code", None), type(exc).__name__),
                "tasks": info["tasks"],
            })
    return output
