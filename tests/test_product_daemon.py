"""Tests for the CAO-owned user-level orchestration daemon.

A fake driver, an injected clock and a no-op sleep keep this hermetic: no live
runtime, no network and never a real sleep. A sandboxed XDG root plus a
sandboxed registry ledger keep the governed ledger untouched. The suite pins
the lifecycle (start/stop/status), one-tick reconciliation, idempotent resume
without a duplicate prompt, deduplicated manager wake across a restart, bounded
retries that hold UNKNOWN, fail-closed corrupt state, and the WRITE claim path
that supplies a real occupancy observation.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from cao import (
    product_config,
    product_daemon,
    product_driver,
    product_state,
    product_task,
    registry,
)
from cao.registry import RuntimeObservation, AgentObservation
from cao.state import StateError

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


class FakeClock:
    def __init__(self) -> None:
        self._moment = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self._moment += timedelta(seconds=1)
        return self._moment


class FakeDriver:
    """Record calls and answer with deterministic, occupancy-bearing values."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.readable = True
        self.wait_state = "done"
        self.wait_error = False
        self.read_text = "agent output"
        self._workspaces: dict[str, dict] = {}
        self._panes: dict[str, dict] = {}
        self._agents: dict[str, str] = {}

    def _note(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    # -- reads -------------------------------------------------------------

    def workspace_list(self):
        self._note("workspace_list")
        if not self.readable:
            raise product_driver.DriverTransportError("runtime unavailable")
        return [
            {
                "workspace_id": w["workspace_id"],
                "label": w.get("label"),
                "active_tab_id": w.get("active_tab_id"),
            }
            for w in self._workspaces.values()
        ]

    def pane_list(self, workspace_id):
        self._note("pane_list", workspace_id)
        if not self.readable:
            raise product_driver.DriverTransportError("runtime unavailable")
        return [
            {
                "pane_id": p["pane_id"],
                "workspace_id": workspace_id,
                "agent_status": p.get("agent_status"),
            }
            for p in self._panes.values()
            if p["workspace_id"] == workspace_id
        ]

    def pane_get(self, pane_id):
        self._note("pane_get", pane_id)
        if not self.readable:
            raise product_driver.DriverTransportError("runtime unavailable")
        pane = self._panes.get(pane_id)
        if pane is None:
            raise product_driver.DriverNotFound("no such pane")
        return dict(pane)

    # -- writes ------------------------------------------------------------

    def workspace_create(self, label, cwd=None):
        self._note("workspace_create", label, cwd)
        ws_id = "w%d" % (len(self._workspaces) + 1)
        pane_id = ws_id + ":p1"
        self._workspaces[ws_id] = {
            "workspace_id": ws_id,
            "label": label,
            "active_tab_id": ws_id + ":t1",
        }
        self._panes[pane_id] = {
            "pane_id": pane_id,
            "workspace_id": ws_id,
            "cwd": cwd,
            "agent": None,
            "agent_status": "unknown",
        }
        return {"workspace_id": ws_id, "tab_id": ws_id + ":t1", "pane_id": pane_id}

    def pane_close(self, pane_id):
        self._note("pane_close", pane_id)
        return "{}"

    def agent_start(self, name, kind, pane_id, *, argv=(), timeout_ms=None):
        self._note("agent_start", name, kind, pane_id)
        self._agents[name] = pane_id
        pane = self._panes.get(pane_id)
        if pane is not None:
            pane["agent"] = name
            pane["agent_status"] = "working"
        return {"agent_pane_id": pane_id, "agent_workspace_id": None}

    def agent_prompt(self, target, text, *, timeout_ms=None, wait=False):
        self._note("agent_prompt", target, text)
        return {"agent_pane_id": self._agents.get(target), "agent_workspace_id": None}

    def agent_read(self, target, *, lines=200, source="recent"):
        self._note("agent_read", target)
        return self.read_text

    def agent_get(self, target):
        self._note("agent_get", target)
        if target in self._agents:
            return {"name": target, "agent_status": "working"}
        raise product_driver.DriverNotFound("not present")

    def agent_wait(self, target, *, until=(), timeout_ms=None):
        self._note("agent_wait", target)
        if self.wait_error:
            raise product_driver.DriverTransportError("wait unavailable")
        return {"agent": {"agent_status": self.wait_state}}

    # -- test controls -----------------------------------------------------

    def drop_agents(self) -> None:
        self._agents.clear()
        for pane in self._panes.values():
            pane["agent"] = None
            pane["agent_status"] = "unknown"


def git_repo(root: Path, name: str = "repo", files=("a.txt",)) -> Path:
    repo = root / name
    repo.mkdir()

    def run(*args):
        subprocess.run(
            ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
            check=True,
            capture_output=True,
            env={**os.environ, **GIT_ENV},
        )

    run("init", "-b", "main")
    for item in files:
        (repo / item).write_text("one\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "init")
    return repo


class DaemonCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = git_repo(self.root, "project")
        self.env = {
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        }
        self.db = str(self.root / "registry.sqlite3")
        self.clock = FakeClock()
        self.config = product_config.validate_config(
            {
                "schema": product_config.SCHEMA,
                "provider": {"cli": "opencode", "model": "m"},
            }
        )
        patcher = mock.patch.dict(os.environ, self.env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            self.addCleanup(os.environ.pop, key, None)

    def build(self, driver, *, spawn=None, alive=None, config=None, max_ticks=None,
              terminate=None, identity=None):
        return product_daemon.Daemon(
            driver=driver,
            config=config or self.config,
            env=self.env,
            registry_db=self.db,
            clock=self.clock,
            sleep=lambda _seconds: None,
            spawn=spawn or (lambda: 0),
            alive=alive or (lambda _pid: False),
            terminate=terminate if terminate is not None else (lambda _pid: None),
            identity=identity,
            max_ticks=max_ticks,
        )

    def run_read(self, driver, prompt="Review this repository", wait_ms=0):
        return product_task.run_task(
            driver=driver,
            repo=str(self.repo),
            prompt=prompt,
            config=self.config,
            env=self.env,
            registry_db=self.db,
            wait_ms=wait_ms,
        )

    def write_task_record(self, task_id: str) -> None:
        stamp = "2026-01-01T00:00:00Z"
        product_state.write_task(
            {
                "schema": product_state.TASK_SCHEMA,
                "task_id": task_id,
                "run_id": product_config.DEFAULT_RUN_ID,
                "work_key": task_id,
                "repo": str(self.repo),
                "prompt": "Edit the code",
                "mode": "WRITE",
                "scope": ["a.txt"],
                "agent_kind": "opencode",
                "agent_args": [],
                "workspace_label": "cao-review-" + task_id,
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
            },
            self.env,
        )


class LifecycleTests(DaemonCase):
    def test_start_writes_running_and_is_idempotent(self) -> None:
        driver = FakeDriver()
        spawns: list[int] = []

        def spawn() -> int:
            spawns.append(1)
            return 4321

        daemon = self.build(
            driver,
            spawn=spawn,
            alive=lambda pid: pid == 4321,
            identity=lambda pid: {"pid": pid, "boot_id": "boot", "starttime": "100"},
        )

        started = daemon.start()
        self.assertEqual(started["desired"], "running")
        self.assertEqual(started["observed"], "running")
        self.assertFalse(started["already_running"])
        service = product_state.read_service(self.env)
        self.assertEqual(service["desired"], "running")
        self.assertEqual(service["observed"], "running")

        first_tick = daemon.tick()
        self.assertTrue(first_tick["ticked"])
        self.assertEqual(first_tick["ticks"], 1)

        second = daemon.start()
        self.assertTrue(second["already_running"])
        # Idempotent: exactly one loop was ever spawned, and no durable progress
        # (the tick counter) was reset.
        self.assertEqual(len(spawns), 1)
        self.assertEqual(product_state.read_daemon(self.env)["ticks"], 1)

    def test_status_reports_the_durable_record(self) -> None:
        driver = FakeDriver()
        identity = lambda pid: {"pid": pid, "boot_id": "boot", "starttime": "100"}
        daemon = self.build(
            driver,
            spawn=lambda: 4321,
            alive=lambda pid: pid == 4321,
            identity=identity,
        )
        daemon.start()
        daemon.tick()
        payload = product_daemon.status(
            self.env, alive=lambda pid: pid == 4321, identity=identity
        )
        self.assertEqual(payload["schema"], product_daemon.DAEMON_STATUS_SCHEMA)
        self.assertEqual(payload["status"], "RUNNING")
        self.assertEqual(payload["ticks"], 1)
        self.assertEqual(payload["desired"], "running")

    def test_stop_writes_stopped_and_the_loop_exits(self) -> None:
        driver = FakeDriver()
        daemon = self.build(driver)
        daemon.start()
        stopped = daemon.stop()
        self.assertEqual(stopped["desired"], "stopped")
        self.assertEqual(stopped["observed"], "stopped")
        service = product_state.read_service(self.env)
        self.assertEqual(service["desired"], "stopped")
        self.assertEqual(service["observed"], "stopped")

        # The loop observes the stop and exits without ticking.
        result = daemon.run()
        self.assertEqual(result["status"], "STOPPED")

    def test_corrupt_daemon_state_raises(self) -> None:
        path = product_state.daemon_record_path(self.env)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text("{ not json", encoding="utf-8")
        daemon = self.build(FakeDriver())
        with self.assertRaises(StateError):
            daemon.tick()
        with self.assertRaises(StateError):
            product_daemon.status(self.env)


class StopIdentityTests(DaemonCase):
    """``stop`` signals only CAO's own verified process instance.

    A pid alone is not identity: after a restart the OS can recycle it onto an
    unrelated process. The daemon records an immutable instance identity (boot
    id plus start time) and refuses to signal unless the live reading matches.
    """

    @staticmethod
    def _identity(pid):
        return {"pid": pid, "boot_id": "boot", "starttime": "100"}

    def test_stop_signals_only_the_verified_own_process(self) -> None:
        signals: list[int] = []
        live = {"alive": True}

        def alive(_pid):
            return live["alive"]

        def terminate(pid):
            signals.append(pid)
            live["alive"] = False

        daemon = self.build(
            FakeDriver(),
            spawn=lambda: 4321,
            alive=alive,
            terminate=terminate,
            identity=self._identity,
        )
        daemon.start()
        stopped = daemon.stop()
        self.assertEqual(signals, [4321])
        self.assertEqual(stopped["observed"], "stopped")
        self.assertEqual(stopped["status"], "STOPPED")

    def test_stop_refuses_a_recycled_pid_without_signaling(self) -> None:
        signals: list[int] = []
        reads = {"n": 0}

        def identity(pid):
            reads["n"] += 1
            # start() records starttime 100; the live reading is a different
            # instance (999), i.e. the pid was recycled onto another process.
            return {
                "pid": pid,
                "boot_id": "boot",
                "starttime": "100" if reads["n"] == 1 else "999",
            }

        daemon = self.build(
            FakeDriver(),
            spawn=lambda: 4321,
            alive=lambda _pid: True,
            terminate=signals.append,
            identity=identity,
        )
        daemon.start()
        stopped = daemon.stop()
        self.assertEqual(signals, [])
        self.assertEqual(stopped["observed"], "unknown")
        self.assertNotEqual(stopped["status"], "STOPPED")

    def test_stop_refuses_an_unreadable_identity(self) -> None:
        signals: list[int] = []
        daemon = self.build(
            FakeDriver(),
            spawn=lambda: 4321,
            alive=lambda _pid: True,
            terminate=signals.append,
            identity=lambda _pid: None,
        )
        daemon.start()
        stopped = daemon.stop()
        self.assertEqual(signals, [])
        self.assertEqual(stopped["observed"], "unknown")

    def test_stop_refuses_a_corrupt_recorded_identity(self) -> None:
        signals: list[int] = []
        record = product_state.default_daemon_record()
        record.update(
            {
                "desired": "running",
                "observed": "running",
                "pid": 4321,
                "pid_identity": "not-a-dict",
            }
        )
        product_state.write_daemon(record, self.env)
        daemon = self.build(
            FakeDriver(),
            alive=lambda _pid: True,
            terminate=signals.append,
            identity=self._identity,
        )
        stopped = daemon.stop()
        self.assertEqual(signals, [])
        self.assertEqual(stopped["observed"], "unknown")

    def test_stop_with_a_stale_identity_never_signals(self) -> None:
        # A recorded identity from before a reboot can never match: the boot id
        # changed, so the pid is not CAO's process.
        signals: list[int] = []
        record = product_state.default_daemon_record()
        record.update(
            {
                "desired": "running",
                "observed": "running",
                "pid": 4321,
                "pid_identity": {
                    "pid": 4321,
                    "boot_id": "before-reboot",
                    "starttime": "1",
                },
            }
        )
        product_state.write_daemon(record, self.env)
        daemon = self.build(
            FakeDriver(),
            alive=lambda _pid: True,
            terminate=signals.append,
            identity=self._identity,
        )
        stopped = daemon.stop()
        self.assertEqual(signals, [])
        self.assertNotEqual(stopped["status"], "STOPPED")

    def test_pidfd_stop_ends_a_real_own_process(self) -> None:
        if not (
            hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")
        ):
            self.skipTest("pidfd is unavailable on this platform")
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        identity = product_daemon._process_identity(proc.pid)
        if identity is None:
            self.skipTest("no /proc process identity is available")
        record = product_state.default_daemon_record()
        record.update(
            {
                "desired": "running",
                "observed": "running",
                "pid": proc.pid,
                "pid_identity": identity,
            }
        )
        product_state.write_daemon(record, self.env)
        daemon = product_daemon.Daemon(
            config=self.config,
            env=self.env,
            clock=self.clock,
            sleep=time.sleep,
            # Popen.poll reaps the child, so a terminated process is observed
            # gone instead of lingering as a zombie.
            alive=lambda _pid: proc.poll() is None,
        )
        stopped = daemon.stop()
        self.assertEqual(stopped["observed"], "stopped")
        proc.wait(timeout=5)
        self.assertIsNotNone(proc.returncode)


class StartIdentityTests(DaemonCase):
    """``start`` and ``status`` require a verified process instance, not a pid."""

    def _write_running(self, pid, pid_identity) -> None:
        record = product_state.default_daemon_record()
        record.update(
            {
                "desired": "running",
                "observed": "running",
                "pid": pid,
                "pid_identity": pid_identity,
            }
        )
        product_state.write_daemon(record, self.env)

    def test_start_reuses_a_verified_live_own_process(self) -> None:
        spawns: list[int] = []
        own = os.getpid()
        self._write_running(own, product_daemon._process_identity(own))
        daemon = self.build(
            FakeDriver(),
            spawn=lambda: (spawns.append(1), 4321)[1],
            alive=lambda pid: pid == own,
        )
        payload = daemon.start()
        self.assertTrue(payload["already_running"])
        self.assertEqual(spawns, [])

    def test_start_refuses_a_live_recycled_pid(self) -> None:
        spawns: list[int] = []
        signals: list[int] = []
        own = os.getpid()
        self._write_running(
            own, {"pid": own, "boot_id": "wrong-boot", "starttime": "1"}
        )
        daemon = self.build(
            FakeDriver(),
            spawn=lambda: (spawns.append(1), 4321)[1],
            alive=lambda pid: pid == own,
            terminate=signals.append,
        )
        with self.assertRaises(product_daemon.DaemonUnavailable):
            daemon.start()
        # No duplicate child, no signal to the foreign process.
        self.assertEqual(spawns, [])
        self.assertEqual(signals, [])

    def test_start_refuses_a_legacy_record_without_identity(self) -> None:
        spawns: list[int] = []
        own = os.getpid()
        self._write_running(own, None)
        daemon = self.build(
            FakeDriver(),
            spawn=lambda: (spawns.append(1), 4321)[1],
            alive=lambda pid: pid == own,
        )
        with self.assertRaises(product_daemon.DaemonUnavailable):
            daemon.start()
        self.assertEqual(spawns, [])

    def test_start_replaces_a_proven_stale_instance(self) -> None:
        spawns: list[int] = []
        self._write_running(4321, {"pid": 4321, "boot_id": "b", "starttime": "1"})
        daemon = self.build(
            FakeDriver(),
            spawn=lambda: (spawns.append(1), 9999)[1],
            alive=lambda _pid: False,
        )
        payload = daemon.start()
        self.assertFalse(payload["already_running"])
        self.assertEqual(len(spawns), 1)

    def test_status_reports_unknown_for_a_recycled_pid(self) -> None:
        own = os.getpid()
        self._write_running(own, {"pid": own, "boot_id": "wrong", "starttime": "1"})
        payload = product_daemon.status(self.env)
        self.assertEqual(payload["status"], "UNKNOWN")
        self.assertFalse(payload["ok"])

    def test_status_reports_unknown_for_a_legacy_record(self) -> None:
        self._write_running(os.getpid(), None)
        payload = product_daemon.status(self.env)
        self.assertEqual(payload["status"], "UNKNOWN")
        self.assertFalse(payload["ok"])

    def test_status_reports_unknown_for_a_stale_pid(self) -> None:
        dead = subprocess.Popen(["true"])
        dead.wait()
        self._write_running(
            dead.pid, {"pid": dead.pid, "boot_id": "b", "starttime": "1"}
        )
        payload = product_daemon.status(self.env)
        self.assertEqual(payload["status"], "UNKNOWN")
        self.assertNotEqual(payload["status"], "RUNNING")


class ReconciliationTests(DaemonCase):
    def test_one_tick_reconciles_a_running_task(self) -> None:
        driver = FakeDriver()
        task = self.run_read(driver)
        self.assertEqual(task["status"], "running")
        daemon = self.build(driver)
        daemon.start()
        summary = daemon.tick()
        self.assertTrue(summary["ticked"])
        self.assertEqual(summary["ticks"], 1)
        self.assertIn("workspace_list", driver.names())
        supervision = product_state.read_daemon(self.env)["supervision"]
        self.assertEqual(supervision[task["task_id"]]["status"], "active")

    def test_stale_task_is_redriven_without_a_duplicate_prompt(self) -> None:
        driver = FakeDriver()
        task = self.run_read(driver)
        self.assertEqual(driver.names().count("agent_prompt"), 1)

        # The agent vanishes; the task record is now stale.
        driver.drop_agents()
        daemon = self.build(driver)
        daemon.start()
        daemon.tick()

        # Idempotent resume: no second prompt and no second start.
        self.assertEqual(driver.names().count("agent_prompt"), 1)
        supervision = product_state.read_daemon(self.env)["supervision"]
        self.assertEqual(supervision[task["task_id"]]["action"], "redrive")

    def test_finished_agent_reaches_handback_through_one_redrive(self) -> None:
        # The live pilot found this: a task whose prompt was issued and whose
        # agent then finished must reach completion through supervision, not
        # re-drive forever. The re-drive waits for the finished agent, so the
        # handback (claim released with the observed HEAD) happens on the first
        # tick after the agent goes, with no second prompt or start.
        driver = FakeDriver()
        task = self.run_read(driver, wait_ms=0)
        self.assertEqual(task["status"], "running")
        self.assertEqual(driver.names().count("agent_prompt"), 1)

        driver.drop_agents()
        daemon = self.build(driver)
        daemon.start()
        summary = daemon.tick()

        self.assertEqual(driver.names().count("agent_prompt"), 1)
        self.assertEqual(driver.names().count("agent_start"), 1)
        record = product_state.read_task(task["task_id"], self.env)
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["claim"]["outcome"], "complete")
        self.assertTrue(record["claim"]["released"])
        self.assertTrue(record["claim"]["released_head"])
        supervision = product_state.read_daemon(self.env)["supervision"]
        self.assertEqual(supervision[task["task_id"]]["action"], "redrive")
        self.assertTrue(summary["ticked"])
        record_daemon = product_state.read_daemon(self.env)
        self.assertEqual(record_daemon["retries"].get(task["task_id"]), None)

    def test_bounded_retries_then_hold_unknown(self) -> None:
        driver = FakeDriver()
        task = self.run_read(driver, wait_ms=0)
        driver.drop_agents()
        # The agent is gone but its terminal state cannot be read: every
        # re-drive fails to wait, so the bound must eventually hold UNKNOWN
        # rather than re-drive forever.
        driver.wait_error = True
        config = product_config.validate_config(
            {
                "schema": product_config.SCHEMA,
                "provider": {"cli": "opencode", "model": "m"},
                "daemon": {"retry_bound": 2},
            }
        )
        daemon = self.build(driver, config=config)
        daemon.start()
        for _ in range(3):
            daemon.tick()
        record = product_state.read_daemon(self.env)
        self.assertEqual(record["supervision"][task["task_id"]]["status"], "HELD_UNKNOWN")
        self.assertGreaterEqual(record["errors"], 1)
        # A held task never re-prompts.
        self.assertEqual(driver.names().count("agent_prompt"), 1)

    def test_occupancy_unreadable_holds_unknown(self) -> None:
        driver = FakeDriver()
        task = self.run_read(driver)
        driver.readable = False
        driver.drop_agents()
        daemon = self.build(driver)
        daemon.start()
        daemon.tick()
        # Nothing was re-driven against unreadable occupancy.
        self.assertEqual(driver.names().count("workspace_create"), 1)
        supervision = product_state.read_daemon(self.env)["supervision"]
        self.assertEqual(supervision[task["task_id"]]["status"], "UNKNOWN")

    def test_write_task_claim_path_supplies_a_real_observation(self) -> None:
        driver = FakeDriver()
        task_id = "twrite01"
        self.write_task_record(task_id)
        daemon = self.build(driver)
        daemon.start()
        daemon.tick()
        record = product_state.read_task(task_id, self.env)
        # The WRITE claim went through with the real occupancy observation and
        # the re-drive waited, so the task reached its handback.
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["claim"]["mode"], "WRITE")
        self.assertEqual(record["claim"]["outcome"], "complete")
        self.assertIn("agent_prompt", driver.names())

    def test_write_task_holds_unknown_when_occupancy_unreadable(self) -> None:
        driver = FakeDriver()
        driver.readable = False
        task_id = "twrite02"
        self.write_task_record(task_id)
        daemon = self.build(driver)
        daemon.start()
        daemon.tick()
        record = product_state.read_task(task_id, self.env)
        # No claim was attempted; the task is exactly as it was.
        self.assertEqual(record["status"], "pending")
        self.assertNotIn("agent_start", driver.names())
        supervision = product_state.read_daemon(self.env)["supervision"]
        self.assertEqual(supervision[task_id]["status"], "UNKNOWN")


class WakeTests(DaemonCase):
    def test_manager_wake_is_deduped_across_a_restart(self) -> None:
        driver = FakeDriver()
        self.run_read(driver)

        first = self.build(driver)
        first.start()
        first_tick = first.tick()
        self.assertGreaterEqual(first_tick["wakes"], 1)

        # A fresh Daemon (a restart) with the same persisted relay ledger must
        # not re-wake for unchanged work.
        restarted = self.build(driver)
        second_tick = restarted.tick()
        self.assertEqual(second_tick["wakes"], 0)


class ProbeTests(DaemonCase):
    def test_collect_occupancy_reports_agents_and_cwd(self) -> None:
        driver = FakeDriver()
        self.run_read(driver)
        occupancy = product_daemon.collect_occupancy(driver)
        self.assertTrue(occupancy.available)
        self.assertEqual(len(occupancy.agents), 1)
        self.assertEqual(occupancy.agents[0]["worktree"], str(self.repo))

    def test_collect_occupancy_fails_closed(self) -> None:
        driver = FakeDriver()
        driver.readable = False
        occupancy = product_daemon.collect_occupancy(driver)
        self.assertFalse(occupancy.available)
        observation = product_daemon.to_runtime_observation(occupancy)
        self.assertFalse(observation.available)

    def test_occupant_without_a_usable_worktree_fails_closed(self) -> None:
        driver = FakeDriver()
        driver._workspaces["w9"] = {
            "workspace_id": "w9",
            "label": "other",
            "active_tab_id": "w9:t1",
        }
        driver._panes["w9:p1"] = {
            "pane_id": "w9:p1",
            "workspace_id": "w9",
            "cwd": None,
            "agent": "somebody",
            "agent_status": "working",
        }
        occupancy = product_daemon.collect_occupancy(driver)
        # An occupant that cannot be placed is not a healthy reading; the claim
        # path must fail closed rather than treat the repository as unoccupied.
        self.assertFalse(occupancy.available)

    def test_empty_occupancy_is_a_real_answer(self) -> None:
        driver = FakeDriver()
        occupancy = product_daemon.collect_occupancy(driver)
        self.assertTrue(occupancy.available)
        self.assertEqual(occupancy.agents, ())
        observation = product_daemon.to_runtime_observation(occupancy)
        self.assertTrue(observation.available)
        self.assertEqual(observation.agents, ())

    def test_daemon_defaults_fail_closed(self) -> None:
        config = product_config.validate_config(
            {
                "schema": product_config.SCHEMA,
                "daemon": {"tick_interval": 0.5},
            }
        )
        with self.assertRaises(product_config.ProductConfigError):
            product_config.daemon_defaults(config)

    def test_observation_mapping_matches_registry_shape(self) -> None:
        occupancy = product_daemon.Occupancy(
            True,
            (
                {
                    "agent_id": "a1",
                    "worktree": str(self.repo),
                    "state": "working",
                    "session_id": "s1",
                },
            ),
        )
        observation = product_daemon.to_runtime_observation(occupancy)
        self.assertEqual(
            observation.agents,
            (AgentObservation("a1", str(self.repo), "working", "s1"),),
        )


class CliTests(DaemonCase):
    def test_daemon_status_absent_is_not_started(self) -> None:
        import contextlib
        import io
        import json

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = product_cli_main(["daemon", "status"])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["status"], "NOT_STARTED")

    def test_daemon_start_without_config_is_unavailable(self) -> None:
        import contextlib
        import io
        import json

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = product_cli_main(["daemon", "start"])
        self.assertEqual(code, 3)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["status"], "UNAVAILABLE")
        self.assertEqual(payload["error"]["code"], "PRODUCT_CONFIG_NOT_READY")


SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


class ForkDetachTests(DaemonCase):
    """The real ``_fork_loop`` must not hold the parent's stdout pipe open.

    Without detaching the inherited standard streams, the background loop keeps
    the pipe's write end open, so any consumer of ``cao daemon start`` (a shell
    pipeline or ``capture_output``) never sees EOF and the command appears to
    hang. This drives the real entry point through a subprocess with a captured
    pipe, so it fails on a timeout if the child is not detached.
    """

    def _fake_runtime(self) -> Path:
        exe = self.root / "fake-runtime"
        exe.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys, time\n"
            "argv = sys.argv[1:]\n"
            "if argv[:2] == ['workspace', 'list']:\n"
            "    time.sleep(0.2)\n"
            "    payload = {'result': {'type': 'workspace_list', 'workspaces': []}}\n"
            "else:\n"
            "    payload = {'result': {'type': 'ok'}}\n"
            "print(json.dumps(payload))\n",
            encoding="utf-8",
        )
        exe.chmod(0o755)
        return exe

    def _run_env(self) -> dict[str, str]:
        return {
            **os.environ,
            "PYTHONPATH": str(SRC_ROOT),
            "CAO_REGISTRY_DB": self.db,
        }

    def test_daemon_start_returns_and_detaches_its_streams(self) -> None:
        exe = self._fake_runtime()
        product_state.write_managed(
            {"schema": product_state.MANAGED_SCHEMA, "path": str(exe)}, self.env
        )
        product_config.write_config(
            {
                "schema": product_config.SCHEMA,
                "provider": {"cli": "opencode", "model": "m"},
            }
        )
        run_env = self._run_env()

        # A captured pipe: this call blocks forever without the fix.
        start = subprocess.run(
            [sys.executable, "-B", "-S", "-m", "cao.product_cli", "daemon", "start"],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(start.returncode, 0, start.stderr)
        payload = json.loads(start.stdout)
        self.assertEqual(payload["status"], "RUNNING")
        self.assertIsInstance(payload["pid"], int)
        pid = payload["pid"]
        self.addCleanup(self._reap, pid)

        stop = subprocess.run(
            [sys.executable, "-B", "-S", "-m", "cao.product_cli", "daemon", "stop"],
            env=run_env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertEqual(json.loads(stop.stdout)["status"], "STOPPED")

        # The detached loop is gone once stop has confirmed it.
        deadline = 5.0
        waited = 0.0
        gone = False
        while waited < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                gone = True
                break
            time.sleep(0.05)
            waited += 0.05
        self.assertTrue(gone, "the daemon loop survived stop")

    @staticmethod
    def _reap(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def product_cli_main(argv):
    from cao import product_cli

    return product_cli.main(argv)


if __name__ == "__main__":
    unittest.main()
