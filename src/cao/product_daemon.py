"""CAO-owned, user-level orchestration daemon for the product.

The daemon is CAO's own control loop. It is deliberately independent of any
private script, and it is runtime-neutral: the managed runtime's path is
supplied as data by the product configuration, never named here.

Three things are true at once and must not be confused:

* the **managed runtime's server** has its own persistence and lifecycle. CAO
  observes it (read-only) and never starts, stops, migrates or repairs it;
* the **Work Registry ledger** is the sole claim book. The daemon never creates
  a second ledger; it can only read the governed one;
* **CAO daemon state** lives under ``$XDG_STATE_HOME/cao`` (``daemon.json`` and
  its manager-wake ledger). It is the daemon's own durable control record.

The daemon writes exactly one service record and one daemon record. Every task
lifecycle action is delegated to :mod:`cao.product_task`, so the daemon never
re-implements claim, start, prompt or handback. It reconciles intended against
observed state each tick (pattern: :func:`cao.coordinator.reconcile`), re-drives
stale or unfinished work through ``product_task``'s idempotent resume, and wakes
a manager at most once per deduped durable-progress event
(:class:`cao.relay.ProgressRelay` with a persisted ledger).

Fail-closed rules this module enforces:

* an unreadable or corrupt daemon record raises; it is never treated as absent;
* a WRITE task is never re-driven without a real, read-only occupancy
  observation. When occupancy cannot be read the daemon holds ``UNKNOWN``;
* retries are bounded. Past the bound the daemon holds ``UNKNOWN``; it never
  fabricates completion, never re-prompts and never re-starts an agent;
* the daemon never schedules automations and never sends anything outward.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from . import (
    coordinator,
    evidence,
    product_config,
    product_state,
    product_task,
    registry,
    relay,
)
from .state import StateError

__all__ = [
    "DAEMON_STATUS_SCHEMA",
    "DaemonError",
    "DaemonUsageError",
    "DaemonUnavailable",
    "Occupancy",
    "collect_occupancy",
    "to_runtime_observation",
    "status",
    "Daemon",
]

DAEMON_STATUS_SCHEMA = "cao.product.daemon.status.v0"

_TERMINAL_STATUSES = ("complete", "stopped")
_RECOVERABLE_STATUSES = ("pending", "claimed", "running")


class DaemonError(Exception):
    """Base class for daemon failures."""

    code = "PRODUCT_DAEMON_ERROR"


class DaemonUsageError(DaemonError):
    """The caller's request is malformed; nothing was attempted."""

    code = "PRODUCT_DAEMON_USAGE"


class DaemonUnavailable(DaemonError):
    """A prerequisite (configuration, runtime, occupancy) is not available."""

    code = "PRODUCT_DAEMON_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Runtime occupancy (read-only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Occupancy:
    """One coherent, read-only occupancy reading.

    ``available=False`` means the runtime could not be read; it is never
    rounded up to "nobody is there". ``available=True`` with no agents is a
    real answer: CAO genuinely observed no agents.
    """

    available: bool
    agents: tuple[dict[str, str], ...] = ()
    reason: str = ""


def _opaque_session(pane: Mapping[str, Any]) -> str:
    """The runtime's opaque session value, only when it is an identity."""

    session = pane.get("agent_session")
    if isinstance(session, Mapping) and session.get("kind") == "id":
        value = session.get("value")
        if isinstance(value, str):
            return value
    return ""


def collect_occupancy(driver: Any) -> Occupancy:
    """Read the runtime's currently occupied agents, or fail closed.

    Enumerates the runtime's workspaces and panes and reconstructs, for every
    pane that reports an agent, the agent identity, the filesystem location it
    occupies and its state. Any inability to read is explicit ``UNAVAILABLE``;
    a partially read list is never returned as if it were complete.
    """

    try:
        workspaces = driver.workspace_list()
    except Exception as exc:  # noqa: BLE001 - an unreadable runtime is not empty
        return Occupancy(False, (), f"workspace list failed: {type(exc).__name__}")
    agents: list[dict[str, str]] = []
    for workspace in workspaces:
        workspace_id = workspace.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            return Occupancy(False, (), "a workspace entry has no usable id")
        try:
            panes = driver.pane_list(workspace_id)
        except Exception as exc:  # noqa: BLE001
            return Occupancy(False, (), f"pane list failed: {type(exc).__name__}")
        for pane in panes:
            pane_id = pane.get("pane_id")
            if not isinstance(pane_id, str) or not pane_id:
                return Occupancy(False, (), "a pane entry has no usable id")
            try:
                detail = driver.pane_get(pane_id)
            except Exception as exc:  # noqa: BLE001
                return Occupancy(False, (), f"pane detail failed: {type(exc).__name__}")
            if not isinstance(detail, Mapping):
                return Occupancy(False, (), "a pane detail was not an object")
            name = detail.get("agent")
            if not isinstance(name, str) or not name:
                continue
            worktree = detail.get("cwd")
            if not isinstance(worktree, str) or not worktree:
                # An agent we cannot place cannot be vouched for. Dropping it
                # keeps the rest of the reading usable; it is never invented.
                continue
            state = detail.get("agent_status") or pane.get("agent_status") or ""
            agents.append(
                {
                    "agent_id": name,
                    "worktree": worktree,
                    "state": str(state),
                    "session_id": _opaque_session(detail),
                }
            )
    return Occupancy(True, tuple(agents), "")


def to_runtime_observation(occupancy: Occupancy) -> registry.RuntimeObservation:
    """Map a read-only occupancy reading to the registry's observation value."""

    if not occupancy.available:
        return registry.RuntimeObservation.unavailable(occupancy.reason or "unavailable")
    agents = []
    for item in occupancy.agents:
        worktree = item.get("worktree")
        if not isinstance(worktree, str) or not worktree:
            continue
        agents.append(
            registry.AgentObservation(
                agent_id=item.get("agent_id", ""),
                worktree=worktree,
                state=item.get("state", ""),
                session_id=item.get("session_id", ""),
            )
        )
    return registry.RuntimeObservation(True, tuple(agents), "")


# ---------------------------------------------------------------------------
# Process liveness helpers (injectable for tests)
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate_pid(pid: int) -> None:
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return


# ---------------------------------------------------------------------------
# The daemon
# ---------------------------------------------------------------------------


def _task_fingerprint(task: Mapping[str, Any]) -> str:
    return evidence.fingerprint(
        {
            "status": task.get("status"),
            "checkpoint": task.get("checkpoint"),
            "updated_at": task.get("updated_at"),
        }
    )


def _status_word(record: Mapping[str, Any]) -> str:
    observed = str(record.get("observed") or "")
    if observed == "not_started":
        return "NOT_STARTED"
    desired = str(record.get("desired") or "")
    if desired == "stopped":
        return "STOPPED" if observed == "stopped" else "STOPPING"
    if observed == "running":
        return "RUNNING"
    if observed == "starting":
        return "STARTING"
    return "UNKNOWN"


def _payload(
    record: Mapping[str, Any], service: Mapping[str, Any] | None, already_running: bool
) -> dict[str, Any]:
    supervision = record.get("supervision") or {}
    tasks = {
        str(task_id): str(entry.get("status"))
        for task_id, entry in supervision.items()
        if isinstance(entry, Mapping)
    }
    held = sorted(
        task_id
        for task_id, entry in supervision.items()
        if isinstance(entry, Mapping) and entry.get("status") == "HELD_UNKNOWN"
    )
    return {
        "schema": DAEMON_STATUS_SCHEMA,
        "status": _status_word(record),
        "ok": str(record.get("observed") or "") in ("running", "stopped"),
        "desired": record.get("desired"),
        "observed": record.get("observed"),
        "ticks": int(record.get("ticks") or 0),
        "wakes": int(record.get("wakes") or 0),
        "errors": int(record.get("errors") or 0),
        "started_at": record.get("started_at"),
        "last_tick_at": record.get("last_tick_at"),
        "last_wake": record.get("last_wake"),
        "pid": record.get("pid"),
        "tasks": tasks,
        "held": held,
        "already_running": already_running,
        "service": {
            "desired": (service or {}).get("desired"),
            "observed": (service or {}).get("observed"),
        },
    }


class Daemon:
    """A CAO-owned periodic orchestration loop over product tasks."""

    def __init__(
        self,
        *,
        driver: Any = None,
        config: product_config.ProductConfig | None = None,
        env: Mapping[str, str] | None = None,
        registry_db: str | None = None,
        clock: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] | None = None,
        spawn: Callable[[], int] | None = None,
        alive: Callable[[int], bool] | None = None,
        terminate: Callable[[int], None] | None = None,
        max_ticks: int | None = None,
        relay_state_path: str | None = None,
    ) -> None:
        if max_ticks is not None and (
            isinstance(max_ticks, bool) or not isinstance(max_ticks, int) or max_ticks < 0
        ):
            raise DaemonUsageError("max_ticks must be a non-negative integer")
        self._driver = driver
        self._config = config
        self._env = env
        self._registry_db = registry_db
        self._clock = clock
        self._sleep = sleep if sleep is not None else time.sleep
        self._spawn = spawn if spawn is not None else self._fork_loop
        self._alive = alive if alive is not None else _pid_alive
        self._terminate = terminate if terminate is not None else _terminate_pid
        self._max_ticks = max_ticks
        self._defaults = product_config.daemon_defaults(config, env)
        self._relay_state_path = (
            relay_state_path
            if relay_state_path is not None
            else product_state.daemon_relay_path(env)
        )
        self._relay: relay.ProgressRelay | None = None
        self._wakes_this_tick: list[dict[str, Any]] = []
        self._release_fd: int | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> dict[str, Any]:
        """Bring the daemon up. Idempotent: a live loop is left untouched."""

        record = product_state.read_daemon(self._env)
        service = product_state.read_service(self._env)
        now = evidence.utc_now(self._clock)
        pid = record.get("pid") if record is not None else None
        live = isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and self._alive(pid)
        if (
            record is not None
            and record.get("desired") == "running"
            and service is not None
            and service.get("observed") == "running"
            and live
        ):
            return _payload(record, service, True)

        record = dict(record) if record is not None else product_state.default_daemon_record()
        record.update(
            {
                "desired": "running",
                "observed": "starting",
                "pid": None,
                "ticks": 0,
                "wakes": 0,
                "errors": 0,
                "started_at": now,
                "last_tick_at": None,
                "last_wake": None,
                "retries": {},
                "checkpoints": {},
                "supervision": {},
                "updated_at": now,
            }
        )
        product_state.write_daemon(record, self._env)
        product_state.write_service(
            {
                "schema": product_state.SERVICE_SCHEMA,
                "desired": "running",
                "observed": "starting",
                "updated_at": now,
            },
            self._env,
        )

        child = self._spawn()
        record["pid"] = child
        record["observed"] = "running"
        record["updated_at"] = evidence.utc_now(self._clock)
        product_state.write_daemon(record, self._env)
        service = {
            "schema": product_state.SERVICE_SCHEMA,
            "desired": "running",
            "observed": "running",
            "updated_at": record["updated_at"],
        }
        product_state.write_service(service, self._env)
        # Only now let a forked child begin; until this point it must not write
        # the same durable records, or the two writers would race.
        self._release_loop()
        return _payload(record, service, False)

    def stop(self) -> dict[str, Any]:
        """Request the loop to exit and confirm it is gone before reporting it."""

        record = product_state.read_daemon(self._env)
        if record is None:
            record = product_state.default_daemon_record()
        now = evidence.utc_now(self._clock)
        record["desired"] = "stopped"
        record["updated_at"] = now
        product_state.write_daemon(record, self._env)

        pid = record.get("pid")
        confirmed = True
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            if self._alive(pid):
                self._terminate(pid)
                confirmed = self._await_exit(pid)
        if confirmed:
            record["observed"] = "stopped"
            record["pid"] = None
            product_state.write_daemon(record, self._env)
            service = {
                "schema": product_state.SERVICE_SCHEMA,
                "desired": "stopped",
                "observed": "stopped",
                "updated_at": now,
            }
            product_state.write_service(service, self._env)
        else:
            service = {
                "schema": product_state.SERVICE_SCHEMA,
                "desired": "stopped",
                "observed": record.get("observed"),
                "updated_at": now,
            }
            product_state.write_service(service, self._env)
        return _payload(record, service, False)

    def _await_exit(self, pid: int) -> bool:
        deadline = self._defaults.stop_timeout
        waited = 0.0
        poll = 0.1
        while self._alive(pid) and waited < deadline:
            self._sleep(poll)
            waited += poll
        return not self._alive(pid)

    def _mark_stopped(self) -> None:
        record = product_state.read_daemon(self._env) or product_state.default_daemon_record()
        now = evidence.utc_now(self._clock)
        record["observed"] = "stopped"
        record["pid"] = None
        record["updated_at"] = now
        product_state.write_daemon(record, self._env)
        if record.get("desired") == "stopped":
            product_state.write_service(
                {
                    "schema": product_state.SERVICE_SCHEMA,
                    "desired": "stopped",
                    "observed": "stopped",
                    "updated_at": now,
                },
                self._env,
            )

    # -- the loop ----------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """Tick within bounds until stopped. Never loops unbounded."""

        ticks = 0
        record = product_state.read_daemon(self._env) or product_state.default_daemon_record()
        while True:
            if record.get("desired") == "stopped":
                break
            if self._max_ticks is not None and ticks >= self._max_ticks:
                return _payload(
                    product_state.read_daemon(self._env) or record,
                    product_state.read_service(self._env),
                    False,
                )
            self.tick()
            ticks += 1
            record = product_state.read_daemon(self._env) or record
            if record.get("desired") == "stopped":
                break
            if self._max_ticks is not None and ticks >= self._max_ticks:
                break
            self._sleep(self._defaults.tick_interval)
        if record.get("desired") == "stopped":
            self._mark_stopped()
            record = product_state.read_daemon(self._env) or record
        return _payload(record, product_state.read_service(self._env), False)

    def tick(self) -> dict[str, Any]:
        """One deterministic reconciliation cycle."""

        record = product_state.read_daemon(self._env) or product_state.default_daemon_record()
        if record.get("desired") == "stopped":
            return {
                "schema": DAEMON_STATUS_SCHEMA,
                "status": "STOPPED",
                "ticked": False,
                "effects": [],
            }

        occupancy = self._occupied()
        tasks = product_state.list_tasks(self._env)
        retries = dict(record.get("retries") or {})
        checkpoints = dict(record.get("checkpoints") or {})
        supervision = dict(record.get("supervision") or {})
        effects: list[dict[str, Any]] = []

        for task in tasks:
            task_id = task.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                continue
            entry = self._reconcile_task(task, occupancy, retries, record)
            supervision[task_id] = entry
            if not entry.get("terminal"):
                checkpoints[task_id] = _task_fingerprint(task)
            if entry.get("action") not in (None, "none", "terminal"):
                effects.append(
                    {
                        "task_id": task_id,
                        "action": entry.get("action"),
                        "status": entry.get("status"),
                        "verdict": entry.get("verdict"),
                        "reason": entry.get("reason"),
                    }
                )

        wakes = self._relay_tasks(tasks)
        now = evidence.utc_now(self._clock)
        record.update(
            {
                "ticks": int(record.get("ticks") or 0) + 1,
                "last_tick_at": now,
                "updated_at": now,
                "observed": "running",
                "retries": retries,
                "checkpoints": checkpoints,
                "supervision": supervision,
            }
        )
        if wakes:
            record["wakes"] = int(record.get("wakes") or 0) + len(wakes)
            record["last_wake"] = wakes[-1]
        product_state.write_daemon(record, self._env)
        if record.get("desired") == "running":
            product_state.write_service(
                {
                    "schema": product_state.SERVICE_SCHEMA,
                    "desired": "running",
                    "observed": "running",
                    "updated_at": now,
                },
                self._env,
            )
        return {
            "schema": DAEMON_STATUS_SCHEMA,
            "status": "RUNNING",
            "ticked": True,
            "ticks": record["ticks"],
            "effects": effects,
            "wakes": len(wakes),
        }

    # -- reconciliation ----------------------------------------------------

    def _occupied(self) -> Occupancy:
        if self._driver is None:
            return Occupancy(False, (), "no runtime driver is configured")
        return collect_occupancy(self._driver)

    def _observed_state(self, task: Mapping[str, Any], occupancy: Occupancy) -> str | None:
        if not occupancy.available:
            return None
        checkpoint = task.get("checkpoint") or {}
        name = checkpoint.get("agent_name")
        if not isinstance(name, str) or not name:
            return "gone"
        for agent in occupancy.agents:
            if agent.get("agent_id") == name:
                state = str(agent.get("state") or "").strip().lower()
                return "gone" if state in registry.DEAD_STATES else "active"
        return "gone"

    def _reconcile_task(
        self,
        task: Mapping[str, Any],
        occupancy: Occupancy,
        retries: dict[str, int],
        record: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = str(task.get("task_id"))
        status = str(task.get("status") or "pending")
        mode = str(task.get("mode") or registry.READ)

        if status in _TERMINAL_STATUSES:
            retries.pop(task_id, None)
            return {"status": status, "action": "terminal", "terminal": True}

        observed = self._observed_state(task, occupancy)
        if observed is None:
            return {
                "status": "UNKNOWN",
                "action": "hold",
                "observed": None,
                "reason": "runtime occupancy could not be read",
            }

        result = coordinator.reconcile(
            [coordinator.IntendedState(task_id, "active")],
            lambda subject: coordinator.ObservedState(subject, observed),
            clock=self._clock,
        )
        verdict = result.subjects[0].verdict.value
        if verdict == "AGREEMENT":
            retries[task_id] = 0
            return {"status": "active", "action": "none", "verdict": verdict, "observed": observed}

        if status not in _RECOVERABLE_STATUSES:
            return {
                "status": "HELD_UNKNOWN",
                "action": "hold",
                "verdict": verdict,
                "observed": observed,
                "reason": f"task is {status} and is not re-driven automatically",
            }
        if mode == registry.WRITE and not occupancy.available:
            return {
                "status": "UNKNOWN",
                "action": "hold",
                "verdict": verdict,
                "observed": observed,
                "reason": "occupancy unreadable; a WRITE task is never claimed blind",
            }

        count = int(retries.get(task_id, 0))
        if count >= self._defaults.retry_bound:
            record["errors"] = int(record.get("errors") or 0) + 1
            return {
                "status": "HELD_UNKNOWN",
                "action": "hold",
                "verdict": verdict,
                "observed": observed,
                "reason": "re-drive retry bound reached; holding UNKNOWN",
            }

        retries[task_id] = count + 1
        observation = to_runtime_observation(occupancy)
        try:
            payload = product_task.run_task(
                driver=self._driver,
                repo=task.get("repo"),
                prompt=task.get("prompt"),
                task_id=task_id,
                mode=task.get("mode"),
                write_scope=task.get("scope") or (),
                config=self._config,
                env=self._env,
                registry_db=self._registry_db,
                observation=observation,
                clock=self._clock,
                # The re-drive must wait for the agent to settle: a task whose
                # prompt was already issued and whose agent has finished can
                # only reach its handback through a wait, and skipping it would
                # re-drive the same unfinished task every tick until the retry
                # bound holds it UNKNOWN.
                wait_ms=None,
            )
        except (product_task.TaskError, StateError) as exc:
            record["errors"] = int(record.get("errors") or 0) + 1
            return {
                "status": "UNKNOWN",
                "action": "hold",
                "verdict": verdict,
                "observed": observed,
                "reason": f"re-drive failed: {getattr(exc, 'code', 'PRODUCT_TASK_ERROR')}",
            }

        new_status = str(payload.get("status"))
        resumed = product_state.read_task(task_id, self._env) or task
        after = self._observed_state(resumed, occupancy)
        if new_status in _TERMINAL_STATUSES:
            retries.pop(task_id, None)
        elif new_status in ("running", "claimed") and after == "active":
            retries[task_id] = 0
        return {
            "status": new_status,
            "action": "redrive",
            "verdict": verdict,
            "observed": observed,
            "after": after,
        }

    # -- manager wake (deduplicated, persisted) ----------------------------

    def _record_wake(self, wake: relay.ManagerWake) -> None:
        self._wakes_this_tick.append(wake.as_dict())

    def _relay_obj(self) -> relay.ProgressRelay:
        if self._relay is None:
            self._relay = relay.ProgressRelay(
                sink=self._record_wake,
                state_path=self._relay_state_path,
                budget=relay.RelayBudget(interval_seconds=0.0, max_checks=1, max_errors=1),
                clock=self._clock,
            )
        return self._relay

    def _writer_state(self, task_id: str) -> dict[str, Any]:
        task = product_state.read_task(task_id, self._env)
        if task is None:
            return {"scope": [], "revision": "absent", "status": "absent", "metrics": {}}
        checkpoint = task.get("checkpoint") or {}
        scope = [item for item in (task.get("scope") or []) if isinstance(item, str) and item]
        return {
            "scope": scope,
            "revision": str(checkpoint.get("stage") or task.get("status") or "unknown"),
            "status": str(task.get("status") or "unknown"),
            "metrics": {"prompt_issued": 1 if checkpoint.get("prompt_issued") else 0},
        }

    def _relay_tasks(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Clear in place: the cached relay sink is bound to this list object.
        self._wakes_this_tick.clear()
        progress_relay = self._relay_obj()
        for task in tasks:
            task_id = task.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                continue
            if str(task.get("status") or "") in _TERMINAL_STATUSES:
                continue
            progress_relay.check(
                (lambda tid=task_id: self._writer_state(tid)), writer_id=task_id
            )
        return list(self._wakes_this_tick)

    # -- default background launch -----------------------------------------

    def _release_loop(self) -> None:
        """Let a forked child proceed once the parent has finished its writes."""

        fd = self._release_fd
        self._release_fd = None
        if fd is None:
            return
        try:
            os.write(fd, b"1")
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def _fork_loop(self) -> int:
        if not hasattr(os, "fork"):
            raise DaemonUnavailable("this platform cannot fork a background loop")
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - exercised only in a real launch
            os.close(write_fd)
            code = 0
            try:
                os.setsid()
            except OSError:
                pass
            # Detach the child from the inherited standard streams. Without
            # this the loop keeps the parent's stdout/stderr pipe write ends
            # open, so a consumer of `cao daemon start` never sees EOF and the
            # command appears to hang. ``dup2`` closes the inherited ends.
            try:
                null_fd = os.open(os.devnull, os.O_RDWR)
                try:
                    os.dup2(null_fd, 0)
                    os.dup2(null_fd, 1)
                    os.dup2(null_fd, 2)
                finally:
                    if null_fd > 2:
                        os.close(null_fd)
            except OSError:
                os._exit(1)
            # Block until the parent has finished its own start writes (or has
            # died and closed the pipe), so the two never write concurrently.
            try:
                os.read(read_fd, 1)
            except OSError:
                pass
            finally:
                os.close(read_fd)
            try:
                own = (
                    product_state.read_daemon(self._env)
                    or product_state.default_daemon_record()
                )
                own["pid"] = os.getpid()
                own["observed"] = "running"
                own["updated_at"] = evidence.utc_now(self._clock)
                product_state.write_daemon(own, self._env)
            except BaseException:  # noqa: BLE001 - a child that cannot record itself must not run
                os._exit(1)
            try:
                self.run()
            except BaseException:  # noqa: BLE001 - never leave a half-written child
                code = 1
            finally:
                os._exit(code)
        os.close(read_fd)
        self._release_fd = write_fd
        return pid


def status(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Report the durable daemon record, or an honest NOT_STARTED answer."""

    record = product_state.read_daemon(env)
    service = product_state.read_service(env)
    if record is None and service is None:
        return {
            "schema": DAEMON_STATUS_SCHEMA,
            "status": "NOT_STARTED",
            "ok": False,
            "desired": "stopped",
            "observed": "not_started",
            "ticks": 0,
            "wakes": 0,
            "errors": 0,
            "started_at": None,
            "last_tick_at": None,
            "last_wake": None,
            "pid": None,
            "tasks": {},
            "held": [],
            "already_running": None,
            "service": {"desired": None, "observed": None},
        }
    return _payload(record or product_state.default_daemon_record(), service, None)
