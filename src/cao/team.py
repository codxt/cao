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
from typing import Any

from . import agent_profiles, product_config, product_driver, product_state, product_task, registry, state, team_approval

SPEC_SCHEMA = "cao.team.v1"
STATE_SCHEMA = "cao.team.state.v1"
_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
_HEAD = re.compile(r"[0-9a-f]{40}\Z")
MAX_TASKS = 64
MAX_WRITERS = 2
MAX_PARALLEL = 8


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
    for task in spec["tasks"]:
        slot = slots[task["id"]]
        if type(slot) is not dict:
            raise TeamError("malformed team task checkpoint")
        phase, current = slot.get("phase"), slot.get("status")
        round_no = slot.get("round")
        retry_no = slot.get("retry_no", 0)
        if (not expected_fields.issubset(slot)
                or set(slot) - expected_fields - {"retry_no"}
                or type(retry_no) is not int or not 0 <= retry_no <= 1
                or (retry_no and (task["mode"] != "read" or phase != "work"))
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
    return record


def _save(record: dict[str, Any], env: Mapping[str, str] | None) -> None:
    state.write_state(team_path(record["team_id"], env), record)


def summary(record: Mapping[str, Any]) -> dict[str, Any]:
    slots = {name: {"status": slot["status"], "phase": slot["phase"],
                    "round": slot["round"], "execution_id": slot["execution_id"],
                    "head": slot["head"], "error": slot["error"],
                    "retry_no": slot.get("retry_no", 0)}
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
    return result


def _admission_config(env: Mapping[str, str] | None,
                      supplied: product_config.ProductConfig | None
                      ) -> product_config.ProductConfig | None:
    return supplied if supplied is not None else product_config.load_config(
        product_config.config_path(env))


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
    if (old["desired"] != "paused" or current["status"] not in ("COMPLETE", "BLOCKED")
            or any(s["status"] == "running" for s in old["slots"].values())):
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


def initialize(spec: Any, *, env: Mapping[str, str] | None = None,
               config: product_config.ProductConfig | None = None,
               approved: bool = False) -> dict[str, Any]:
    selected = validate_spec(spec)
    resolved = _admission_config(env, config)
    mode = team_approval.policy(resolved)["mode"]
    with dispatch_lock(env):
        old = _read(selected["team_id"], env)
        if old is not None:
            if old["spec_hash"] != fingerprint(selected):
                raise TeamError("team ID already exists with different immutable manifest")
            return summary(old)
        admission = None
        if mode == "chat":
            if approved is not True:
                raise TeamAdmissionError("confirm the team in chat before --approved")
            _check_initiative_lineage(selected, env)
            try:
                admission = team_approval.confirmation(selected, resolved)
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
                                "feedback": None, "output": None, "error": None}
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
                try:
                    team_approval.verify_saved(record["spec"], config, record["admission"])
                except team_approval.ApprovalError as exc:
                    raise TeamAdmissionError(str(exc)) from exc
            elif team_approval.policy(config)["mode"] == "chat":
                raise TeamAdmissionError("existing unapproved team cannot be newly started")
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
        slot = record["slots"].get(_ident(task_id))
        if (slot is None or slot["status"] != "unknown"
                or not slot.get("execution_id") or not slot.get("prompt")):
            raise TeamError("only a checkpointed UNKNOWN task can be resumed")
        slot["status"] = "running"
        slot["error"] = None
        _save(record, env)
        return summary(record)


def retry_read(team_id: str, task_id: str, *,
               env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Manually admit one new READ attempt after an invalid terminal receipt.

    The original agent must first be stopped through CAO's normal ownership
    gate. The original task, output and claim remain durable for inspection.
    This does not retry WRITE, reuse a stopped agent, or dispatch new work.
    """
    with dispatch_lock(env):
        record = _required(team_id, env)
        slot = record["slots"].get(_ident(task_id))
        task = next((t for t in record["spec"]["tasks"] if t["id"] == task_id), None)
        if (record["desired"] != "paused" or task is None or slot is None
                or task["mode"] != "read" or slot["phase"] != "work"
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
        slot.update(status="pending", execution_id=None, prompt=None, head=None,
                    feedback=None, output=None, error=None, retry_no=1)
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
            or old.get("scope") != task["write_scope"]
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
                or c["paths"].split("\n") != task["write_scope"]):
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
                    or earlier.get("scope") != task["write_scope"]
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
        scope = task["write_scope"]
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
        text = payload.get("output")
        if not _receipt_present(text, receipt):
            return dict(payload, status="unknown",
                        error={"code": "TEAM_DURABLE_RESULT_UNVERIFIED"})
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
         head_reader: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    """Advance ready agents at most once per durable execution identity."""
    db = registry_db or product_config.registry_db_path(env)
    runner, occupancy, head = execute or _execute, writer_snapshot or _active_writes, head_reader or _git_head
    with dispatch_lock(env):
        record = _required(team_id, env)
        if record["desired"] != "running":
            return summary(record)
        spec, slots = record["spec"], record["slots"]
        chat_mode = team_approval.policy(config)["mode"] == "chat"
        admission = record.get("admission")
        if admission is not None:
            try:
                team_approval.verify_saved(spec, config, admission)
            except team_approval.ApprovalError as exc:
                raise TeamAdmissionError(str(exc)) from exc
        freeze_legacy_pending = chat_mode and admission is None
        record["tick_count"] += 1
        changes = 0
        # Reconcile ALL existing executions before starting any new ones.
        # Once one sibling is UNKNOWN/BLOCKED, drain the remaining workers
        # without launching more independent work.
        halted = any(s["status"] in ("blocked", "unknown") for s in slots.values())
        running = [t for t in spec["tasks"] if slots[t["id"]]["status"] == "running"]
        pending = [t for t in spec["tasks"] if slots[t["id"]]["status"] == "pending"]
        # An injected occupancy reader can be stale between consecutive starts.
        reserved_floor = sum(
            slots[t["id"]]["status"] == "running"
            and slots[t["id"]]["phase"] != "review" and t["mode"] == "write"
            for t in spec["tasks"]
        )
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
                            n, working = occupancy(db)
                        except TeamError:
                            slot["error"] = "REGISTRY_OCCUPANCY_UNKNOWN"
                            _save(record, env)
                            continue
                        n = max(n, reserved_floor)
                        reserved_floor = n
                        if n >= spec["max_writers"] or same_checkout in working:
                            continue
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
                    _save(record, env)  # effect intent persisted before worker start
                elif (execute is None and task["mode"] == "write"
                      and slot["phase"] != "review"):
                    # Crash window: checkpoint says RUNNING, but the product task
                    # may not yet own a claim. Re-admit before that first effect.
                    backing = product_state.read_task(slot["execution_id"], env)
                    if not backing or not (backing.get("claim") or {}).get("claim_id"):
                        try:
                            n, working = occupancy(db)
                        except TeamError:
                            slot["error"] = "REGISTRY_OCCUPANCY_UNKNOWN"
                            _save(record, env)
                            continue
                        if n >= spec["max_writers"] or os.path.realpath(task["repo"]) in working:
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
                if outcome == "complete":
                    released = (result.get("claim") or {}).get("released_head")
                    if not isinstance(released, str) or not _HEAD.fullmatch(released) or not (result.get("claim") or {}).get("released"):
                        slot["status"], slot["error"] = "unknown", "TASK_RELEASE_UNVERIFIED"
                    elif slot["phase"] == "review":
                        verdict = (None if result.get("output_truncated")
                                   else _review_verdict(result.get("output"), slot["head"]))
                        if head(task["repo"]) != slot["head"]:
                            slot["status"], slot["error"] = "blocked", "REVIEW_HEAD_DRIFT"
                        elif verdict == "APPROVED":
                            slot["status"], slot["error"] = "complete", None
                        elif verdict == "CHANGES_REQUESTED":
                            if slot["round"] >= task["max_corrections"]:
                                slot["status"], slot["error"] = "blocked", "REVIEW_BUDGET_EXHAUSTED"
                            else:
                                slot["phase"], slot["round"] = "correction", slot["round"] + 1
                                slot["status"] = "pending"
                                slot["feedback"] = str(result.get("output") or "")[:5000]
                                slot["execution_id"], slot["prompt"] = None, None
                        else:
                            slot["status"], slot["error"] = "unknown", "REVIEW_VERDICT_UNVERIFIED"
                    else:
                        slot["head"] = released
                        slot["output"] = str(result.get("output") or "")[:5000]
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
        return dict(summary(record), advanced=changes)


def tick_all(*, config: product_config.ProductConfig, driver: Any,
             env: Mapping[str, str] | None = None,
             registry_db: str | None = None) -> list[dict[str, Any]]:
    """Daemon adapter; never creates teams or starts a paused team."""
    output = []
    for info in list_teams(env=env):
        if info["status"] == "CORRUPT":
            output.append(info)
            continue
        active = any(s["status"] == "running" for s in info["tasks"].values())
        if info["desired"] != "running" or (
                not active and info["status"] in ("COMPLETE", "BLOCKED")):
            continue
        try:
            output.append(tick(info["team_id"], config=config, driver=driver,
                               env=env, registry_db=registry_db))
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
