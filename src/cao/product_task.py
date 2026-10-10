"""Task lifecycle for the CAO product.

A product task ties three durable things together:

* the **claim book** -- the Work Registry ledger is the sole authority for who
  may act. A review task claims ``READ``; a code task requires the user to
  declare ``--write <path>...`` explicitly and then claims ``WRITE``. The
  ledger's own gate decides conflicts and is never overridden here.
* the **task record** -- durable state under ``product_state`` holding the
  task's checkpoint, so a re-run with the same id resumes instead of
  duplicating work.
* the **runtime work** -- a CAO-owned workspace/pane and one started agent,
  driven only through the injected :class:`~cao.product_driver.RuntimeDriver`.

Fail-closed rules this module enforces:

* a checkpoint is written *before* every external effect, so a crash between
  checkpoint and effect resumes without repeating the effect;
* a prompt is submitted at most once. Once a submission has been issued the
  checkpoint is durable; a failed or ambiguous acknowledgement leaves the task
  ``unknown`` and it is never re-prompted automatically -- ``status``/``logs``
  surface it for an operator;
* an unreadable or malformed task record raises; it is never treated as absent;
* ``complete`` handback releases the claim with the observed repository HEAD;
  stop/abandon releases with outcome ``abandon`` and closes only CAO's own
  pane in the CAO-owned workspace.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Callable

from . import evidence, git, product_config, product_driver, product_state, registry

__all__ = [
    "OWNER",
    "TASK_SCHEMA",
    "LOGS_SCHEMA",
    "TaskError",
    "TaskUsageError",
    "TaskUnavailable",
    "TaskAmbiguous",
    "normalize_scope",
    "idempotency_key",
    "task_id_for",
    "task_payload",
    "run_task",
    "read_status",
    "read_logs",
    "stop_task",
]

OWNER = "cao-product"
TASK_SCHEMA = "cao.product.task.v0"
LOGS_SCHEMA = "cao.product.logs.v0"

_TERMINAL_STATUSES = ("complete", "stopped")


class TaskError(Exception):
    """Base class for task-lifecycle failures."""

    code = "PRODUCT_TASK_ERROR"


class TaskUsageError(TaskError):
    """The caller's request is malformed; nothing was attempted."""

    code = "PRODUCT_TASK_USAGE"


class TaskUnavailable(TaskError):
    """A prerequisite (configuration, runtime, task) is not available."""

    code = "PRODUCT_TASK_UNAVAILABLE"


class TaskAmbiguous(TaskError):
    """A recovery scan found more than one matching object; refusing to guess."""

    code = "PRODUCT_TASK_AMBIGUOUS"


# --------------------------------------------------------------------------
# Identity and scope
# --------------------------------------------------------------------------


def _normalize_repo(repo: Any) -> str:
    if not isinstance(repo, str) or not repo.strip():
        raise TaskUsageError("repo must be a non-empty path")
    real = os.path.realpath(os.path.expanduser(repo.strip()))
    if not os.path.isdir(real):
        raise TaskUsageError("repo is not a directory")
    return real


def normalize_scope(scope: Any) -> list[str]:
    """Repository-relative write paths, validated by the registry's own rule."""

    try:
        return registry.normalize_paths(list(scope or ()))
    except registry.RegistryError as exc:
        raise TaskUsageError(str(exc)) from None


def idempotency_key(repo: Any, prompt: Any, scope: Any = ()) -> str:
    """A deterministic key over the exact (repo, prompt, scope) of a task."""

    payload = {
        "repo": _normalize_repo(repo),
        "prompt": str(prompt),
        "scope": sorted(scope or ()),
    }
    return evidence.fingerprint(payload)


def task_id_for(repo: Any, prompt: Any, scope: Any = ()) -> str:
    digest = idempotency_key(repo, prompt, scope).split(":", 1)[1]
    return "t" + digest[:20]


def _agent_name(task_id: str) -> str:
    base = "".join(ch for ch in task_id.lower() if ch.isalnum() or ch in "-_")
    if not base or not base[0].isalpha():
        base = "t" + base
    return ("cao" + base)[:32]


def _repo_head_and_branch(repo: str) -> tuple[str | None, str | None]:
    """The checkout's exact HEAD commit and branch, or ``(None, None)``.

    Unreadable git state is ``UNKNOWN``; it is never rounded up to a commit id.
    """

    probe = git.probe_worktrees(repo)
    if not probe.get("ok"):
        return None, None
    real = os.path.realpath(os.path.expanduser(repo))
    for tree in probe.get("items", ()):
        if tree.get("path") != real:
            continue
        head = tree.get("head")
        if isinstance(head, str) and git.HEAD_RE.fullmatch(head):
            branch = tree.get("branch")
            return head, (branch if isinstance(branch, str) else "")
    return None, None


# --------------------------------------------------------------------------
# Record helpers
# --------------------------------------------------------------------------


def _bounded(text: str, limit: int) -> tuple[str, bool]:
    if not isinstance(text, str):
        return "", False
    if limit > 0 and len(text) > limit:
        return text[:limit], True
    return text, False


def _new_record(
    task_id: str,
    run_id: str,
    repo: str,
    prompt: str,
    mode: str,
    scope: list[str],
    defaults: product_config.TaskDefaults,
    clock: Callable[[], Any] | None,
) -> dict[str, Any]:
    stamp = evidence.utc_now(clock)
    return {
        "schema": product_state.TASK_SCHEMA,
        "task_id": task_id,
        "run_id": run_id,
        "work_key": task_id,
        "repo": repo,
        "prompt": prompt,
        "mode": mode,
        "scope": list(scope),
        "agent_kind": defaults.agent_kind,
        "agent_args": list(defaults.agent_args),
        "workspace_label": defaults.workspace_label_prefix + task_id,
        "status": "pending",
        "checkpoint": {
            "workspace_id": None,
            "tab_id": None,
            "pane_id": None,
            "agent_name": None,
            "agent_started": False,
            "start_attempted": False,
            "prompt_issued": False,
            "prompt_ack": "none",
            "stage": None,
        },
        "created_at": stamp,
        "updated_at": stamp,
        "claim": None,
        "output": None,
        "output_truncated": False,
        "observation": None,
        "error": None,
        "refusal": None,
    }


def _save(
    record: dict[str, Any],
    env: Mapping[str, str] | None,
    clock: Callable[[], Any] | None,
) -> dict[str, Any]:
    record["updated_at"] = evidence.utc_now(clock)
    return product_state.write_task(record, env)


def _checkpoint(
    record: dict[str, Any],
    checkpoint: Mapping[str, Any],
    env: Mapping[str, str] | None,
    clock: Callable[[], Any] | None,
) -> dict[str, Any]:
    record["checkpoint"] = dict(checkpoint)
    return _save(record, env, clock)


def _write_service(
    observed: str,
    env: Mapping[str, str] | None,
    clock: Callable[[], Any] | None,
) -> None:
    product_state.write_service(
        {
            "schema": product_state.SERVICE_SCHEMA,
            "desired": observed,
            "observed": observed,
            "updated_at": evidence.utc_now(clock),
        },
        env,
    )


def task_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    """Public, structured view of a task record."""

    checkpoint = record.get("checkpoint") or {}
    status = str(record.get("status") or "pending")
    workspace_id = checkpoint.get("workspace_id")
    agent_name = checkpoint.get("agent_name")
    return {
        "schema": TASK_SCHEMA,
        "ok": status not in ("unknown", "refused", "unavailable", "blocked"),
        "task_id": record.get("task_id"),
        "status": status,
        "repo": record.get("repo"),
        "mode": record.get("mode"),
        "scope": list(record.get("scope") or []),
        "run_id": record.get("run_id"),
        "prompt_state": checkpoint.get("prompt_ack", "none"),
        "claim": record.get("claim"),
        "workspace": (
            {"id": workspace_id, "label": record.get("workspace_label")}
            if workspace_id
            else None
        ),
        "pane_id": checkpoint.get("pane_id"),
        "agent": (
            {
                "name": agent_name,
                "kind": record.get("agent_kind"),
                "started": bool(checkpoint.get("agent_started")),
            }
            if agent_name
            else None
        ),
        "observation": record.get("observation"),
        "output": record.get("output"),
        "output_truncated": bool(record.get("output_truncated")),
        "pane_closed": record.get("pane_closed"),
        "error": record.get("error"),
        "refusal": record.get("refusal"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


# --------------------------------------------------------------------------
# Authority
# --------------------------------------------------------------------------


def _claim_authority(
    conn: Any,
    record: dict[str, Any],
    observation: registry.RuntimeObservation | None,
) -> dict[str, Any]:
    """Propose and claim the task's scope through the registry's own gate."""

    repo = record["repo"]
    scope = list(record.get("scope") or [])
    if record["mode"] == registry.WRITE:
        # Bind the terminal result to the exact original source state. A
        # resumed claim must never treat another commit as its new baseline.
        base_head, base_branch = _repo_head_and_branch(repo)
        if not base_head or not base_branch:
            return {
                "ok": False,
                "stage": "preflight",
                "result": {
                    "status": registry.NEEDS_RECONCILIATION,
                    "errors": ["WRITE checkout HEAD or branch could not be verified"],
                    "conflicts": [],
                },
            }
        proposed = registry.propose(
            conn,
            record["run_id"],
            record["work_key"],
            repo,
            paths=scope,
            objective=str(record.get("prompt") or "")[:200],
        )
        spec = registry.ClaimSpec(
            run_id=record["run_id"],
            work_key=record["work_key"],
            owner=OWNER,
            mode=registry.WRITE,
            issue="",
            branch=base_branch,
            worktree=repo,
        )
    else:
        proposed = registry.propose(
            conn,
            record["run_id"],
            record["work_key"],
            repo,
            components=["review"],
            objective=str(record.get("prompt") or "")[:200],
        )
        spec = registry.ClaimSpec(
            run_id=record["run_id"],
            work_key=record["work_key"],
            owner=OWNER,
            mode=registry.READ,
            issue="",
        )
    if not proposed.get("ok"):
        return {"ok": False, "stage": "propose", "result": proposed}
    preflight = registry.preflight(conn, spec, observation)
    if not preflight.get("ok"):
        return {"ok": False, "stage": "preflight", "result": preflight}
    claimed = registry.claim(conn, spec, observation)
    if not claimed.get("ok"):
        return {"ok": False, "stage": "claim", "result": claimed}
    if record["mode"] == registry.WRITE and claimed.get("status") != registry.CLAIMED:
        # A preexisting ledger claim without this task's saved base cannot
        # establish which commits belong to it. Preserve that claim and stop.
        return {
            "ok": False,
            "stage": "claim",
            "result": {
                "status": registry.NEEDS_RECONCILIATION,
                "errors": ["preexisting WRITE claim lacks a saved task baseline"],
                "conflicts": [],
            },
        }
    return {
        "ok": True,
        "claim_id": claimed.get("claim_id"),
        "status": claimed.get("status"),
        "claim_mode": record["mode"],
        "base_head": base_head if record["mode"] == registry.WRITE else None,
        "base_branch": base_branch if record["mode"] == registry.WRITE else None,
    }


# --------------------------------------------------------------------------
# Provision / start / prompt / observe
# --------------------------------------------------------------------------


def _ensure_provisioned(
    driver: product_driver.RuntimeDriver,
    record: dict[str, Any],
    env: Mapping[str, str] | None,
    clock: Callable[[], Any] | None,
) -> dict[str, Any]:
    checkpoint = dict(record.get("checkpoint") or {})
    if checkpoint.get("workspace_id") and checkpoint.get("pane_id"):
        return checkpoint

    label = record["workspace_label"]
    matches = [w for w in driver.workspace_list() if w.get("label") == label]
    if len(matches) > 1:
        raise TaskAmbiguous(f"{len(matches)} runtime workspaces share label {label!r}")
    if len(matches) == 1:
        workspace = matches[0]
        panes = driver.pane_list(workspace["workspace_id"])
        pane_id = next((p["pane_id"] for p in panes if p.get("pane_id")), None)
        if pane_id is None:
            raise TaskUnavailable("the recovered CAO workspace has no pane")
        checkpoint.update(
            workspace_id=workspace["workspace_id"],
            tab_id=workspace.get("active_tab_id"),
            pane_id=pane_id,
        )
        _checkpoint(record, checkpoint, env, clock)
        return checkpoint

    # Checkpoint the intent before the effect so a crash resumes without a
    # duplicate workspace; the recovery scan above adopts a half-created one.
    _checkpoint(record, dict(checkpoint, stage="provision"), env, clock)
    created = driver.workspace_create(label, cwd=record["repo"])
    checkpoint.update(
        workspace_id=created["workspace_id"],
        tab_id=created["tab_id"],
        pane_id=created["pane_id"],
        stage="provisioned",
    )
    _checkpoint(record, checkpoint, env, clock)
    return checkpoint


def _ensure_started(
    driver: product_driver.RuntimeDriver,
    record: dict[str, Any],
    env: Mapping[str, str] | None,
    clock: Callable[[], Any] | None,
    defaults: product_config.TaskDefaults,
) -> dict[str, Any]:
    checkpoint = dict(record.get("checkpoint") or {})
    if checkpoint.get("agent_started"):
        return checkpoint
    name = checkpoint.get("agent_name") or _agent_name(record["task_id"])
    if checkpoint.get("start_attempted"):
        # A previous attempt may have started the agent without confirming it.
        # Only an explicit not-found makes a second start safe; any other
        # reading stays unknown rather than risking a duplicate.
        try:
            driver.agent_get(name)
            checkpoint.update(agent_name=name, agent_started=True)
            _checkpoint(record, checkpoint, env, clock)
            return checkpoint
        except product_driver.DriverNotFound:
            pass

    _checkpoint(
        record,
        dict(checkpoint, agent_name=name, start_attempted=True, stage="start"),
        env,
        clock,
    )
    started = driver.agent_start(
        name,
        record["agent_kind"],
        checkpoint["pane_id"],
        argv=tuple(record.get("agent_args") or ()),
        timeout_ms=defaults.start_timeout_ms,
    )
    checkpoint.update(
        agent_name=name,
        agent_started=True,
        stage="started",
        agent_pane_id=started.get("agent_pane_id"),
    )
    _checkpoint(record, checkpoint, env, clock)
    return checkpoint


def _prompt_once(
    driver: product_driver.RuntimeDriver,
    record: dict[str, Any],
    env: Mapping[str, str] | None,
    clock: Callable[[], Any] | None,
    defaults: product_config.TaskDefaults,
) -> dict[str, Any]:
    checkpoint = dict(record.get("checkpoint") or {})
    if checkpoint.get("prompt_issued"):
        return checkpoint

    # The submission checkpoint is durable BEFORE the send. Once written, no
    # run -- however it ends -- submits the prompt again.
    checkpoint.update(prompt_issued=True, prompt_ack="pending", stage="prompt")
    _checkpoint(record, checkpoint, env, clock)
    try:
        driver.agent_prompt(
            checkpoint["agent_name"],
            record["prompt"],
            timeout_ms=defaults.prompt_timeout_ms,
        )
    except product_driver.DriverError as exc:
        checkpoint.update(prompt_ack="ambiguous", prompt_error=getattr(exc, "code", None))
        _checkpoint(record, checkpoint, env, clock)
        return checkpoint
    checkpoint.update(prompt_ack="accepted", prompt_error=None, stage="prompted")
    _checkpoint(record, checkpoint, env, clock)
    return checkpoint


def _observe(
    driver: product_driver.RuntimeDriver,
    record: dict[str, Any],
    env: Mapping[str, str] | None,
    clock: Callable[[], Any] | None,
    defaults: product_config.TaskDefaults,
) -> None:
    checkpoint = record.get("checkpoint") or {}
    agent_name = checkpoint.get("agent_name")
    if not agent_name:
        return
    try:
        text = driver.agent_read(
            agent_name, lines=defaults.read_lines, source="recent"
        )
    except product_driver.DriverError as exc:
        record["observation"] = {"status": "UNKNOWN", "reason": getattr(exc, "code", None)}
        return
    bounded, truncated = _bounded(text, defaults.max_output_chars)
    record["output"] = bounded
    record["output_truncated"] = truncated
    record["observation"] = {
        "status": "OBSERVED",
        "captured_at": evidence.utc_now(clock),
    }


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def run_task(
    *,
    driver: product_driver.RuntimeDriver,
    repo: Any,
    prompt: Any,
    task_id: str | None = None,
    mode: str | None = None,
    write_scope: Any = (),
    config: product_config.ProductConfig | None = None,
    env: Mapping[str, str] | None = None,
    registry_db: str | None = None,
    observation: registry.RuntimeObservation | None = None,
    clock: Callable[[], Any] | None = None,
    wait_ms: int | None = None,
) -> dict[str, Any]:
    """Run (or idempotently resume) one product task and return its record view."""

    repo = _normalize_repo(repo)
    if not isinstance(prompt, str) or not prompt.strip():
        raise TaskUsageError("a task prompt is required")
    prompt = prompt.strip()

    requested = (mode or "").strip().lower() if isinstance(mode, str) else ""
    scope = normalize_scope(write_scope)
    if scope:
        effective_mode = registry.WRITE
    elif requested == "write":
        raise TaskUsageError(
            "a WRITE task must declare at least one --write path; no scope was given"
        )
    else:
        effective_mode = registry.READ

    defaults = product_config.task_defaults(config, env)
    if defaults.agent_kind is None:
        raise TaskUnavailable(
            "no agent kind is configured; set a provider CLI or task.agent_kind"
        )
    run_id = defaults.run_id
    task_id = task_id or task_id_for(repo, prompt, scope)

    record = product_state.read_task(task_id, env)
    if record is None:
        record = _new_record(
            task_id, run_id, repo, prompt, effective_mode, scope, defaults, clock
        )
        _save(record, env, clock)

    _validate_resume(record, repo, prompt, effective_mode, scope)

    if record["status"] in _TERMINAL_STATUSES:
        return task_payload(record)

    conn = registry.connect(db=registry_db or product_config.registry_db_path(env))
    try:
        prior_claim = record.get("claim") or {}
        if prior_claim.get("claim_id"):
            # Once a task has a recorded claim, retrying must never create a
            # replacement claim for its existing agent/prompt. Match the same
            # OPEN row by owner and exact claim id before any runtime action.
            held = registry.get_open_claim(
                conn, record["run_id"], record["work_key"], "", OWNER,
            )
            if (prior_claim.get("released") or held is None or
                    held["claim_id"] != prior_claim["claim_id"] or
                    held["mode"] != record["mode"]):
                record["status"] = (
                    "running" if record.get("status") == "running" else "unknown"
                )
                record["error"] = {
                    "code": "TASK_CLAIM_RELEASE_REJECTED",
                    "message": "the task's original claim is no longer open; refusing to re-claim it",
                }
                _save(record, env, clock)
                return task_payload(record)
            # Exact owner and claim id have already been verified against the
            # live OPEN ledger row. There is nothing to acquire again on a
            # resume: a second preflight against newly committed work would
            # mistake the original writer for an overlapping unregistered one.
            authority = {
                "ok": True,
                "claim_id": held["claim_id"],
                "claim_mode": held["mode"],
                "status": registry.RESUMED,
            }
        else:
            authority = _claim_authority(conn, record, observation)
        if not authority.get("ok"):
            record["status"] = "refused"
            record["refusal"] = {
                "stage": authority.get("stage"),
                "status": (authority.get("result") or {}).get("status"),
                "errors": (authority.get("result") or {}).get("errors", []),
                "conflicts": (authority.get("result") or {}).get("conflicts", []),
            }
            _save(record, env, clock)
            return task_payload(record)

        record["claim"] = {
            "claim_id": authority.get("claim_id"),
            "status": authority.get("status"),
            "mode": authority.get("claim_mode"),
            **(
                {
                    "base_head": (
                        prior_claim.get("base_head") if prior_claim
                        else authority.get("base_head")
                    ),
                    "base_branch": (
                        prior_claim.get("base_branch") if prior_claim
                        else authority.get("base_branch")
                    ),
                }
                if record["mode"] == registry.WRITE else {}
            ),
        }
        record["status"] = "claimed"
        record["refusal"] = None
        record.pop("error", None)
        _save(record, env, clock)
        _write_service("running", env, clock)

        try:
            checkpoint = _ensure_provisioned(driver, record, env, clock)
            checkpoint = _ensure_started(driver, record, env, clock, defaults)
            checkpoint = _prompt_once(driver, record, env, clock, defaults)
        except (TaskUnavailable, TaskAmbiguous):
            raise
        except product_driver.DriverError as exc:
            record["status"] = "unknown"
            record["error"] = {
                "code": getattr(exc, "code", "RUNTIME_DRIVER_ERROR"),
                "message": "the runtime command failed",
            }
            _save(record, env, clock)
            return task_payload(record)

        if checkpoint.get("prompt_ack") != "accepted":
            record["status"] = "unknown"
            record["error"] = {
                "code": "PROMPT_ACKNOWLEDGEMENT_AMBIGUOUS",
                "message": (
                    "the prompt was issued but the acknowledgement is not "
                    "confirmed; the task holds UNKNOWN and is never re-prompted"
                ),
            }
            _save(record, env, clock)
            return task_payload(record)

        record["status"] = "running"
        _save(record, env, clock)
        return _finish(conn, driver, record, env, clock, defaults, wait_ms)
    finally:
        conn.close()


def _validate_resume(
    record: Mapping[str, Any],
    repo: str,
    prompt: str,
    mode: str,
    scope: list[str],
) -> None:
    mismatches = []
    if record.get("repo") != repo:
        mismatches.append("repo")
    if record.get("prompt") != prompt:
        mismatches.append("prompt")
    if record.get("mode") != mode:
        mismatches.append("mode")
    if list(record.get("scope") or []) != list(scope):
        mismatches.append("scope")
    if mismatches:
        raise TaskUsageError(
            "task id already exists with different "
            + ", ".join(mismatches)
            + "; choose a different --id or drop it"
        )


def _write_completion_error(
    conn: Any, record: Mapping[str, Any], head: str, branch: str | None
) -> tuple[str, str] | None:
    """Verify an accepted WRITE uses exactly its original claim and Git scope.

    This is a completion predicate, not a new authority. Live observations are
    read-only, and any unreadable source retains the existing WRITE claim.
    """
    claim = record.get("claim") or {}
    base = claim.get("base_head")
    base_branch = claim.get("base_branch")
    if (not isinstance(base, str) or not git.HEAD_RE.fullmatch(base)
            or not isinstance(base_branch, str) or not base_branch):
        return ("TASK_WRITE_RESULT_UNVERIFIED", "WRITE claim baseline missing")

    held = registry.get_open_claim(
        conn, record["run_id"], record["work_key"], "", OWNER,
    )
    work = registry.get_work(conn, record["run_id"], record["work_key"], "")
    scope = record.get("scope")
    if (held is None or work is None or held["claim_id"] != claim.get("claim_id")
            or held["mode"] != registry.WRITE
            or held["worktree"] != record["repo"]
            or held["branch"] != base_branch
            or not isinstance(scope, list)
            or not scope
            or sorted(scope) != sorted(work["paths"].split("\n"))):
        return ("TASK_WRITE_SCOPE_VIOLATION", "WRITE claim identity or scope changed")
    if branch != base_branch:
        return ("TASK_WRITE_SCOPE_VIOLATION", "WRITE checkout branch changed")

    committed = git.probe_committed_diff(record["repo"], base)
    status = git.probe_status(record["repo"])
    if (not committed.get("ok") or committed.get("base") != base
            or not status.get("ok")
            or not isinstance(committed.get("items"), list)
            or not isinstance(status.get("items"), list)):
        return ("TASK_WRITE_RESULT_UNVERIFIED", "WRITE Git evidence unavailable")
    changed = committed["items"]
    dirty = git.touched_paths(status["items"])
    if any(
        not any(path == prefix or path.startswith(prefix + "/") for prefix in scope)
        for path in changed + dirty
    ):
        return ("TASK_WRITE_SCOPE_VIOLATION", "WRITE modified an undeclared Git path")
    if dirty or not changed or head == base:
        return ("TASK_WRITE_RESULT_UNVERIFIED", "WRITE has no clean committed result")
    # A changing HEAD between probes cannot be accepted as proof of the head
    # that will be recorded in the Registry release receipt.
    final_head, final_branch = _repo_head_and_branch(record["repo"])
    if final_head != head or final_branch != branch:
        return ("TASK_WRITE_RESULT_UNVERIFIED", "WRITE Git evidence changed during review")
    return None


def _finish(
    conn: Any,
    driver: product_driver.RuntimeDriver,
    record: dict[str, Any],
    env: Mapping[str, str] | None,
    clock: Callable[[], Any] | None,
    defaults: product_config.TaskDefaults,
    wait_ms: int | None,
) -> dict[str, Any]:
    _observe(driver, record, env, clock, defaults)
    _save(record, env, clock)

    effective_wait = defaults.wait_timeout_ms if wait_ms is None else wait_ms
    if isinstance(effective_wait, bool) or not isinstance(effective_wait, int) or effective_wait < 0:
        raise TaskUsageError("wait_ms must be a non-negative integer")

    state: str | None = None
    if effective_wait > 0:
        checkpoint = record.get("checkpoint") or {}
        try:
            result = driver.agent_wait(
                checkpoint["agent_name"],
                until=("idle", "done", "blocked"),
                timeout_ms=effective_wait,
            )
            state = product_driver.wait_state(result)
        except product_driver.DriverError as exc:
            record["observation"] = {
                "status": "UNKNOWN",
                "reason": getattr(exc, "code", None),
            }
            state = None

    if state in ("idle", "done"):
        head, branch = _repo_head_and_branch(record["repo"])
        if head:
            if record["mode"] == registry.WRITE:
                failure = _write_completion_error(conn, record, head, branch)
                if failure is not None:
                    record["status"] = "blocked"
                    record["error"] = {
                        "code": failure[0],
                        "message": failure[1],
                    }
                    _save(record, env, clock)
                    return task_payload(record)
            _observe(driver, record, env, clock, defaults)
            released = registry.release(
                conn,
                record["run_id"],
                OWNER,
                record["work_key"],
                issue="",
                outcome="complete",
                released_head=head,
            )
            if not released.get("ok"):
                # A rejected release leaves ownership unresolved. Reporting
                # completion would make the open claim disappear from the UI.
                record["status"] = "running"
                record["error"] = {
                    "code": "TASK_CLAIM_RELEASE_REJECTED",
                    "message": "the registry refused claim release; the task is not complete",
                    "detail": list(released.get("errors") or []),
                }
            else:
                record["claim"] = dict(
                    record.get("claim") or {},
                    outcome="complete",
                    released=True,
                    released_head=head,
                )
                record.pop("error", None)
                record["status"] = "complete"
        else:
            record["status"] = "running"
            record["error"] = {
                "code": "TASK_HEAD_UNREADABLE",
                "message": (
                    "the agent finished but the repository HEAD could not be "
                    "read; the claim is left open rather than reported complete"
                ),
            }
    elif state == "blocked":
        record["status"] = "blocked"
    else:
        record["status"] = "running"
    _save(record, env, clock)
    return task_payload(record)


def read_status(
    task_id: str | None = None, *, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    record = (
        product_state.read_task(task_id, env)
        if task_id
        else product_state.latest_task(env)
    )
    if record is None:
        return {
            "schema": TASK_SCHEMA,
            "ok": False,
            "status": "NONE",
            "task_id": task_id,
            "reason": "no product task has been recorded",
        }
    return task_payload(record)


def read_logs(
    task_id: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    record = (
        product_state.read_task(task_id, env)
        if task_id
        else product_state.latest_task(env)
    )
    if record is None:
        return {
            "schema": LOGS_SCHEMA,
            "ok": False,
            "status": "NONE",
            "task_id": task_id,
            "output": None,
            "reason": "no product task has been recorded",
        }
    output = record.get("output") or ""
    truncated = bool(record.get("output_truncated"))
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise TaskUsageError("limit must be a non-negative integer")
        if len(output) > limit:
            output = output[-limit:] if limit else ""
            truncated = True
    return {
        "schema": LOGS_SCHEMA,
        "ok": True,
        "task_id": record.get("task_id"),
        "status": record.get("status"),
        "mode": record.get("mode"),
        "workspace": (record.get("checkpoint") or {}).get("workspace_id"),
        "pane_id": (record.get("checkpoint") or {}).get("pane_id"),
        "output": output,
        "truncated": truncated,
        "updated_at": record.get("updated_at"),
    }


def stop_task(
    *,
    driver: product_driver.RuntimeDriver | None = None,
    task_id: str | None = None,
    env: Mapping[str, str] | None = None,
    registry_db: str | None = None,
    clock: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """End the CAO task: release the claim, close only CAO's own pane."""

    record = (
        product_state.read_task(task_id, env)
        if task_id
        else product_state.latest_task(env)
    )
    if record is None:
        raise TaskUnavailable("no product task exists to stop")
    if record["status"] in _TERMINAL_STATUSES:
        return task_payload(record)

    # Establish ownership before touching a pane. An old task record alone is
    # not authority to close an agent or to represent a released claim.
    claim = record.get("claim") or {}
    prior_released = claim.get("released") is True and claim.get("outcome") == "abandon"
    conn = registry.connect(db=registry_db or product_config.registry_db_path(env))
    try:
        if prior_released:
            row = conn.execute(
                "SELECT outcome, released_at FROM claims "
                "WHERE claim_id=? AND run_id=? AND work_key=? AND issue=? AND owner=?",
                (claim.get("claim_id"), record["run_id"], record["work_key"], "", OWNER),
            ).fetchone()
            ownership_ok = bool(
                row is not None and row["released_at"] and row["outcome"] == "abandon"
            )
        else:
            held = registry.get_open_claim(
                conn, record["run_id"], record["work_key"], "", OWNER,
            )
            ownership_ok = bool(
                held is not None and held["claim_id"] == claim.get("claim_id")
            )
    finally:
        conn.close()

    if not ownership_ok:
        # A missing, released, or replaced claim cannot authorize pane closure.
        # In particular, never close a pane after an out-of-band release.
        record["error"] = {
            "code": "TASK_CLAIM_RELEASE_REJECTED",
            "message": "the task's original claim cannot be verified; pane left untouched",
        }
        _save(record, env, clock)
        return task_payload(record)

    checkpoint = dict(record.get("checkpoint") or {})
    pane_id = checkpoint.get("pane_id")
    closed = None
    close_error = (
        "runtime driver unavailable; cannot verify task-owned pane closure"
        if pane_id and driver is None else None
    )
    if driver is not None and pane_id:
        try:
            pane = driver.pane_get(pane_id)
        except product_driver.DriverError as exc:
            close_error = str(exc)
        else:
            if isinstance(pane, Mapping) and pane.get("workspace_id") == checkpoint.get(
                "workspace_id"
            ):
                try:
                    driver.pane_close(pane_id)
                    closed = pane_id
                except product_driver.DriverError as exc:
                    close_error = str(exc)
            # Unknown or foreign ownership: never close that pane. Preserve
            # the existing skip behavior for a pane not owned by this task.

    if close_error is not None:
        # IMPORTANT: claim release comes AFTER confirmed pane closure.
        # A still-running writer must retain its Registry ownership.
        record["error"] = {
            "code": "TASK_PANE_CLOSE_FAILED",
            "message": "the task-owned pane could not be closed; claim retained",
            "detail": close_error,
        }
        _save(record, env, clock)
        return task_payload(record)

    if not prior_released:
        conn = registry.connect(db=registry_db or product_config.registry_db_path(env))
        try:
            released = registry.release(
                conn, record["run_id"], OWNER, record["work_key"],
                issue="", outcome="abandon",
            )
        finally:
            conn.close()
        if not released.get("ok"):
            # Closing a pane does not establish Registry release. Keep the
            # nonterminal state and avoid a counterfeit stopped service.
            record["pane_closed"] = closed
            record["error"] = {
                "code": "TASK_CLAIM_RELEASE_REJECTED",
                "message": "the registry refused claim release; task not marked stopped",
                "detail": list(released.get("errors") or []),
            }
            _save(record, env, clock)
            return task_payload(record)

    record["claim"] = dict(
        claim, status="RELEASED", outcome="abandon", released=True,
    )
    record["pane_closed"] = closed
    record.pop("error", None)
    record["status"] = "stopped"
    _save(record, env, clock)
    _write_service("stopped", env, clock)
    return task_payload(record)
