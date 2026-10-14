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
* ``stop`` signals only CAO's own verified process instance. The recorded pid
  must still match the live boot id and start time, and the signal travels
  through a pidfd; otherwise it fails closed and reports ``UNKNOWN``, never a
  foreign kill and never a false ``STOPPED``;
* ``start`` and ``status`` require the same identity: a live but unverifiable
  pid is ``unknown``, so ``status`` never reports ``RUNNING`` for it and
  ``start`` refuses rather than forking a duplicate daemon;
* the daemon never schedules automations and never sends anything outward.
"""

from __future__ import annotations

import os
import re
import signal
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from . import (
    coordinator,
    evidence,
    improvement,
    product_config,
    product_driver,
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
_TEAM_EXECUTION_ID = re.compile(
    r"^ct-.+-(?:work|review|correction)-[0-9]+-[0-9a-f]{20}$"
)


def _team_execution_owners(
    env: Mapping[str, str] | None,
) -> dict[str, tuple[str, str] | None]:
    """Resolve current and historical stage IDs from validated team manifests.

    A task's work, review and correction stages each have a distinct durable
    execution ID. The current slot alone is insufficient: previous stage
    records still exist and must never be adopted by generic recovery.
    """
    from . import team

    owners: dict[str, tuple[str, str] | None] = {}
    try:
        listed = team.list_teams(env=env)
    except Exception:
        # If the team directory itself is unreadable, do not impede ordinary
        # task recovery. Unverifiable team-shaped tasks still hold UNKNOWN.
        return owners
    for info in listed:
        if info.get("status") == "CORRUPT":
            continue
        team_id = info.get("team_id")
        if not isinstance(team_id, str):
            continue
        try:
            checkpoint = team._required(team_id, env)
        except Exception:
            # A disappearing or corrupt team cannot grant a new owner.
            # Its task records will hold UNKNOWN via their team run ID.
            continue
        for task in checkpoint["spec"]["tasks"]:
            slot = checkpoint["slots"][task["id"]]
            # work0 -> review0 -> correction1 -> review1 -> ...
            current_position = (
                0 if slot["phase"] == "work"
                else 2 * slot["round"] + (1 if slot["phase"] == "review" else 0)
            )
            # Include both the original READ work identity and a one-time
            # explicitly admitted retry, preserving previous task ownership.
            retry_no = slot.get("retry_no", 0)
            candidates = [("work", 0, 0)]
            if retry_no:
                candidates.append(("work", 0, retry_no))
            if task["reviewer"] is not None:
                candidates.extend(
                    ("review", r, 0)
                    for r in range(task["max_corrections"] + 1)
                )
                candidates.extend(
                    ("correction", r, 0)
                    for r in range(1, task["max_corrections"] + 1)
                )
            for phase, round_no, attempt in candidates:
                execution_id = team._execution_id(
                    team_id, task["id"], phase, round_no, attempt)
                stage_position = (
                    0 if phase == "work"
                    else 2 * round_no + (1 if phase == "review" else 0)
                )
                stage_kind = (
                    "current" if execution_id == slot["execution_id"]
                    else "historical" if (
                        stage_position < current_position
                        or (phase == "work" and attempt < retry_no)
                    )
                    else "unlaunched"
                )
                owner = (team_id, stage_kind)
                if execution_id not in owners:
                    owners[execution_id] = owner
                elif owners[execution_id] != owner:
                    owners[execution_id] = None
    return owners


def _task_owner_lane(
    record: Mapping[str, Any],
    owners: Mapping[str, tuple[str, str] | None],
) -> str:
    """Choose ordinary/current-team/historical-team/unknown recovery."""
    run_id, task_id = record.get("run_id"), record.get("task_id")
    if isinstance(task_id, str) and task_id in owners:
        owner = owners[task_id]
        if owner is not None and run_id == "cao-team-" + owner[0]:
            return "team_" + owner[1]
        return "unknown"
    if isinstance(run_id, str) and run_id.startswith("cao-team-"):
        # Missing/invalid checkpoint is never generic recovery authority.
        return "unknown"
    if isinstance(task_id, str) and _TEAM_EXECUTION_ID.fullmatch(task_id):
        # Fail closed even if BOTH the team checkpoint and run ID are missing.
        # Prefix-only lookalikes such as ct-job remain ordinary.
        return "unknown"
    return "ordinary"


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
                # An agent we cannot place cannot be vouched for, and a partial
                # occupancy reading is not a healthy one: a WRITE claim must not
                # proceed while an occupant is unaccounted for.
                return Occupancy(False, (), "an agent is not bound to a usable worktree")
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


_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"


def _read_boot_id() -> str | None:
    """The kernel boot id, or ``None`` when it cannot be read.

    The boot id is new on every reboot, so an identity recorded before a reboot
    can never match a recycled pid afterwards.
    """

    try:
        with open(_BOOT_ID_PATH, "r", encoding="ascii") as handle:
            value = handle.read().strip()
    except OSError:
        return None
    return value or None


def _read_starttime(pid: int) -> str | None:
    """Field 22 (starttime) of ``/proc/<pid>/stat``, or ``None``.

    The process name (field 2) can contain spaces and parentheses, so the
    fields are split after the final ``)``. A recycled pid gets a later
    starttime, which is what makes this value immutable for one instance.
    """

    try:
        with open(
            os.path.join("/proc", str(pid), "stat"), "r", encoding="ascii"
        ) as handle:
            data = handle.read()
    except OSError:
        return None
    end = data.rfind(")")
    if end < 0:
        return None
    fields = data[end + 1:].split()
    # fields[0] is state (field 3), so starttime (field 22) is fields[19].
    if len(fields) < 20:
        return None
    return fields[19]


def _process_identity(pid: int) -> dict[str, Any] | None:
    """A durable, immutable identity for the process instance ``pid`` now.

    Combines the kernel boot id with the instance start time. It is only
    available from Linux ``/proc``; when any part cannot be read it returns
    ``None`` so the caller fails closed instead of signaling an unverified pid.
    """

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    boot = _read_boot_id()
    start = _read_starttime(pid)
    if boot is None or start is None:
        return None
    return {"pid": pid, "boot_id": boot, "starttime": start}


def _identity_matches(stored: Any, current: Any) -> bool:
    """Whether a live reading identifies the same process instance as stored."""

    if not isinstance(stored, dict) or not isinstance(current, dict):
        return False
    pid = stored.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    for key in ("boot_id", "starttime"):
        value = stored.get(key)
        if not isinstance(value, str) or not value or current.get(key) != value:
            return False
    return current.get("pid") == pid


def _instance_state(
    record: Mapping[str, Any],
    alive: Callable[[int], bool],
    identity: Callable[[int], dict[str, Any] | None],
) -> str:
    """Classify a persisted pid as CAO's own ``live`` instance, ``stale`` or ``unknown``.

    ``live`` requires the pid to be alive AND its immutable identity to match
    the stored one. A pid that is not alive is ``stale`` (safe to replace). A
    pid that is alive but whose identity is missing, unreadable or mismatched is
    ``unknown``: it may be a recycled foreign process, so nothing may be
    signaled and no second daemon may be started.
    """

    pid = record.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return "stale"
    if not alive(pid):
        return "stale"
    if _identity_matches(record.get("pid_identity"), identity(pid)):
        return "live"
    return "unknown"


def _pidfd_terminate(pid: int, identity: Any) -> bool:
    """SIGTERM exactly the verified process instance, or refuse.

    ``pidfd_open`` binds a handle to the instance that exists at open time, so
    the signal can never reach a recycled pid: if the instance already exited
    the handle refers to nothing and the send fails with ``ESRCH``. The stored
    start time is re-checked before sending, so a pid reused between the
    decision and the open is caught. When the kernel or Python lacks pidfd
    support this refuses rather than falling back to a racy ``os.kill``.
    """

    if not isinstance(identity, dict):
        return False
    expected_boot = identity.get("boot_id")
    expected_start = identity.get("starttime")
    if not isinstance(expected_boot, str) or not isinstance(expected_start, str):
        return False
    if _read_boot_id() != expected_boot:
        return False
    open_pidfd = getattr(os, "pidfd_open", None)
    send_signal = getattr(signal, "pidfd_send_signal", None)
    if open_pidfd is None or send_signal is None:
        return False
    try:
        fd = open_pidfd(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    try:
        if _read_starttime(pid) != expected_start:
            return False
        try:
            send_signal(fd, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return True
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


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
        identity: Callable[[int], dict[str, Any] | None] | None = None,
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
        # ``terminate`` is a test seam: when supplied it signals an already
        # identity-verified pid. The production default (None) goes through
        # ``_pidfd_terminate``, which re-verifies the immutable identity and
        # cannot reach a recycled pid.
        self._terminate = terminate
        self._identity = identity if identity is not None else _process_identity
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

    def _instance_state(self, record: Mapping[str, Any]) -> str:
        """Classify the persisted pid with this daemon's injectable seams."""

        return _instance_state(record, self._alive, self._identity)

    def start(self) -> dict[str, Any]:
        """Bring the daemon up. Idempotent: a live loop is left untouched.

        A persisted ``running`` record is acknowledged only when its pid is
        still CAO's own verified process instance (boot id plus start time). A
        pid that is alive but cannot be verified is ``unknown``: start refuses
        with :class:`DaemonUnavailable` rather than forking a second daemon or
        signaling a possibly foreign process. A pid that is provably gone is
        ``stale`` and is safely replaced.
        """

        record = product_state.read_daemon(self._env)
        service = product_state.read_service(self._env)
        now = evidence.utc_now(self._clock)
        if record is not None:
            state = self._instance_state(record)
            if state == "live":
                # A verified live loop is reused, never duplicated.
                return _payload(record, service, True)
            if state == "unknown":
                raise DaemonUnavailable(
                    "the persisted daemon process cannot be verified as CAO's "
                    "own; refusing to start a second daemon or signal a "
                    "foreign process"
                )
        # Absent, not-running, or a proven-stale instance: start a new child.

        record = dict(record) if record is not None else product_state.default_daemon_record()
        record.update(
            {
                "desired": "running",
                "observed": "starting",
                "pid": None,
                "pid_identity": None,
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
        # Record the immutable instance identity BEFORE releasing the child:
        # the child only writes its own durable record once released, so the
        # parent's identity (and pid) is already on disk and the two writers
        # never race for it.
        record["pid_identity"] = self._identity(child)
        # The child has not started yet: it is waiting for _release_loop.
        # Only the child may publish RUNNING after its own startup succeeds.
        record["observed"] = "starting"
        record["updated_at"] = evidence.utc_now(self._clock)
        product_state.write_daemon(record, self._env)
        service = {
            "schema": product_state.SERVICE_SCHEMA,
            "desired": "running",
            "observed": "starting",
            "updated_at": record["updated_at"],
        }
        product_state.write_service(service, self._env)
        # Only now let a forked child begin; until this point it must not write
        # the same durable records, or the two writers would race.
        self._release_loop()
        return _payload(record, service, False)

    def stop(self) -> dict[str, Any]:
        """Request the loop to exit and confirm it is gone before reporting it.

        The desired state is written first, so a cooperative loop stops itself
        even when CAO declines to signal. A signal is sent only to CAO's own
        verified process instance; a recycled pid, an unreadable or corrupt
        identity, or a missing pidfd path leaves ``observed`` at ``unknown``
        rather than reporting a false ``STOPPED``.
        """

        record = product_state.read_daemon(self._env)
        if record is None:
            record = product_state.default_daemon_record()
        now = evidence.utc_now(self._clock)
        record["desired"] = "stopped"
        record["updated_at"] = now
        product_state.write_daemon(record, self._env)

        pid = record.get("pid")
        identity = record.get("pid_identity")
        confirmed = True
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            confirmed = self._stop_instance(pid, identity)
        if confirmed:
            record["observed"] = "stopped"
            record["pid"] = None
            record["pid_identity"] = None
            service = {
                "schema": product_state.SERVICE_SCHEMA,
                "desired": "stopped",
                "observed": "stopped",
                "updated_at": now,
            }
        else:
            # Fail closed: the process instance could not be verified, so it is
            # neither signaled nor reported as stopped.
            record["observed"] = "unknown"
            service = {
                "schema": product_state.SERVICE_SCHEMA,
                "desired": "stopped",
                "observed": "unknown",
                "updated_at": now,
            }
        product_state.write_daemon(record, self._env)
        product_state.write_service(service, self._env)
        return _payload(record, service, False)

    def _stop_instance(self, pid: int, stored_identity: Any) -> bool:
        """End CAO's own verified process instance, or fail closed.

        Returns ``True`` only when the instance is confirmed gone. When it is
        not the recorded instance -- a recycled pid, an unreadable or corrupt
        identity -- no signal is sent.
        """

        if not self._alive(pid):
            return True
        current = self._identity(pid)
        if not _identity_matches(stored_identity, current):
            return False
        if self._terminate is not None:
            self._terminate(pid)
        elif not _pidfd_terminate(pid, stored_identity):
            return False
        return self._await_exit(pid)

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
        record["pid_identity"] = None
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
        from . import team

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
        # Snapshot team ownership once, ahead of generic recovery effects.
        # Ordinary tasks still operate even when a separate team is corrupt.
        owners = _team_execution_owners(self._env)
        retries = dict(record.get("retries") or {})
        checkpoints = dict(record.get("checkpoints") or {})
        supervision = dict(record.get("supervision") or {})
        effects: list[dict[str, Any]] = []

        for task in tasks:
            task_id = task.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                continue
            lane = _task_owner_lane(task, owners)
            if lane != "ordinary":
                # Only a positively checkpointed team can own a team task.
                # Ambiguous/missing identity holds UNKNOWN; never re-drive
                # or release its Registry claim through the generic loop.
                retries.pop(task_id, None)
                recorded_status = str(task.get("status") or "")
                historical = lane == "team_historical"
                hold = lane in ("unknown", "team_unlaunched") or (
                    historical and recorded_status not in _TERMINAL_STATUSES)
                reason = (
                    "team ownership is unverified" if lane == "unknown"
                    else "team stage was not launched" if lane == "team_unlaunched"
                    else "non-current team stage" if hold else None
                )
                supervision[task_id] = {
                    "status": "HELD_UNKNOWN" if hold else recorded_status,
                    "action": ("hold" if hold else
                               "team_historical" if historical else "team_owned"),
                    "terminal": not hold and recorded_status in _TERMINAL_STATUSES,
                    "reason": reason,
                    "reconciler": "team" if lane != "unknown" else None,
                }
                if hold:
                    effects.append({
                        "task_id": task_id, "action": "team_ownership_hold",
                        "status": "UNKNOWN", "reason": reason,
                    })
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

        wakes = self._relay_tasks(tasks, owners=owners)
        # Source-attested feedback uses this existing cadence. A watch admits
        # bounded READ investigations only; all WRITE remains explicit and
        # Registry-gated in Team Execution. No separate scheduler or ledger.
        if self._config is not None and self._defaults.feedback_watch_file is not None:
            from . import feedback_loop
            try:
                report = feedback_loop.tick(self._defaults.feedback_watch_file)
                effects.append({
                    "action": "feedback_watch", "status": report["status"],
                    "watch_id": report["watch_id"],
                    "cases": len(report["observations"]),
                })
            except (feedback_loop.FeedbackError, improvement.ImprovementError,
                    team.TeamError, product_config.ProductConfigError, StateError,
                    OSError, ValueError, TypeError, UnicodeError) as exc:
                record["errors"] = int(record.get("errors") or 0) + 1
                effects.append({
                    "action": "feedback_watch_hold", "status": "UNKNOWN",
                    "reason": getattr(exc, "code", type(exc).__name__),
                })
        # One bounded engineering automation uses this same daemon cadence.
        # Only a separately authenticated operator approval can admit a
        # GitHub-native auto-merge request; the daemon never grants it.
        if self._config is not None and self._defaults.engineering_watch_file is not None:
            from . import engineering_loop
            try:
                report = engineering_loop.tick(
                    self._defaults.engineering_watch_file, execute=True)
                effects.append({
                    "action": "engineering_watch", "status": report["status"],
                    "watch_id": report["watch_id"],
                    "merge_requested": report["merge_requested"],
                })
            except (engineering_loop.EngineeringError, OSError, ValueError,
                    TypeError, UnicodeError) as exc:
                record["errors"] = int(record.get("errors") or 0) + 1
                effects.append({
                    "action": "engineering_watch_hold", "status": "UNKNOWN",
                    "reason": getattr(exc, "code", type(exc).__name__),
                })
        if self._config is not None and self._defaults.engineering_queue_dir is not None:
            from . import engineering_loop
            try:
                report = engineering_loop.tick_queue(
                    self._defaults.engineering_queue_dir, execute=True)
                effects.append({
                    "action": "engineering_queue", "status": report["status"],
                    "in_flight": report["in_flight"],
                    "merge_requested": any(
                        item["merge_requested"] for item in report["items"]),
                })
            except (engineering_loop.EngineeringError, OSError, ValueError,
                    TypeError, UnicodeError) as exc:
                record["errors"] = int(record.get("errors") or 0) + 1
                effects.append({
                    "action": "engineering_queue_hold", "status": "UNKNOWN",
                    "reason": getattr(exc, "code", type(exc).__name__),
                })
        # Teams reuse this daemon and the existing Registry/task runner. No
        # second wake daemon, queue or WRITE authority is introduced.
        if self._config is not None and self._driver is not None:
            from . import team
            try:
                for update in team.tick_all(
                    config=self._config, driver=self._driver, env=self._env,
                    registry_db=self._registry_db, clock=self._clock,
                ):
                    effects.append({
                        "team_id": update["team_id"], "action": "team_reconcile",
                        "status": update["status"],
                    })
                    # Surface the bounded per-task recovery decision the team
                    # recorded in its own checkpoint. The daemon never applies
                    # a recovery itself: it reports what the team decided.
                    for decision in update.get("recovery") or ():
                        effects.append({
                            "team_id": update["team_id"],
                            "task_id": decision.get("task_id"),
                            "action": "team_recovery",
                            "status": decision.get("decision"),
                            "classification": decision.get("classification"),
                            "auto_resume": bool(decision.get("auto_resume")),
                            "reason": decision.get("reason"),
                        })
            except (team.TeamError, StateError, OSError,
                    product_driver.DriverError, registry.RegistryError,
                    product_config.ProductConfigError) as exc:
                record["errors"] = int(record.get("errors") or 0) + 1
                effects.append({
                    "action": "team_hold", "status": "UNKNOWN",
                    "reason": getattr(exc, "code", type(exc).__name__),
                })
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

    def _relay_tasks(self, tasks: list[dict[str, Any]], *,
                     owners: Mapping[str, tuple[str, str] | None] | None = None
                     ) -> list[dict[str, Any]]:
        # Clear in place: the cached relay sink is bound to this list object.
        self._wakes_this_tick.clear()
        progress_relay = self._relay_obj()
        owners = owners if owners is not None else _team_execution_owners(self._env)
        for task in tasks:
            task_id = task.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                continue
            if _task_owner_lane(task, owners) != "ordinary":
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
                # A fast stop can race the child startup handshake. Never
                # resurrect a daemon whose desired state is already stopped.
                if own.get("desired") != "running":
                    os._exit(0)
                own["pid"] = os.getpid()
                own["observed"] = "running"
                own["updated_at"] = evidence.utc_now(self._clock)
                product_state.write_daemon(own, self._env)
                product_state.write_service(
                    {"schema": product_state.SERVICE_SCHEMA,
                     "desired": "running", "observed": "running",
                     "updated_at": own["updated_at"]},
                    self._env,
                )
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


def status(
    env: Mapping[str, str] | None = None,
    *,
    alive: Callable[[int], bool] | None = None,
    identity: Callable[[int], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Report the durable daemon record, or an honest NOT_STARTED answer.

    A record that claims a running daemon is reported ``RUNNING`` only when its
    pid is still the same, verified process instance. A stale, missing or
    mismatched identity is ``UNKNOWN``, never rounded up to ``RUNNING``.
    """

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
    current = record or product_state.default_daemon_record()
    payload = _payload(current, service, None)
    if str(current.get("observed") or "") in ("running", "starting"):
        state = _instance_state(
            current,
            alive if alive is not None else _pid_alive,
            identity if identity is not None else _process_identity,
        )
        if state != "live":
            payload["status"] = "UNKNOWN"
            payload["ok"] = False
            payload["instance"] = state
        elif str(current.get("observed") or "") == "starting":
            payload["status"] = "STARTING"
            payload["ok"] = False
    return payload
