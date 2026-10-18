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
* a bounded output capture keeps the terminal suffix of a runtime read and
  states how much of its beginning was dropped, so the completion receipt is
  never the part an operator loses, and a later read that captured less never
  erases the one already captured;
* every bounded observation of the task's own pane is retained with its
  timestamp and prompt provenance, so a longer earlier read that missed the
  receipt cannot displace a shorter later read that carries it -- the durable
  record keeps both, and the completion receipt is evaluated against each;
* a verified terminal settlement is made durable *before* the first pane-close
  attempt, so a crash during closure never loses the completion receipt or
  strands the still-open claim; a later run reconciles only its own recorded
  settlement, and only once its exact owned pane is verifiably gone;
* ``complete`` handback releases the claim with the observed repository HEAD;
  stop/abandon releases with outcome ``abandon`` and closes only CAO's own
  pane in the CAO-owned workspace.
"""

from __future__ import annotations

import hashlib
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
#: How many of a task's own bounded pane observations are retained. A longer
#: earlier read that missed the receipt and a shorter later one that carries it
#: must both survive; the longest window is always preserved.
_MAX_OBSERVATIONS = 4


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
    name = "cao" + base
    if len(name) <= 32:
        return name
    # The team task ID contains its unambiguous digest at the *end*. Pure
    # truncation collapses many sibling tasks to the same agent identity.
    # Preserve a readable prefix and a task-ID-bound suffix instead.
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]
    return name[:19] + "-" + digest


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


def _bounded(text: str, limit: int) -> tuple[str, dict[str, Any]]:
    """Bound one runtime read by keeping its terminal suffix.

    An agent writes its completion receipt last, so a bounded capture retains
    the end of the read and states exactly how much of the beginning was
    dropped. Keeping the head instead silently discards the receipt that proves
    the work finished, which is the one part an operator must be able to read.
    """

    if not isinstance(text, str):
        return "", {"kept": "none", "limit": limit, "captured_chars": 0,
                    "dropped_chars": 0}
    if limit > 0 and len(text) > limit:
        return (
            text[-limit:],
            {
                "kept": "tail",
                "limit": limit,
                "captured_chars": limit,
                "dropped_chars": len(text) - limit,
            },
        )
    return text, {"kept": "all", "limit": limit, "captured_chars": len(text),
                  "dropped_chars": 0}


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
        "output_bounds": None,
        "output_observations": [],
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
        "output_bounds": record.get("output_bounds"),
        "output_observations": list(record.get("output_observations") or []),
        "pane_closed": record.get("pane_closed"),
        "error": record.get("error"),
        "refusal": record.get("refusal"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


# --------------------------------------------------------------------------
# Authority
# --------------------------------------------------------------------------


def _recover_acquired_claim(conn: Any, record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Adopt an OPEN claim this task already acquired but never recorded.

    A crash between the durable Registry claim and the product record save
    leaves the ledger holding ownership this task can no longer see. Adoption
    is only ever of *this attempt's own* row, and only when the intent
    persisted before the claim still proves it: exact run, work key, owner,
    mode, repository, registered scope, branch and worktree, plus -- for WRITE
    -- a checkout still sitting exactly on the recorded precrash baseline with
    no dirty, drifted or unverifiable evidence. A precrash baseline that cannot
    be proven is refused with a precise blocker rather than re-derived from the
    current checkout, because a post-acquisition commit would then look like
    this claim's own baseline. Nothing here creates a second claim, releases a
    row the task cannot justify, or leaves the operator a silent stall.
    """

    intent = record.get("claim_intent")
    if not isinstance(intent, Mapping):
        # No durable intent means no provable precrash baseline. Fall through to
        # the Registry's own gate, which refuses an unattributable preexisting
        # WRITE claim instead of adopting it.
        return None
    held = registry.get_open_claim(
        conn, record["run_id"], record["work_key"], "", OWNER)
    if held is None:
        return None
    work = registry.get_work(conn, record["run_id"], record["work_key"], "")
    scope = list(record.get("scope") or [])
    problems = []
    if (intent.get("run_id") != record["run_id"]
            or intent.get("work_key") != record["work_key"]
            or intent.get("repo") != record["repo"]
            or intent.get("mode") != record["mode"]
            or intent.get("scope") != scope):
        problems.append("the recorded claim intent is not this task's own")
    if work is None or work["repo"] != git.normalize_repo(record["repo"]):
        problems.append("the preexisting claim's repository is not this checkout")
    elif list(work["paths"].split("\n") if work["paths"] else []) != scope:
        problems.append("the preexisting claim's registered scope is not this task's scope")
    if held["mode"] != record["mode"]:
        problems.append("the preexisting claim's mode is not this task's mode")
    elif held["claimed_at"] < record["created_at"]:
        problems.append("the preexisting claim predates this task record")
    base_head = base_branch = None
    if record["mode"] == registry.WRITE:
        base_head, base_branch = _repo_head_and_branch(record["repo"])
        if (held["branch"] != base_branch or held["worktree"] != record["repo"]
                or base_head != intent.get("base_head")
                or base_branch != intent.get("base_branch")):
            problems.append(
                "the WRITE checkout no longer matches the claim's recorded "
                "branch, worktree or precrash baseline")
        elif not base_head or not base_branch:
            problems.append("the WRITE checkout HEAD or branch could not be verified")
        else:
            # An unclean checkout means uncommitted work whose author cannot be
            # proven. Refuse rather than attribute it to this claim.
            status = git.probe_status(record["repo"])
            if not status.get("ok") or not isinstance(status.get("items"), list):
                problems.append("the WRITE checkout state could not be verified")
            elif status["items"]:
                problems.append("the WRITE checkout is dirty after claim acquisition")
    elif held["branch"] or held["worktree"]:
        problems.append("the preexisting READ claim carries write identity")
    if problems:
        return {
            "ok": False,
            "stage": "claim",
            "result": {
                "status": registry.NEEDS_RECONCILIATION,
                "errors": ["recovery of the acquired claim was refused: "
                           + "; ".join(problems)],
                "conflicts": [],
            },
        }
    return {
        "ok": True,
        "claim_id": held["claim_id"],
        "status": registry.RESUMED,
        "claim_mode": record["mode"],
        "base_head": base_head,
        "base_branch": base_branch,
    }


def _claim_authority(
    conn: Any,
    record: dict[str, Any],
    observation: registry.RuntimeObservation | None,
    env: Mapping[str, str] | None,
    clock: Callable[[], Any] | None,
) -> dict[str, Any]:
    """Propose and claim the task's scope through the registry's own gate.

    The acquisition intent is persisted *before* the durable Registry claim. It
    is the only precrash baseline a crash inside that window can later be
    recovered against, so the baseline is recorded once, here, and never
    re-derived from the checkout after the claim exists.
    """

    repo = record["repo"]
    scope = list(record.get("scope") or [])
    base_head = base_branch = None
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
        record["claim_intent"] = {
            "run_id": record["run_id"],
            "work_key": record["work_key"],
            "repo": repo,
            "mode": record["mode"],
            "scope": list(scope),
            "base_head": base_head,
            "base_branch": base_branch,
            "recorded_at": evidence.utc_now(clock),
        }
        _save(record, env, clock)

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


def _remember_observation(record: dict[str, Any], entry: dict[str, Any]) -> None:
    """Retain a bounded set of this task's own observations of its pane.

    A later read that captured less must never erase a completion receipt an
    earlier read already proved, and the converse is just as important: a
    longer earlier read that missed the receipt must not displace a shorter
    later one that carries it. Both are kept, each with its own timestamp and
    prompt provenance, so the durable record preserves every bounded window
    that could prove completion. Entries are de-duplicated by their exact
    captured text, the total is bounded, and the longest window is always
    retained so an operator's completion receipt is never the part lost.
    """

    entries = [
        existing
        for existing in (record.get("output_observations") or [])
        if isinstance(existing, Mapping) and existing.get("output") != entry["output"]
    ]
    entries.append(entry)
    if len(entries) > _MAX_OBSERVATIONS:
        longest = max(
            range(len(entries)),
            key=lambda index: len(str(entries[index].get("output") or "")),
        )
        drop = next(index for index in range(len(entries)) if index != longest)
        del entries[drop]
    record["output_observations"] = entries


def _observe(
    driver: product_driver.RuntimeDriver,
    record: dict[str, Any],
    env: Mapping[str, str] | None,
    clock: Callable[[], Any] | None,
    defaults: product_config.TaskDefaults,
    *,
    terminal: bool = False,
) -> dict[str, Any] | None:
    checkpoint = record.get("checkpoint") or {}
    agent_name = checkpoint.get("agent_name")
    if not agent_name:
        return None
    try:
        text = driver.agent_read(
            agent_name, lines=defaults.read_lines, source="recent-unwrapped"
        )
    except product_driver.DriverError as exc:
        record["observation"] = {"status": "UNKNOWN", "reason": getattr(exc, "code", None)}
        return None
    stamp = evidence.utc_now(clock)
    bounded, bounds = _bounded(text, defaults.max_output_chars)
    # Every bounded read of this task's own pane is retained with the prompt
    # provenance and terminal state that were current when it was captured, so
    # a shorter later read carrying the receipt is never discarded in favour of
    # a longer earlier read that missed it. The recorded entry is returned so a
    # caller that provisionally marked a candidate capture terminal and then
    # refused it can withdraw that provenance instead of leaving a claim it
    # cannot support.
    entry = {
        "output": bounded,
        "bounds": dict(bounds),
        "observed_at": stamp,
        "post_prompt": checkpoint.get("prompt_issued") is True,
        "prompt_ack": checkpoint.get("prompt_ack"),
        "stage": checkpoint.get("stage"),
        "terminal": bool(terminal),
    }
    _remember_observation(record, entry)
    stored = record.get("output")
    if isinstance(stored, str) and stored and len(bounded) < len(stored):
        # Each read is a bounded window of a scrolling pane. A later read that
        # captured less must not erase a completion receipt an earlier read
        # already proved, so the longer capture is retained as the primary
        # window and the newer, shorter read is recorded as not stored. The
        # shorter read is still kept among the observations above, so a receipt
        # it uniquely contains remains available to the receipt evaluator.
        record["output_bounds"] = dict(
            record.get("output_bounds") or {},
            superseded_read_chars=len(text),
            observed_at=stamp,
        )
        record["observation"] = {"status": "OBSERVED", "captured_at": stamp}
        return entry
    record["output"] = bounded
    record["output_truncated"] = bounds["kept"] == "tail"
    record["output_bounds"] = dict(bounds, observed_at=stamp)
    record["observation"] = {"status": "OBSERVED", "captured_at": stamp}
    return entry


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def _model_policy_refusal(
    config: product_config.ProductConfig | None,
    defaults: product_config.TaskDefaults,
    mode: Any = None,
) -> str | None:
    """The dispatch-policy refusal reason for this task's NEW dispatch route.

    The route is read only from the configured provider entry and this task's
    own verified argv, never from an untrusted prompt. The OpenCode
    ``mini``-harness qualification is unconditional, so an absent allowlist
    never permits a bare or implicit OpenCode launch; a configured allowlist
    additionally denies an unresolved or unlisted selection with no silent
    fallback. The resolved agent kind and provider CLI are supplied so an
    allowlisted OpenCode model id also binds the actual OpenCode runtime
    identity, and ``mode`` carries this task's declared READ/WRITE mode so a
    read-only runtime reached for a WRITE task — or a non-zero-data-retention
    provider at all — is refused.
    """

    policy = product_config.team_model_policy(config)
    provider = config.provider if config is not None else None
    return product_config.model_policy_error(
        {"agent_kind": defaults.agent_kind,
         "agent_args": list(defaults.agent_args),
         "provider": {"cli": provider.cli if provider is not None else None,
                      "model": (provider.model
                                if provider is not None else None)}},
        policy, mode=mode)


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
    verify: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
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

    # A configured operator-local model allowlist denies a direct NEW agent
    # start whose effective route is unresolvable or unlisted. The gate runs
    # BEFORE any Registry claim, workspace or agent effect: a refusal must
    # leave zero claim rows and no runtime effect. It reads only the
    # configured provider and this task's own verified argv; an already
    # started agent, a recorded prompt or an existing claim is never re-gated,
    # so the original claim/agent resume below is preserved exactly. A record
    # that holds a durable precrash claim intent is deferred to the recovery
    # path below, which adopts the exact original claim without re-gating.
    checkpoint = record.get("checkpoint") or {}
    if (not (record.get("claim") or {}).get("claim_id")
            and record.get("claim_intent") is None
            and not checkpoint.get("agent_started")
            and not checkpoint.get("prompt_issued")):
        reason = _model_policy_refusal(
            config, defaults, mode=effective_mode)
        if reason is not None:
            record["status"] = "refused"
            record["refusal"] = {
                "stage": "model_policy",
                "status": "REFUSED",
                "errors": [reason],
                "conflicts": [],
            }
            _save(record, env, clock)
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
            # A crash between the durable Registry claim and this record's
            # save leaves an OPEN row this task must recognize as its own.
            authority = _recover_acquired_claim(conn, record)
            if authority is None:
                # The only remaining NEW acquisition path (a durable intent
                # whose row never materialized, or a wholly fresh intent that
                # reached the ledger) is gated by the identical route decision
                # immediately before any claim row can be created, so a
                # refusal still leaves zero claim rows.
                checkpoint = record.get("checkpoint") or {}
                if (not checkpoint.get("agent_started")
                        and not checkpoint.get("prompt_issued")):
                    reason = _model_policy_refusal(
                        config, defaults, mode=effective_mode)
                    if reason is not None:
                        record["status"] = "refused"
                        record["refusal"] = {
                            "stage": "model_policy",
                            "status": "REFUSED",
                            "errors": [reason],
                            "conflicts": [],
                        }
                        _save(record, env, clock)
                        return task_payload(record)
                authority = _claim_authority(conn, record, observation, env, clock)
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
        return _finish(conn, driver, record, env, clock, defaults, wait_ms, verify)
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


def _owned_pane_gone(driver, record: Mapping[str, Any]) -> bool:
    """Whether the task's exact owned pane is verifiably absent right now.

    Only the checkpoint's own workspace/pane identity is probed, and only a
    runtime ``not found`` answer counts. An unreadable runtime, a missing pane
    identity or any other error is not absence, so a caller must never treat it
    as proven closure.
    """

    checkpoint = record.get("checkpoint") or {}
    pane_id = checkpoint.get("pane_id")
    if not isinstance(pane_id, str) or not pane_id or driver is None:
        return False
    try:
        driver.pane_get(pane_id)
    except product_driver.DriverNotFound:
        return True
    except product_driver.DriverError:
        return False
    return False


def _own_settled_disposition(record: Mapping[str, Any]) -> bool:
    """Whether the record carries this task's own durable verified settlement.

    The disposition is written only after a terminal observation passed the
    completion gate (or an ordinary settled DONE), and records which claim,
    repository, run, work key and owned pane it settled. A later run may
    reconcile an interrupted settlement only when every one of those still
    matches this record and a durable terminal observation is still present, so
    a foreign, stale or tampered disposition is refused rather than released.
    """

    settled = record.get("terminal_result")
    if not isinstance(settled, Mapping):
        return False
    claim = record.get("claim") or {}
    checkpoint = record.get("checkpoint") or {}
    proof = any(
        isinstance(entry, Mapping) and entry.get("terminal") is True
        for entry in record.get("output_observations") or []
    )
    return (
        proof
        and settled.get("claim_id") is not None
        and settled.get("claim_id") == claim.get("claim_id")
        and settled.get("mode") == record.get("mode")
        and settled.get("repo") == record.get("repo")
        and settled.get("run_id") == record.get("run_id")
        and settled.get("work_key") == record.get("work_key")
        and settled.get("pane_id") == checkpoint.get("pane_id")
        and settled.get("agent_name") == checkpoint.get("agent_name")
    )


def _named_agent_ownership_error(
    driver,
    checkpoint: Mapping[str, Any],
    pane: Mapping[str, Any],
    pane_id: str,
    workspace_id: str,
) -> str | None:
    """Prove the pane's occupant is exactly this task's own named agent.

    The runtime reports the worker KIND on a pane (a type such as a CLI name),
    never this task's unique agent name, so the pane's ``agent`` field is never
    compared against the unique name. When the pane carries an occupant, that
    occupant is proved to be this task's own worker only by an exact
    ``agent.get`` on the checkpoint's unique name reporting the same pane and
    workspace; the kind fields are compared only to each other. An *empty* pane
    this task already owned is safe to close -- there is no process to mistake
    -- but a pane occupied by a worker this task cannot resolve as its own
    never is.
    """

    agent_name = checkpoint.get("agent_name")
    pane_kind = pane.get("agent")
    occupied = isinstance(pane_kind, str) and bool(pane_kind)
    if not isinstance(agent_name, str) or not agent_name:
        if occupied:
            return "an unidentified worker occupies this task's pane"
        return None
    try:
        agent = driver.agent_get(agent_name)
    except product_driver.DriverNotFound:
        # This task's named agent has exited. Its now-empty pane is safe to
        # close; a pane still showing an occupant it cannot own is not.
        if occupied:
            return "a worker occupies a pane this task's named agent does not own"
        return None
    except product_driver.DriverError as exc:
        return "the task's named agent could not be read: " + str(exc)
    if not isinstance(agent, Mapping):
        return "the task's named agent identity is unreadable"
    reported_name = agent.get("name")
    if not isinstance(reported_name, str) or reported_name != agent_name:
        return "the named agent identity does not match this task"
    if (agent.get("pane_id") != pane_id
            or agent.get("workspace_id") != workspace_id):
        return "the named agent occupies a different pane or workspace"
    agent_kind = agent.get("agent")
    if (occupied and isinstance(agent_kind, str) and agent_kind
            and pane_kind != agent_kind):
        return "the pane's worker kind does not match the named agent"
    return None


def _close_owned_pane(driver, record):
    """Confirm exact task-owned pane closure before releasing ownership.

    Only the checkpoint's own pane is addressed. Its identity and workspace
    must match the durable checkpoint, and its occupying process must be this
    task's own named agent -- proved through ``agent.get`` on the unique name,
    matching the reported kind only to the reported kind -- so a foreign,
    reused or unverified worker is never closed. A successful close request is
    only an acknowledgement: the pane must then be verifiably absent.
    """
    checkpoint = record.get("checkpoint") or {}
    pane_id = checkpoint.get("pane_id")
    workspace_id = checkpoint.get("workspace_id")
    if not isinstance(pane_id, str) or not pane_id:
        if checkpoint.get("agent_started") or checkpoint.get("prompt_issued"):
            return None, "started agent has no verified pane identity"
        return None, None
    if not isinstance(workspace_id, str) or not workspace_id:
        return None, "task workspace identity is unavailable"
    if driver is None:
        return None, "runtime driver unavailable; pane closure not verified"
    try:
        pane = driver.pane_get(pane_id)
    except product_driver.DriverNotFound:
        return pane_id, None
    except product_driver.DriverError as exc:
        return None, str(exc)
    if (not isinstance(pane, Mapping) or pane.get("pane_id") != pane_id
            or pane.get("workspace_id") != workspace_id):
        return None, "pane identity or workspace identity mismatch"
    ownership_error = _named_agent_ownership_error(
        driver, checkpoint, pane, pane_id, workspace_id)
    if ownership_error is not None:
        return None, ownership_error
    try:
        driver.pane_close(pane_id)
    except product_driver.DriverError as exc:
        return None, str(exc)
    # A successful close request is an acknowledgement, not proof that the
    # runtime-owned worker process has stopped. Verify the pane is absent.
    try:
        driver.pane_get(pane_id)
    except product_driver.DriverNotFound:
        return pane_id, None
    except product_driver.DriverError as exc:
        return None, "pane closure verification unavailable: " + str(exc)
    return None, "pane still exists after close acknowledged"


def _finish(
    conn: Any,
    driver: product_driver.RuntimeDriver,
    record: dict[str, Any],
    env: Mapping[str, str] | None,
    clock: Callable[[], Any] | None,
    defaults: product_config.TaskDefaults,
    wait_ms: int | None,
    verify: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    _observe(driver, record, env, clock, defaults)
    _save(record, env, clock)

    effective_wait = defaults.wait_timeout_ms if wait_ms is None else wait_ms
    if isinstance(effective_wait, bool) or not isinstance(effective_wait, int) or effective_wait < 0:
        raise TaskUsageError("wait_ms must be a non-negative integer")

    state: str | None = None
    wait_not_found = False
    if effective_wait > 0:
        checkpoint = record.get("checkpoint") or {}
        # A focused tab reports `idle` (ready-for-input) for the same underlying
        # settled completion that an unseen background worker reports as
        # `done`; focusing a finished pane therefore turns DONE into IDLE. A
        # team execution (one that supplies a completion gate) may settle at
        # either, so it waits on both. An ordinary task keeps waiting only on
        # `done`/`blocked`: a bare idle never proves its work.
        wait_until = (
            ("idle", "done", "blocked") if verify is not None
            else ("done", "blocked")
        )
        try:
            result = driver.agent_wait(
                checkpoint["agent_name"],
                until=wait_until,
                timeout_ms=effective_wait,
            )
            state = product_driver.wait_state(result)
        except product_driver.DriverNotFound as exc:
            # An asynchronous pane close may have completed after a previous
            # post-close probe failed. The exact claim, Git result and owned
            # pane absence still have to pass verification below.
            wait_not_found = True
            record["observation"] = {
                "status": "UNKNOWN", "reason": getattr(exc, "code", None),
            }
        except product_driver.DriverError as exc:
            record["observation"] = {
                "status": "UNKNOWN",
                "reason": getattr(exc, "code", None),
            }
            state = None

    checkpoint = record.get("checkpoint") or {}
    recorded_close = (
        isinstance(record.get("pane_closed"), str)
        and bool(record.get("pane_closed"))
        and record.get("pane_closed") == checkpoint.get("pane_id")
    )
    # The exact owned pane is probed once: a durable settlement is reconciled
    # only when the pane it settled is verifiably absent right now.
    pane_gone = _owned_pane_gone(driver, record)
    # A settlement that was made durable (see ``terminal_result``) but whose
    # pane closure was never recorded is an interrupted close: the receipt is
    # already on disk, the claim is still open and the owned pane is gone. It is
    # trusted only for this task's own disposition and only when the pane is
    # verifiably absent, and it still runs the full verified-result, claim and
    # Git predicate below -- never a bare not-found.
    settled_recovery = _own_settled_disposition(record) and pane_gone
    # Post-close recovery: the pane was closed on an earlier run but the claim
    # was not released. It is trusted only when the exact owned pane is now
    # verifiably absent. A bare wait not-found is ambiguous for a READ and
    # holds it, while a WRITE keeps its existing recovery followed by the full
    # Git/claim completion predicate below.
    closed_checkpoint = (
        (recorded_close and pane_gone)
        or (record["mode"] == registry.WRITE and wait_not_found)
        or settled_recovery
    )
    # If the pane closed and the Registry refused release, agent_wait can no
    # longer find that agent. Retain its original claim and recover by proving
    # the same closed pane plus committed Git result, without restarting it.
    # Prompt acknowledgement does not mean prompt completion. Some runtimes
    # report IDLE immediately after accepting a prompt, before the model starts
    # working, so a bare idle is never terminal. A post-prompt DONE is
    # authoritative; a team IDLE is only a *candidate* that must carry this
    # execution's own verified, task-bound terminal result before it may settle.
    settled = state == "done"
    idle_candidate = (
        state == "idle" and verify is not None and not closed_checkpoint
    )
    if settled or idle_candidate or closed_checkpoint:
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
            observed_entry = _observe(
                driver, record, env, clock, defaults, terminal=True)
            # A caller-supplied completion gate (the team's exact output,
            # nonce and review-verdict check) runs on the just-saved durable
            # settled evidence, BEFORE the pane is closed and the claim is
            # released. A refused DONE (or closed recovery) holds the claim and
            # the pane UNKNOWN; a refused IDLE is only an early idle and stays
            # RUNNING (handled below).
            if verify is not None:
                refusal = verify(record)
                if refusal is not None:
                    if idle_candidate:
                        # An idle that does not carry this execution's verified
                        # result is an early/transient idle, not completion.
                        # Withdraw the provisional terminal provenance and keep
                        # the claim: the task stays RUNNING -- never falsely
                        # complete and never UNKNOWN on an early idle.
                        if isinstance(observed_entry, dict):
                            observed_entry["terminal"] = False
                        record["status"] = "running"
                        record.pop("error", None)
                        _save(record, env, clock)
                        return task_payload(record)
                    record["status"] = "unknown"
                    record["error"] = {
                        "code": str(refusal.get("code") or "TASK_RESULT_UNVERIFIED"),
                        "message": str(refusal.get("message") or
                                       "the terminal result could not be verified"),
                    }
                    _save(record, env, clock)
                    return task_payload(record)
            # The verified terminal settlement is made durable BEFORE the first
            # pane-close attempt. A crash between this save and the recorded
            # close would otherwise lose the settled receipt while leaving the
            # claim open and stranding ownership. The disposition is bound to
            # this task's exact claim, repository, run and owned pane, so a
            # later run only ever reconciles its own interrupted settlement.
            record["terminal_result"] = {
                "verified_at": evidence.utc_now(clock),
                "claim_id": (record.get("claim") or {}).get("claim_id"),
                "mode": record["mode"],
                "repo": record["repo"],
                "run_id": record["run_id"],
                "work_key": record["work_key"],
                "pane_id": checkpoint.get("pane_id"),
                "agent_name": checkpoint.get("agent_name"),
            }
            _save(record, env, clock)
            # Closing the exact CAO-owned pane and verifying its disappearance
            # is a precondition for handing back either mode. A still-running
            # worker -- of either mode -- must retain its Registry ownership.
            closed, error = _close_owned_pane(driver, record)
            if error is not None:
                record["status"] = "blocked"
                record["error"] = {
                    "code": "TASK_PANE_CLOSE_FAILED",
                    "message": "agent was not proven stopped; claim retained",
                    "detail": error,
                }
                _save(record, env, clock)
                return task_payload(record)
            record["pane_closed"] = closed
            # Persist the verified close immediately, BEFORE the Registry
            # release, so a crash between the two never loses the exact
            # owned-pane identity a later run uses to recover the open claim
            # without re-executing the worker.
            _save(record, env, clock)
            if record["mode"] == registry.WRITE:
                # Closing a worker is a boundary. Check source state again
                # because the previous Git probe ran while it was alive.
                failure = _write_completion_error(conn, record, head, branch)
                if failure is not None:
                    record["status"] = "blocked"
                    record["error"] = {"code": failure[0], "message": failure[1]}
                    _save(record, env, clock)
                    return task_payload(record)
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
    # The stored capture is already a bounded window of the runtime read. A
    # view limit keeps the SAME end as that bound, so a caller asking for the
    # last characters still sees the completion receipt, and the metadata keeps
    # the stored bound and the view bound separate instead of conflating them.
    bounds = dict(record.get("output_bounds") or {})
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise TaskUsageError("limit must be a non-negative integer")
        if len(output) > limit:
            bounds["view_dropped_chars"] = len(output) - (limit if limit else 0)
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
        "output_bounds": bounds or None,
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
        if not claim.get("claim_id"):
            # A task refused before its first effect holds no claim at all.
            # Stop it only when the ledger proves no row was ever opened for
            # this exact identity and the record started nothing: nothing is
            # released, nothing is closed, and the stopped state is safe.
            checkpoint = record.get("checkpoint") or {}
            started = (checkpoint.get("agent_started")
                       or checkpoint.get("prompt_issued")
                       or checkpoint.get("pane_id")
                       or checkpoint.get("workspace_id"))
            open_rows = conn.execute(
                "SELECT claim_id FROM claims WHERE run_id=? AND work_key=? "
                "AND issue='' AND owner=?",
                (record["run_id"], record["work_key"], OWNER),
            ).fetchall()
            if started or open_rows:
                record["error"] = {
                    "code": "TASK_CLAIM_RELEASE_REJECTED",
                    "message": "the task's original claim cannot be verified; pane left untouched",
                }
                _save(record, env, clock)
                return task_payload(record)
            record["claim"] = {
                "claim_id": None, "status": "RELEASED", "outcome": "abandon",
                "released": True,
            }
            record["pane_closed"] = None
            record.pop("error", None)
            record["status"] = "stopped"
            _save(record, env, clock)
            _write_service("stopped", env, clock)
            return task_payload(record)
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

    closed, close_error = _close_owned_pane(driver, record)

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

    # Persist the verified close immediately, BEFORE the Registry release, so a
    # crash between the two never loses the exact owned-pane identity a later
    # stop run uses to finish the release without touching the pane again.
    record["pane_closed"] = closed
    _save(record, env, clock)

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
