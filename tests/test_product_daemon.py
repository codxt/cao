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
    team,
    team_recovery,
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
        self.assertEqual(started["observed"], "starting")
        self.assertEqual(started["status"], "STARTING")
        self.assertFalse(started["already_running"])
        service = product_state.read_service(self.env)
        self.assertEqual(service["desired"], "running")
        self.assertEqual(service["observed"], "starting")

        first_tick = daemon.tick()
        self.assertTrue(first_tick["ticked"])
        self.assertEqual(first_tick["ticks"], 1)

        second = daemon.start()
        self.assertTrue(second["already_running"])
        # Idempotent: exactly one loop was ever spawned, and no durable progress
        # (the tick counter) was reset.
        self.assertEqual(len(spawns), 1)
        self.assertEqual(product_state.read_daemon(self.env)["ticks"], 1)

    def test_start_requires_child_handshake_before_reporting_running(self) -> None:
        daemon = self.build(
            FakeDriver(), spawn=lambda: 4321,
            alive=lambda _pid: False,
            identity=lambda pid: {"pid": pid, "boot_id": "boot", "starttime": "100"},
        )
        started = daemon.start()
        self.assertEqual(started["status"], "STARTING")
        self.assertEqual(started["observed"], "starting")
        self.assertEqual(product_state.read_service(self.env)["observed"], "starting")
        seen = product_daemon.status(self.env, alive=lambda _pid: False)
        self.assertEqual(seen["status"], "UNKNOWN")

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
        # In this unit seam no child exists to acknowledge startup. Simulate
        # the child's verified RUNNING handback before checking status.
        child_record = product_state.read_daemon(self.env)
        child_record["observed"] = "running"
        product_state.write_daemon(child_record, self.env)
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
    def test_engineering_queue_uses_same_daemon_and_no_worker_change(self):
        queue = str(self.root / "private-queue")
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "example/model"},
            "daemon": {"engineering_queue_dir": queue},
        })
        daemon = self.build(FakeDriver(), config=config)
        daemon.start()
        report = {
            "status": "PENDING", "in_flight": "one-case",
            "items": [{"merge_requested": False, "status": "AWAITING_OPERATOR_APPROVAL"}],
        }
        with mock.patch("cao.engineering_loop.tick_queue", return_value=report) as called:
            observed = daemon.tick()
        called.assert_called_once_with(queue, execute=True)
        self.assertTrue(any(item.get("action") == "engineering_queue"
                            and item.get("status") == "PENDING"
                            and not item["merge_requested"]
                            for item in observed["effects"]))

    def test_optional_engineering_watch_uses_same_daemon_without_unapproved_merge(self):
        watch = str(self.root / "engineering-watch.json")
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "example/model"},
            "daemon": {"engineering_watch_file": watch},
        })
        driver = FakeDriver()
        daemon = self.build(driver, config=config)
        daemon.start()
        report = {
            "watch_id": "sample-loop", "status": "AWAITING_OPERATOR_APPROVAL",
            "merge_requested": False,
        }
        with mock.patch("cao.engineering_loop.tick", return_value=report) as called:
            result = daemon.tick()
        called.assert_called_once_with(watch, execute=True)
        self.assertTrue(any(x.get("action") == "engineering_watch"
                            and x.get("status") == "AWAITING_OPERATOR_APPROVAL"
                            and not x["merge_requested"]
                            for x in result["effects"]))
        self.assertEqual(product_state.read_daemon(self.env)["errors"], 0)
        with mock.patch("cao.engineering_loop.tick",
                        side_effect=ValueError("private source invalid")):
            held = daemon.tick()
        self.assertTrue(any(x.get("action") == "engineering_watch_hold"
                            and x.get("status") == "UNKNOWN"
                            for x in held["effects"]))
        self.assertEqual(product_state.read_daemon(self.env)["errors"], 1)

    def test_healthy_tick_emits_no_engineering_metrics_exception(self):
        driver = FakeDriver()
        daemon = self.build(driver)
        daemon.start()
        report = daemon.tick()
        self.assertFalse(any(item.get("action") == "engineering_metrics"
                             for item in report["effects"]))

    def test_engineering_exception_is_reported_bounded_and_applied_never(self):
        watch = str(self.root / "engineering-watch.json")
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "example/model"},
            "daemon": {"engineering_watch_file": watch},
        })
        driver = FakeDriver()
        daemon = self.build(driver, config=config)
        daemon.start()
        held = {
            "watch_id": "sample-loop", "status": "AWAITING_GITHUB_RECONCILIATION",
            "merge_requested": True,
            "exceptions": [{
                "code": "GITHUB_RECONCILIATION_PENDING",
                "source": "engineering_checkpoint",
                "subject": "sample-loop",
                "detail": "AWAITING_GITHUB_RECONCILIATION",
                "observed": None, "head": None,
            }],
        }
        with mock.patch("cao.engineering_loop.tick", return_value=held) as called:
            result = daemon.tick()
        called.assert_called_once_with(watch, execute=True)
        metrics = [item for item in result["effects"]
                   if item.get("action") == "engineering_metrics"]
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0]["status"], "EXCEPTIONS")
        self.assertEqual(metrics[0]["codes"], ["GITHUB_RECONCILIATION_PENDING"])
        self.assertEqual(metrics[0]["count"], 1)
        # The bounded view carries no subject, detail or source identity.
        self.assertNotIn("subject", metrics[0])
        self.assertEqual(product_state.read_daemon(self.env)["errors"], 0)

    def test_daemon_watch_uses_existing_tick_and_never_grants_write(self):
        watch = str(self.root / "feedback-watch.json")
        configured = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "example/model"},
            "daemon": {"feedback_watch_file": watch},
        })
        daemon = self.build(FakeDriver(), config=configured)
        daemon.start()
        reply = {"status": "RUNNING", "watch_id": "sample-one",
                 "observations": [{"decision": "INVESTIGATE"}]}
        with mock.patch("cao.feedback_loop.tick", return_value=reply) as ran:
            output = daemon.tick()
        ran.assert_called_once_with(watch)
        self.assertTrue(any(x.get("action") == "feedback_watch"
                            and x.get("status") == "RUNNING"
                            and x.get("cases") == 1
                            for x in output["effects"]))
        self.assertEqual(product_state.read_daemon(self.env)["errors"], 0)
        # The same daemon holds unknown rather than interpreting absence of
        # source as a healthy no-action observation.
        with mock.patch("cao.feedback_loop.tick",
                        side_effect=ValueError("source unavailable")):
            held = daemon.tick()
        self.assertTrue(any(x.get("action") == "feedback_watch_hold"
                            and x.get("status") == "UNKNOWN"
                            for x in held["effects"]))
        self.assertEqual(product_state.read_daemon(self.env)["errors"], 1)

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

    def test_team_owned_done_worker_is_never_redriven_by_plain_daemon(self) -> None:
        # Reproduce the actual ownership collision: a team-owned product task
        # still has a shared READ claim, its agent became DONE, and an ordinary
        # daemon tick must not complete/release it before Team Execution has
        # verified the post-prompt result receipt.
        driver = FakeDriver()
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "m"},
            "task": {"run_id": "cao-team-factory"},
        })
        spec = {
            "schema": "cao.team.v1", "team_id": "factory",
            "roles": {"reader": {"instruction": "Read only."}},
            "tasks": [{"id": "inspection", "role": "reader",
                       "repo": str(self.repo), "mode": "read",
                       "prompt": "Read tenant source"}],
        }
        team.initialize(spec, env=self.env)
        team.set_desired("factory", "running", env=self.env)
        ident = team._execution_id("factory", "inspection", "work", 0)
        record = team._required("factory", self.env)
        record["slots"]["inspection"].update(
            status="running", execution_id=ident, prompt="Read tenant source")
        team._save(record, self.env)
        created = product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Read tenant source",
            task_id=ident, mode="read", config=config, env=self.env,
            registry_db=self.db, wait_ms=0)
        self.assertEqual(created["status"], "running")
        self.assertEqual(driver.names().count("agent_prompt"), 1)
        # Simulate true completed agent instead of vanishing.
        pane_id = product_state.read_task(ident, self.env)["checkpoint"]["pane_id"]
        driver._panes[pane_id]["agent_status"] = "done"
        daemon = self.build(driver, config=config)
        daemon.start()
        with (
            mock.patch("cao.product_task.run_task",
                       side_effect=AssertionError("legacy re-drive of team task")) as legacy,
            mock.patch("cao.team.tick_all", return_value=[
                {"team_id": "factory", "status": "RUNNING"}]) as team_tick,
        ):
            report = daemon.tick()
        legacy.assert_not_called()
        team_tick.assert_called_once()
        # Teams observe liveness against the daemon's own clock, so the existing
        # supervision loop is the only cadence the bound ever depends on.
        self.assertIs(team_tick.call_args.kwargs["clock"], daemon._clock)
        self.assertTrue(report["ticked"])
        record = product_state.read_task(ident, self.env)
        self.assertEqual(record["status"], "running")
        self.assertFalse(record["claim"].get("released", False))
        self.assertEqual(driver.names().count("agent_prompt"), 1)
        self.assertNotIn("pane_close", driver.names())
        supervision = product_state.read_daemon(self.env)["supervision"]
        self.assertEqual(supervision[ident]["action"], "team_owned")
        self.assertEqual(supervision[ident]["status"], "running")
        self.assertTrue(any(x.get("action") == "team_reconcile"
                            for x in report["effects"]))
        self.assertEqual(report["wakes"], 0)
        c = registry.connect_readonly(self.db)
        try:
            rows = c.execute(
                "SELECT mode,released_at FROM claims WHERE work_key=?",
                (ident,)).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["mode"], "READ")
            self.assertIsNone(rows[0]["released_at"])
        finally:
            c.close()

    def test_unregistered_team_prefixed_product_task_is_held_unknown(self) -> None:
        # A manually configured run_id/name does not grant team ownership.
        driver = FakeDriver()
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "m"},
            "task": {"run_id": "cao-team-unregistered"},
        })
        ident = "ct-unregistered-declared-work-0-abcd"
        original = product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Orphaned-looking job",
            task_id=ident, config=config, env=self.env,
            registry_db=self.db, wait_ms=0)
        self.assertEqual(original["status"], "running")
        driver.drop_agents()
        daemon = self.build(driver, config=config)
        daemon.start()
        with mock.patch("cao.product_task.run_task",
                        side_effect=AssertionError("must not infer ownership")) as legacy:
            result = daemon.tick()
        legacy.assert_not_called()
        entry = product_state.read_daemon(self.env)["supervision"][ident]
        self.assertEqual(entry["status"], "HELD_UNKNOWN")
        self.assertEqual(entry["action"], "hold")
        self.assertTrue(any(x.get("action") == "team_ownership_hold"
                            for x in result["effects"]))
        self.assertEqual(product_state.read_task(ident, self.env)["status"], "running")

    def test_team_checkpoint_with_corrupted_run_id_blocks_generic_recovery(self) -> None:
        driver = FakeDriver()
        spec = {
            "schema": "cao.team.v1", "team_id": "factory",
            "roles": {"reader": {"instruction": "Read only."}},
            "tasks": [{"id": "inspection", "role": "reader",
                       "repo": str(self.repo), "mode": "read",
                       "prompt": "Read tenant source"}],
        }
        team.initialize(spec, env=self.env)
        ident = team._execution_id("factory", "inspection", "work", 0)
        record = team._required("factory", self.env)
        record["slots"]["inspection"].update(
            status="running", execution_id=ident, prompt="Read tenant source")
        team._save(record, self.env)
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "m"},
            "task": {"run_id": "cao-team-factory"},
        })
        product_task.run_task(driver=driver, repo=str(self.repo),
                              prompt="Read tenant source", task_id=ident,
                              config=config, env=self.env,
                              registry_db=self.db, wait_ms=0)
        existing = product_state.read_task(ident, self.env)
        existing["run_id"] = None
        product_state.write_task(existing, self.env)
        driver.drop_agents()
        daemon = self.build(driver, config=config)
        daemon.start()
        with mock.patch("cao.product_task.run_task",
                        side_effect=AssertionError("cannot bypass team ownership")) as legacy:
            daemon.tick()
        legacy.assert_not_called()
        supervision = product_state.read_daemon(self.env)["supervision"]
        self.assertEqual(supervision[ident]["status"], "HELD_UNKNOWN")

    def test_team_checkpoint_loss_keeps_unverified_task_out_of_legacy_loop(self) -> None:
        # A team task still has its original claim and task record after its
        # team checkpoint disappears. We must not silently recover it as an
        # ordinary task when its completion receipt is no longer verifiable.
        driver = FakeDriver()
        spec = {
            "schema": "cao.team.v1", "team_id": "missing-state",
            "roles": {"reader": {"instruction": "Read only."}},
            "tasks": [{"id": "inspection", "role": "reader",
                       "repo": str(self.repo), "mode": "read",
                       "prompt": "Read tenant source"}],
        }
        team.initialize(spec, env=self.env)
        ident = team._execution_id("missing-state", "inspection", "work", 0)
        record = team._required("missing-state", self.env)
        record["slots"]["inspection"].update(
            status="running", execution_id=ident,
            prompt="Read tenant source")
        team._save(record, self.env)
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "m"},
            "task": {"run_id": "cao-team-missing-state"},
        })
        product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Read tenant source",
            task_id=ident, config=config, env=self.env,
            registry_db=self.db, wait_ms=0)
        team_path = team.team_path("missing-state", self.env)
        Path(team_path).write_text("corrupt checkpoint", encoding="utf-8")
        driver.drop_agents()
        daemon = self.build(driver, config=config)
        daemon.start()
        with mock.patch(
                "cao.product_task.run_task",
                side_effect=AssertionError("must not recover absent team")) as legacy:
            daemon.tick()
        legacy.assert_not_called()
        entry = product_state.read_daemon(self.env)["supervision"][ident]
        self.assertEqual(entry["status"], "HELD_UNKNOWN")
        self.assertEqual(product_state.read_task(ident, self.env)["status"], "running")

    def test_team_loop_can_finish_on_same_daemon_without_legacy_redrive(self) -> None:
        # Prove that blocking legacy recovery does not also block the team
        # execution loop: the same daemon owns exactly one active team turn.
        import re

        class CompletedTeamDriver(FakeDriver):
            def agent_prompt(self, target, text, *, timeout_ms=None, wait=False):
                result = super().agent_prompt(
                    target, text, timeout_ms=timeout_ms, wait=wait)
                challenge = re.search(
                    r"CAO_TEAM_NONCE_V2: ([0-9a-f]{16})", text)
                assert challenge
                self.read_text = (
                    "Inspected the bounded source.\nCAO_TEAM_RESULT: "
                    + challenge.group(1))
                self._panes[self._agents[target]]["agent_status"] = "done"
                return result

            def pane_close(self, pane_id):
                self._note("pane_close", pane_id)
                self._panes.pop(pane_id, None)
                return "{}"

        driver = CompletedTeamDriver()
        spec = {
            "schema": "cao.team.v1", "team_id": "actual-run",
            "max_parallel": 1, "max_writers": 0,
            "roles": {"reader": {"instruction": "Read only."}},
            "tasks": [{"id": "inspection", "role": "reader",
                       "repo": str(self.repo), "mode": "read",
                       "prompt": "Read bounded source"}],
        }
        team.initialize(spec, env=self.env)
        team.set_desired("actual-run", "running", env=self.env)
        daemon = self.build(driver)
        daemon.start()
        first = daemon.tick()
        state = team.status("actual-run", env=self.env)
        self.assertEqual(state["status"], "COMPLETE", state)
        self.assertTrue(any(
            x.get("action") == "team_reconcile"
            and x.get("status") == "COMPLETE" for x in first["effects"]))
        ident = state["tasks"]["inspection"]["execution_id"]
        task = product_state.read_task(ident, self.env)
        self.assertEqual(task["status"], "complete")
        self.assertTrue(task["claim"]["released"])
        self.assertEqual(task["pane_closed"], task["checkpoint"]["pane_id"])
        self.assertEqual(driver.names().count("agent_prompt"), 1)
        # On the following daemon tick, terminal team task remains team-owned;
        # no legacy recovery or additional model turn is permitted.
        second = daemon.tick()
        self.assertTrue(second["ticked"])
        self.assertEqual(driver.names().count("agent_prompt"), 1)
        self.assertEqual(
            product_state.read_daemon(self.env)["supervision"][ident]["action"],
            "team_owned")

    def test_retry_read_preserves_current_and_historical_team_ownership(self) -> None:
        """A stopped failed READ remains historical; its retry is team-owned."""
        spec = {
            "schema": "cao.team.v1", "team_id": "retried-inspection",
            "max_parallel": 1, "max_writers": 0,
            "roles": {"reader": {"instruction": "Inspect."}},
            "tasks": [{
                "id": "source", "role": "reader", "repo": str(self.repo),
                "mode": "read", "prompt": "Read bounded source",
            }],
        }
        team.initialize(spec, env=self.env)
        old_id = team._execution_id("retried-inspection", "source", "work", 0)
        new_id = team._execution_id("retried-inspection", "source", "work", 0, 1)
        checkpoint = team._required("retried-inspection", self.env)
        slot = checkpoint["slots"]["source"]
        slot.update(status="running", execution_id=new_id,
                    retry_no=1, prompt="read retry")
        checkpoint["desired"] = "paused"
        team._save(checkpoint, self.env)
        product_state.write_task({
            "schema": product_state.TASK_SCHEMA, "task_id": old_id,
            "run_id": "cao-team-retried-inspection", "status": "stopped",
            "updated_at": "2026-01-01T00:00:00Z",
        }, self.env)
        product_state.write_task({
            "schema": product_state.TASK_SCHEMA, "task_id": new_id,
            "run_id": "cao-team-retried-inspection", "status": "running",
            "updated_at": "2026-01-01T00:00:01Z",
        }, self.env)
        driver = FakeDriver()
        daemon = self.build(driver)
        daemon.start()
        with mock.patch("cao.product_task.run_task",
                        side_effect=AssertionError("team task was re-driven")) as run:
            daemon.tick()
        run.assert_not_called()
        supervision = product_state.read_daemon(self.env)["supervision"]
        self.assertEqual(supervision[old_id]["action"], "team_historical")
        self.assertEqual(supervision[old_id]["status"], "stopped")
        self.assertEqual(supervision[new_id]["action"], "team_owned")
        self.assertEqual(supervision[new_id]["status"], "running")
        self.assertFalse(any(
            v.get("status") == "HELD_UNKNOWN" for v in
            (supervision[old_id], supervision[new_id])
        ))

    def test_prior_review_and_correction_stages_preserve_team_ownership(self) -> None:
        # A reviewer/correction team retains durable per-phase task records
        # after the current checkpoint has moved on. Old complete stages are
        # historical, not unregistered team tasks or daemon retry targets.
        driver = FakeDriver()
        spec = {
            "schema": "cao.team.v1", "team_id": "review-phases",
            "max_parallel": 1, "max_writers": 1,
            "roles": {
                "writer": {"instruction": "Scoped implementation."},
                "reviewer": {"instruction": "Independent review."},
            },
            "tasks": [{
                "id": "changes", "role": "writer", "repo": str(self.repo),
                "mode": "write", "write_scope": ["a.txt"],
                "prompt": "Make one bounded change", "reviewer": "reviewer",
                "max_corrections": 1,
            }],
        }
        team.initialize(spec, env=self.env)
        checkpoint = team._required("review-phases", self.env)
        head = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            text=True).strip()
        # The team has completed work0, review0 and correction1 and is
        # queued for the final review1; paused means no fresh worker launches.
        checkpoint["slots"]["changes"].update(
            phase="review", round=1, status="pending",
            head=head, execution_id=None, prompt=None)
        team._save(checkpoint, self.env)
        team._required("review-phases", self.env)  # validate exact checkpoint
        prior = [
            team._execution_id("review-phases", "changes", phase, round_no)
            for phase, round_no in (
                ("work", 0), ("review", 0), ("correction", 1))]
        for index, ident in enumerate(prior):
            product_state.write_task({
                "schema": product_state.TASK_SCHEMA,
                "task_id": ident, "run_id": "cao-team-review-phases",
                "status": "complete", "updated_at": f"2026-01-01T00:00:0{index}Z",
            }, self.env)
        daemon = self.build(driver)
        daemon.start()
        with mock.patch("cao.product_task.run_task",
                        side_effect=AssertionError("historical task re-driven")) as legacy:
            first = daemon.tick()
        legacy.assert_not_called()
        supervision = product_state.read_daemon(self.env)["supervision"]
        for ident in prior:
            self.assertEqual(supervision[ident]["action"], "team_historical")
            self.assertEqual(supervision[ident]["status"], "complete")
            self.assertTrue(supervision[ident]["terminal"])
        self.assertFalse(any(
            effect.get("action") == "team_ownership_hold"
            for effect in first["effects"]))

        # A historical stage that inexplicably remains running is held for
        # inspection instead of being revived or released by generic recovery.
        stale = product_state.read_task(prior[0], self.env)
        stale["status"] = "running"
        product_state.write_task(stale, self.env)
        with mock.patch("cao.product_task.run_task",
                        side_effect=AssertionError("stale team task re-driven")) as legacy:
            second = daemon.tick()
        legacy.assert_not_called()
        held = product_state.read_daemon(self.env)["supervision"][prior[0]]
        self.assertEqual(held["status"], "HELD_UNKNOWN")
        self.assertEqual(held["reason"], "non-current team stage")
        self.assertTrue(any(
            effect.get("task_id") == prior[0]
            and effect.get("action") == "team_ownership_hold"
            for effect in second["effects"]))

        # A future review stage that has not been launched cannot claim a
        # historical completion and would also be held if materialized.
        unlaunched = team._execution_id(
            "review-phases", "changes", "review", 1)
        product_state.write_task({
            "schema": product_state.TASK_SCHEMA,
            "task_id": unlaunched, "run_id": "cao-team-review-phases",
            "status": "complete", "updated_at": "2026-01-01T00:00:05Z",
        }, self.env)
        with mock.patch("cao.product_task.run_task",
                        side_effect=AssertionError("future team stage adopted")) as legacy:
            daemon.tick()
        legacy.assert_not_called()
        future = product_state.read_daemon(self.env)["supervision"][unlaunched]
        self.assertEqual(future["status"], "HELD_UNKNOWN")
        self.assertEqual(future["reason"], "team stage was not launched")

    def test_missing_team_and_corrupt_run_id_does_not_redrive_team_shaped_id(self):
        # Both independently identifying records have been lost/altered.
        # A deterministic CAO Team Execution ID is a residual safety hint,
        # not an ownership grant: hold instead of performing a generic re-drive.
        driver = FakeDriver()
        config = product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "m"},
            "task": {"run_id": "cao-team-vanished"},
        })
        ident = team._execution_id("vanished", "missing", "work", 0)
        created = product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Read source",
            task_id=ident, config=config, env=self.env,
            registry_db=self.db, wait_ms=0)
        self.assertEqual(created["status"], "running")
        task = product_state.read_task(ident, self.env)
        task["run_id"] = None
        product_state.write_task(task, self.env)
        driver.drop_agents()
        daemon = self.build(driver, config=config)
        daemon.start()
        with mock.patch("cao.product_task.run_task",
                        side_effect=AssertionError("dual-fault recovery")) as legacy:
            daemon.tick()
        legacy.assert_not_called()
        entry = product_state.read_daemon(self.env)["supervision"][ident]
        self.assertEqual(entry["status"], "HELD_UNKNOWN")
        self.assertEqual(entry["reason"], "team ownership is unverified")
        self.assertEqual(product_state.read_task(ident, self.env)["status"], "running")

    def test_unreadable_team_directory_preserves_ordinary_task_recovery(self):
        # A directory-level error must not abort all ordinary supervision.
        # CAO's team dispatch reports the error rather than silently succeeding.
        driver = FakeDriver()
        ordinary = self.run_read(driver, wait_ms=0)
        self.assertEqual(ordinary["status"], "running")
        driver.drop_agents()
        daemon = self.build(driver)
        daemon.start()
        with mock.patch("cao.team.list_teams",
                        side_effect=PermissionError("simulated teams unreadable")):
            update = daemon.tick()
        self.assertTrue(update["ticked"])
        self.assertTrue(any(
            item.get("action") == "team_hold"
            and item.get("status") == "UNKNOWN"
            for item in update["effects"]))
        result = product_state.read_task(ordinary["task_id"], self.env)
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["claim"]["released"])
        self.assertEqual(
            product_state.read_daemon(self.env)["supervision"]
            [ordinary["task_id"]]["action"], "redrive")

    def test_ordinary_product_task_still_uses_original_recovery_loop(self) -> None:
        # The new filter is narrow: a fake team-prefixed *task ID* alone is
        # not enough to exclude an ordinary run from its existing recovery.
        driver = FakeDriver()
        created = product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Ordinary task",
            task_id="ct-id-lookalike", config=self.config, env=self.env,
            registry_db=self.db, wait_ms=0)
        self.assertEqual(created["status"], "running")
        driver.drop_agents()
        daemon = self.build(driver)
        daemon.start()
        daemon.tick()
        record = product_state.read_task("ct-id-lookalike", self.env)
        self.assertEqual(record["status"], "complete")
        self.assertTrue(record["claim"]["released"])
        self.assertEqual(
            product_state.read_daemon(self.env)["supervision"]
            ["ct-id-lookalike"]["action"], "redrive")

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
        # Real occupancy permits the WRITE claim and the agent runs. The fake
        # agent performs no Git commit, so completion must remain blocked and
        # retain the writer claim until a clean scoped result is demonstrated.
        self.assertEqual(record["status"], "blocked")
        self.assertEqual(record["claim"]["mode"], "WRITE")
        self.assertFalse(record["claim"].get("released"))
        self.assertEqual(record["error"]["code"], "TASK_WRITE_RESULT_UNVERIFIED")
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
        self.assertEqual(payload["status"], "STARTING")
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


class ClosingDriver(FakeDriver):
    """The daemon fixture driver that honours a real pane close."""

    def pane_close(self, pane_id):
        self._note("pane_close", pane_id)
        self._panes.pop(pane_id, None)
        return "{}"


class TeamRecoveryDaemonTests(DaemonCase):
    """R3 automatic safe recovery observed through the real daemon cadence."""

    def team_config(self, team_id="factory"):
        return product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "m"},
            "task": {"agent_kind": "opencode", "run_id": "cao-team-" + team_id},
        })

    def team_read_setup(self, *, team_id="factory", task_id="inspect",
                        pinned_head=Ellipsis, driver=None):
        driver = driver or ClosingDriver()
        config = self.team_config(team_id)
        spec = {
            "schema": "cao.team.v1", "team_id": team_id,
            "roles": {"reader": {"instruction": "Read the checkout only."}},
            "tasks": [{"id": task_id, "role": "reader", "repo": str(self.repo),
                       "mode": "read", "prompt": "Inspect the checkout"}],
        }
        team.initialize(spec, env=self.env)
        team.set_desired(team_id, "running", env=self.env)
        ident = team._execution_id(team_id, task_id, "work", 0)
        # A durable READ execution with a real open claim and a live agent.
        product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Inspect the checkout",
            task_id=ident, mode="read", config=config, env=self.env,
            registry_db=self.db, wait_ms=0)
        head = (team._git_head(str(self.repo)) if pinned_head is Ellipsis
                else pinned_head)
        record = team._required(team_id, self.env)
        slot = record["slots"][task_id]
        slot.update(status="unknown", error="TEAM_RESULT_UNREADABLE",
                    execution_id=ident, prompt="Inspect the checkout",
                    pinned_head=head, pinned_branch=team._git_branch(str(self.repo)),
                    started_at="2026-01-01T00:00:00Z",
                    progress_at="2026-01-01T00:00:00Z")
        team._save(record, self.env)
        return driver, config, ident

    def start_daemon(self, driver, config):
        record = product_state.default_daemon_record()
        record["desired"] = "running"
        product_state.write_daemon(record, self.env)
        return self.build(driver, config=config)

    def released_claim_count(self):
        conn = registry.connect_readonly(self.db)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM claims").fetchone()[0]
        finally:
            conn.close()

    def test_daemon_auto_resumes_original_read_and_completes(self):
        driver, config, ident = self.team_read_setup()
        driver.read_text = "analysis\nCAO_TEAM_RESULT: " + team._result_tag(ident)
        report = self.start_daemon(driver, config).tick()
        recovery = [e for e in report["effects"]
                    if e.get("action") == "team_recovery"]
        self.assertTrue(recovery)
        self.assertTrue(any(e["auto_resume"] for e in recovery))
        self.assertEqual(
            team.status("factory", env=self.env)["tasks"]["inspect"]["status"],
            "complete")
        # The SAME original worker answered; it was never re-prompted.
        self.assertEqual(driver.names().count("agent_prompt"), 1)
        self.assertEqual(self.released_claim_count(), 1)
        record = team._required("factory", self.env)
        self.assertEqual(record["recovery"]["resumed"][ident], 1)

    def test_daemon_restart_preserves_the_recovery_decision(self):
        driver, config, ident = self.team_read_setup(pinned_head="0" * 40)
        daemon = self.start_daemon(driver, config)
        daemon.tick()
        first = team._required("factory", self.env)["recovery"]["decisions"]["inspect"]
        self.assertEqual(first["classification"], team_recovery.GIT_DRIFT)
        self.assertEqual(first["decision"], team_recovery.HOLD)
        # A fresh daemon instance over the same durable checkpoint must not
        # rewrite or re-apply the stored decision.
        restarted = self.build(driver, config=config)
        restarted.tick()
        second = team._required("factory", self.env)["recovery"]["decisions"]["inspect"]
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["at"], second["at"])
        self.assertNotIn(ident, team._required("factory", self.env)["recovery"]["resumed"])
        self.assertEqual(driver.names().count("agent_prompt"), 1)

    def test_daemon_lost_terminal_receipt_holds_with_no_new_claim(self):
        driver, config, ident = self.team_read_setup()
        # The runtime returns DONE without a valid post-prompt receipt.
        driver.read_text = "Model credits exhausted."
        daemon = self.start_daemon(driver, config)
        first = daemon.tick()
        self.assertTrue(any(e.get("action") == "team_recovery" and e["auto_resume"]
                            for e in first["effects"]))
        self.assertEqual(
            team.status("factory", env=self.env)["tasks"]["inspect"]["status"],
            "unknown")
        self.assertEqual(team._required("factory", self.env)
                         ["recovery"]["resumed"][ident], 1)
        # The next tick reclassifies the persisted UNKNOWN as a lost terminal
        # receipt and holds: no second resume, no new claim, no re-prompt.
        self.build(driver, config=config).tick()
        decision = team._required("factory", self.env)["recovery"]["decisions"]["inspect"]
        self.assertEqual(decision["classification"],
                         team_recovery.TERMINAL_RECEIPT_MISSING)
        self.assertEqual(decision["decision"], team_recovery.HOLD)
        self.assertFalse(decision["auto_resume"])
        self.assertEqual(team._required("factory", self.env)
                         ["recovery"]["resumed"][ident], 1)
        self.assertEqual(self.released_claim_count(), 1)
        self.assertEqual(driver.names().count("agent_prompt"), 1)

    def test_daemon_reports_a_held_team_exception_without_applying_it(self):
        driver, config, ident = self.team_read_setup(pinned_head="0" * 40)
        report = self.start_daemon(driver, config).tick()
        metrics = [item for item in report["effects"]
                   if item.get("action") == "engineering_metrics"]
        self.assertTrue(metrics)
        self.assertIn("HELD_EVIDENCE", metrics[0]["codes"])
        # The report changed nothing: the original worker was never re-prompted
        # and no bounded resume was applied by the metric path.
        self.assertEqual(driver.names().count("agent_prompt"), 1)
        self.assertNotIn(ident, team._required(
            "factory", self.env)["recovery"]["resumed"])

    def test_daemon_recovery_makes_no_external_egress(self):
        driver, config, ident = self.team_read_setup()
        import socket
        with mock.patch.object(socket, "socket",
                               side_effect=AssertionError("egress")) as net, \
                mock.patch.object(socket, "create_connection",
                                  side_effect=AssertionError("egress")) as dial:
            self.start_daemon(driver, config).tick()
        net.assert_not_called()
        dial.assert_not_called()


if __name__ == "__main__":
    unittest.main()
