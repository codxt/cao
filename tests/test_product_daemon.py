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
    intake,
    intake_handoff,
    intake_triage,
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
        self._panes.pop(pane_id, None)
        return "{}"

    def agent_start(self, name, kind, pane_id, *, argv=(), timeout_ms=None):
        self._note("agent_start", name, kind, pane_id)
        self._agents[name] = pane_id
        pane = self._panes.get(pane_id)
        if pane is not None:
            pane["agent"] = kind  # the worker KIND, never the unique name
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
        pane_id = self._agents.get(target)
        if pane_id is None:
            raise product_driver.DriverNotFound("not present")
        pane = self._panes.get(pane_id) or {}
        return {
            "name": target,
            "agent": pane.get("agent"),
            "pane_id": pane_id,
            "workspace_id": pane.get("workspace_id"),
            "agent_status": pane.get("agent_status"),
        }

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
              terminate=None, identity=None, authority_reader=None):
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
            authority_reader=authority_reader,
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

    # -- synthetic review READ retry probe ---------------------------------
    def _stage_synthetic_review_retry(self) -> tuple[str, str, str]:
        """Stage a round-0 READ review retry with a later round left future.

        Returns the original attempt id, the bounded retry id and the
        unlaunched id of the next review round (max_corrections >= 1).
        """
        spec = {
            "schema": "cao.team.v1", "team_id": "review-retry-audit",
            "max_parallel": 1, "max_writers": 1,
            "roles": {
                "writer": {"instruction": "Scoped implementation."},
                "reviewer": {"instruction": "Independent READ-only review."},
            },
            "tasks": [{
                "id": "audit-report", "role": "writer", "repo": str(self.repo),
                "mode": "write", "write_scope": ["a.txt"],
                "prompt": "Make one bounded change", "reviewer": "reviewer",
                "max_corrections": 1,
            }],
        }
        team.initialize(spec, env=self.env)
        head = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            text=True).strip()
        checkpoint = team._required("review-retry-audit", self.env)
        retry = team._execution_id(
            "review-retry-audit", "audit-report", "review", 0, 1)
        checkpoint["slots"]["audit-report"].update(
            phase="review", round=0, status="complete",
            execution_id=retry, retry_no=1, head=head,
            prompt="bounded review retry")
        checkpoint["desired"] = "paused"
        team._save(checkpoint, self.env)
        team._required("review-retry-audit", self.env)  # validate exact slot
        return (
            team._execution_id("review-retry-audit", "audit-report", "review", 0),
            retry,
            team._execution_id("review-retry-audit", "audit-report", "review", 1),
        )

    def test_review_read_retry_classifies_exact_identities(self) -> None:
        # Exact deterministic identities for the original review attempt and
        # its one admitted READ retry, on a fully synthetic team.
        original, retry, future = self._stage_synthetic_review_retry()
        self.assertEqual(
            original,
            "ct-review-retry-audit-audit-report-review-0-3b815876927340fe29ce")
        self.assertEqual(
            retry,
            "ct-review-retry-audit-audit-report-review-0-e33ca1182194ee725cbe")
        owners = product_daemon._team_execution_owners(self.env)
        # Previous attempt of the current stage is historical, the retry is
        # current, and a later review round stays unlaunched (fail-closed)
        # even though it shares the review phase with the retried slot.
        self.assertEqual(owners[original], ("review-retry-audit", "historical"))
        self.assertEqual(owners[retry], ("review-retry-audit", "current"))
        self.assertEqual(owners[future], ("review-retry-audit", "unlaunched"))
        # The not-yet-launched correction of this round stays unlaunched too.
        self.assertEqual(
            owners[team._execution_id(
                "review-retry-audit", "audit-report", "correction", 1)],
            ("review-retry-audit", "unlaunched"))
        # Earlier work identities remain historical, including the READ work
        # identity and its admitted retry.
        for attempt in (0, 1):
            self.assertEqual(
                owners[team._execution_id(
                    "review-retry-audit", "audit-report", "work", 0, attempt)],
                ("review-retry-audit", "historical"))

    def test_review_read_retry_holds_the_future_round_fail_closed(self) -> None:
        original, retry, future = self._stage_synthetic_review_retry()
        # All three durable records claim a completed stage; only the first
        # two were ever launched by this checkpoint.
        for index, ident in enumerate((original, retry, future)):
            product_state.write_task({
                "schema": product_state.TASK_SCHEMA, "task_id": ident,
                "run_id": "cao-team-review-retry-audit", "status": "complete",
                "updated_at": f"2026-01-01T00:00:0{index}Z",
            }, self.env)
        daemon = self.build(FakeDriver())
        daemon.start()
        with mock.patch("cao.product_task.run_task",
                        side_effect=AssertionError("team task was re-driven")):
            tick = daemon.tick()
        supervision = product_state.read_daemon(self.env)["supervision"]
        # Historical attempt and current retry resolve to their own lanes.
        self.assertEqual(supervision[original]["action"], "team_historical")
        self.assertTrue(supervision[original]["terminal"])
        self.assertEqual(supervision[retry]["action"], "team_owned")
        self.assertTrue(supervision[retry]["terminal"])
        # The future review round is never adopted as a historical completion.
        future_entry = supervision[future]
        self.assertEqual(future_entry["status"], "HELD_UNKNOWN")
        self.assertEqual(future_entry["reason"], "team stage was not launched")
        self.assertTrue(any(
            effect.get("task_id") == future
            and effect.get("action") == "team_ownership_hold"
            for effect in tick["effects"]))
        self.assertFalse(any(
            effect.get("task_id") in (original, retry)
            for effect in tick["effects"]))

    def test_review_read_retry_orphan_identities_are_refused(self) -> None:
        # Orphan refusal: with no readable team checkpoint, neither the
        # original attempt nor its retry may be owned by generic recovery.
        original = team._execution_id(
            "review-retry-audit", "audit-report", "review", 0)
        retry = team._execution_id(
            "review-retry-audit", "audit-report", "review", 0, 1)
        owners = product_daemon._team_execution_owners(self.env)
        self.assertNotIn(original, owners)
        self.assertNotIn(retry, owners)
        for ident in (original, retry):
            for run_id in ("cao-team-review-retry-audit",
                           product_config.DEFAULT_RUN_ID):
                self.assertEqual(
                    product_daemon._task_owner_lane(
                        {"task_id": ident, "run_id": run_id}, owners),
                    "unknown")

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


class HygieneTests(DaemonCase):
    """Conservative, bounded, default-off historical pane hygiene.

    The production lifecycle closes a task's exact owned pane and records
    ``pane_closed`` BEFORE it releases the claim (``product_task._finish`` and
    ``stop_task``), so a real released terminal record has no live owned pane.
    A terminal pane observed present is therefore an anomaly that is always
    held, never closed. Every fixture below that pairs a released terminal
    record with a live pane is an INVALID adversarial state the real native
    accepted lifecycle cannot produce; it exists only to prove CAO never closes
    it and never reports it as a positive canary.
    """

    def _ledger(self):
        # Hygiene fails closed on an absent Registry ledger, so a readable
        # ledger (here empty) is required before a terminal pane is examined.
        registry.connect(self.db).close()

    def forged_terminal_read(self, driver, task_id="forged-read", *,
                             kind="opencode", agent_status="done", label=None,
                             agent_name=None, register_agent=True, mode="READ",
                             scope=(), ledger=True, outcome="complete"):
        """An INVALID adversarial fixture: a released terminal record + live pane.

        The real native accepted lifecycle cannot reach this state: it closes
        the exact owned pane and records the verified close before releasing the
        claim. This fixture asserts the opposite on purpose, so every test that
        uses it proves CAO holds such a record instead of treating it as a
        production-reachable positive. A real pane reports the worker KIND in
        ``agent`` (not the unique name), and ``agent.get`` on the task's own
        unique name resolves that pane and workspace. ``register_agent=False``
        models a pane whose occupant this task cannot prove is its own (a
        foreign or already-vanished worker); ``ledger=False`` models an absent
        Registry ledger.
        """
        if ledger:
            self._ledger()
        created = driver.workspace_create("cao-review-" + task_id, cwd=str(self.repo))
        pane_id = created["pane_id"]
        if label is not None:
            driver._workspaces[created["workspace_id"]]["label"] = label
        if agent_name is None:
            agent_name = "cao-" + task_id
        if register_agent:
            driver._agents[agent_name] = pane_id
        driver._panes[pane_id]["agent"] = kind
        driver._panes[pane_id]["agent_status"] = agent_status
        stamp = "2026-01-01T00:00:00Z"
        product_state.write_task({
            "schema": product_state.TASK_SCHEMA,
            "task_id": task_id,
            "run_id": product_config.DEFAULT_RUN_ID,
            "work_key": task_id,
            "repo": str(self.repo),
            "prompt": "Read the repository",
            "mode": mode, "scope": list(scope),
            "agent_kind": "opencode", "agent_args": [],
            "workspace_label": "cao-review-" + task_id,
            "status": "complete",
            "checkpoint": {
                "workspace_id": created["workspace_id"],
                "tab_id": created["tab_id"], "pane_id": pane_id,
                "agent_name": agent_name, "agent_started": True,
                "start_attempted": True, "prompt_issued": True,
                "prompt_ack": "accepted", "stage": "prompted",
            },
            "created_at": stamp, "updated_at": stamp,
            "claim": {"claim_id": 1, "status": "RELEASED", "released": True,
                      "outcome": outcome},
            "output": None, "output_truncated": False, "output_bounds": None,
            "output_observations": [], "observation": None, "error": None,
            "refusal": None, "pane_closed": None,
        }, self.env)
        return pane_id

    def hygiene_config(self, *, enabled, dry_run):
        return product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "m"},
            "daemon": {"hygiene_enabled": enabled, "hygiene_dry_run": dry_run},
        })

    def test_default_off_only_plans_and_never_closes(self):
        driver = FakeDriver()
        pane_id = self.forged_terminal_read(driver)
        daemon = self.build(driver)
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(hygiene["mode"], "plan")
        # The forged released-record + live-pane state is never a positive.
        self.assertEqual(hygiene["counts"]["eligible"], 0)
        self.assertEqual(hygiene["counts"]["closed"], 0)
        self.assertGreaterEqual(hygiene["counts"]["protected_unknown"], 1)
        self.assertIn(pane_id, driver._panes)
        # Visible on the existing status surface, without raw process output.
        surfaced = product_daemon.status(env=self.env)["hygiene"]
        self.assertEqual(surfaced["mode"], "plan")
        self.assertEqual(surfaced["counts"], hygiene["counts"])
        # Process exit is never asserted without a stable recorded identity.
        self.assertEqual(surfaced["counts"]["process_leak"], 0)
        self.assertNotIn("process", surfaced)

    def test_enabled_dry_run_still_never_closes(self):
        driver = FakeDriver()
        pane_id = self.forged_terminal_read(driver)
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=True))
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(hygiene["mode"], "plan")
        self.assertEqual(hygiene["counts"]["closed"], 0)
        self.assertEqual(hygiene["counts"]["eligible"], 0)
        self.assertGreaterEqual(hygiene["counts"]["protected_unknown"], 1)
        self.assertIn(pane_id, driver._panes)

    def test_enabled_apply_holds_the_forged_record_and_is_idempotent(self):
        # The previous team treated this impossible state as a positive that
        # qualified automatic cleanup. It is not production-reachable, so even
        # with both effects flags set CAO holds it and closes nothing.
        driver = FakeDriver()
        pane_id = self.forged_terminal_read(driver)
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        self.assertEqual(driver.names().count("pane_close"), 0)
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(hygiene["mode"], "applied")
        self.assertEqual(hygiene["counts"]["closed"], 0)
        self.assertEqual(hygiene["counts"]["eligible"], 0)
        self.assertGreaterEqual(hygiene["counts"]["protected_unknown"], 1)
        self.assertIn(pane_id, driver._panes)
        record = product_state.read_task("forged-read", self.env)
        self.assertIsNone(record["pane_closed"])
        # Idempotent: a second applied pass makes the identical held decision
        # and closes nothing further.
        daemon.tick()
        self.assertEqual(driver.names().count("pane_close"), 0)
        second = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(second["counts"], hygiene["counts"])
        self.assertEqual(second["counts"]["closed"], 0)

    def test_forged_released_terminal_record_is_never_a_positive_canary(self):
        # Explicitly label the adversarial fixture: status complete AND a
        # released claim AND a live pane AND no recorded close is a state the
        # real accepted lifecycle cannot produce. It can never qualify as an
        # eligible orphan or be closed.
        driver = FakeDriver()
        pane_id = self.forged_terminal_read(driver, task_id="forged-canary")
        record = product_state.read_task("forged-canary", self.env)
        self.assertEqual(record["status"], "complete")
        self.assertTrue(record["claim"]["released"])
        self.assertIsNone(record["pane_closed"])
        self.assertIn(pane_id, driver._panes)
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(hygiene["counts"]["eligible"], 0)
        self.assertEqual(hygiene["counts"]["closed"], 0)
        self.assertGreaterEqual(hygiene["counts"]["protected_unknown"], 1)
        self.assertIn(pane_id, driver._panes)

    def test_verified_close_with_a_reappeared_pane_holds(self):
        # A record whose own durable release AND verified close both match, yet
        # whose pane is observed PRESENT, is an unexpected reappearance: held,
        # never eligible and never closed.
        driver = FakeDriver()
        pane_id = self.forged_terminal_read(driver, task_id="reappeared")
        record = product_state.read_task("reappeared", self.env)
        record["pane_closed"] = pane_id
        product_state.write_task(record, self.env)
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(hygiene["counts"]["eligible"], 0)
        self.assertEqual(hygiene["counts"]["closed"], 0)
        self.assertGreaterEqual(hygiene["counts"]["protected_unknown"], 1)
        self.assertIn(pane_id, driver._panes)

    def test_released_claim_without_a_durable_outcome_holds(self):
        # A record that merely asserts released=True without a durable outcome
        # is not the accepted lifecycle's disposition, so even a matching
        # recorded close does not make the pane closeable: it is held.
        driver = FakeDriver()
        pane_id = self.forged_terminal_read(driver, task_id="no-outcome",
                                            outcome=None)
        record = product_state.read_task("no-outcome", self.env)
        record["pane_closed"] = pane_id
        product_state.write_task(record, self.env)
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(hygiene["counts"]["eligible"], 0)
        self.assertEqual(hygiene["counts"]["closed"], 0)
        self.assertGreaterEqual(hygiene["counts"]["protected_unknown"], 1)
        self.assertIn(pane_id, driver._panes)

    def test_ordinary_completed_task_leaves_no_eligible_or_held_orphan(self):
        # A reachable, task-owned successful close + release leaves no live
        # pane, so a genuinely finished task is never an eligible orphan and
        # never inflates the held counts. The only close is its own.
        driver = FakeDriver()
        driver.wait_state = "done"
        created = product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Review this repository",
            config=self.config, env=self.env, registry_db=self.db, wait_ms=1000)
        self.assertEqual(created["status"], "complete")
        self.assertTrue(created["claim"]["released"])
        pane_id = created["pane_id"]
        self.assertEqual(created["pane_closed"], pane_id)
        self.assertNotIn(pane_id, driver._panes)
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(hygiene["counts"]["eligible"], 0)
        self.assertEqual(hygiene["counts"]["closed"], 0)
        self.assertEqual(hygiene["counts"]["protected_unknown"], 0)
        self.assertEqual(hygiene["counts"]["protected_active"], 0)
        self.assertEqual(driver.names().count("pane_close"), 1)

    def test_dirty_checkout_holds_a_terminal_pane(self):
        driver = FakeDriver()
        pane_id = self.forged_terminal_read(driver, task_id="dirty-hold")
        (self.repo / "untracked.txt").write_text("wip\n", encoding="utf-8")
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(hygiene["counts"]["eligible"], 0)
        self.assertGreaterEqual(hygiene["counts"]["protected_unknown"], 1)
        self.assertIn(pane_id, driver._panes)

    def test_pane_reused_by_another_task_is_protected(self):
        driver = FakeDriver()
        pane_id = self.forged_terminal_read(driver, task_id="reuse-hold")
        # A second, non-terminal task shares the exact pane/workspace identity,
        # so the terminal pane is reused and never a hygiene candidate.
        task = product_state.read_task("reuse-hold", self.env)
        active = dict(task)
        active.update(task_id="reuse-active", work_key="reuse-active",
                      status="blocked")
        active["checkpoint"] = dict(task["checkpoint"])
        product_state.write_task(active, self.env)
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertGreaterEqual(hygiene["counts"]["protected_active"], 1)
        self.assertEqual(hygiene["counts"]["eligible"], 0)
        self.assertIn(pane_id, driver._panes)

    def test_active_worker_is_protected_even_with_apply(self):
        driver = FakeDriver()
        self.forged_terminal_read(driver, agent_status="working")
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertGreaterEqual(hygiene["counts"]["protected_active"], 1)
        self.assertEqual(hygiene["counts"]["eligible"], 0)

    def test_foreign_worker_is_never_closed(self):
        # The pane is occupied and done, but ``agent.get`` cannot resolve the
        # task's own unique name to this pane, so it is never closed.
        driver = FakeDriver()
        self.forged_terminal_read(
            driver, agent_name="caoown", register_agent=False)
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertGreaterEqual(hygiene["counts"]["protected_active"], 1)

    def test_live_claim_holds_even_a_terminal_record(self):
        driver = FakeDriver()
        created = self.run_read(driver, wait_ms=0)
        driver.drop_agents()
        record = product_state.read_task(created["task_id"], self.env)
        record["status"] = "complete"
        product_state.write_task(record, self.env)
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertGreaterEqual(hygiene["counts"]["protected_active"], 1)

    def test_unreadable_runtime_never_closes(self):
        driver = FakeDriver()
        self.forged_terminal_read(driver)
        driver.readable = False
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(hygiene["runtime"], "UNAVAILABLE")
        self.assertEqual(hygiene["counts"]["eligible"], 0)

    def test_foreign_workspace_label_is_unowned(self):
        driver = FakeDriver()
        self.forged_terminal_read(driver, label="someone-elses-workspace")
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertGreaterEqual(hygiene["counts"]["unowned"], 1)

    def test_unaccepted_team_result_holds(self):
        # A terminal task record owned by a team that has NOT accepted the
        # result is never cleaned up: an unknown team result holds.
        driver = FakeDriver()
        task_id = team._execution_id("squad", "a", "work", 0)
        self.forged_terminal_read(driver, task_id=task_id)
        spec = {
            "schema": team.SPEC_SCHEMA, "team_id": "squad",
            "roles": {"analyst": {"instruction": "Read only."}},
            "tasks": [{"id": "a", "role": "analyst", "repo": str(self.repo),
                       "mode": "read", "prompt": "Analyze source"}],
        }
        team.initialize(spec, env=self.env)
        record = team._required("squad", self.env)
        record["slots"]["a"].update(status="unknown", execution_id=task_id,
                                    prompt="Analyze source")
        team._save(record, self.env)
        task = product_state.read_task(task_id, self.env)
        task["run_id"] = "cao-team-squad"
        product_state.write_task(task, self.env)
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertGreaterEqual(hygiene["counts"]["protected_unknown"], 1)
        self.assertEqual(hygiene["counts"]["eligible"], 0)

    def test_done_receipt_alone_cannot_authorize_cleanup(self):
        # A terminal record whose output happens to contain a receipt is still
        # not authority: an active worker or a live claim protects the pane.
        driver = FakeDriver()
        self.forged_terminal_read(driver, agent_status="working")
        record = product_state.read_task("forged-read", self.env)
        record["output"] = "done\nCAO_TEAM_RESULT: " + ("a" * 16)
        record["claim"] = {"claim_id": 7, "status": "CLAIMED", "released": False}
        product_state.write_task(record, self.env)
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(hygiene["counts"]["eligible"], 0)

    def test_absent_registry_ledger_fails_closed(self):
        # A missing Registry ledger is not "zero claims": it is unavailable, so
        # every terminal pane holds rather than being treated as unclaimed.
        driver = FakeDriver()
        self.forged_terminal_read(driver, ledger=False)
        self.assertFalse(os.path.exists(self.db))
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(hygiene["registry"], "UNAVAILABLE")
        self.assertEqual(hygiene["counts"]["eligible"], 0)

    def test_active_conflicting_claim_on_the_same_checkout_holds(self):
        # A live claim on the SAME checkout -- even under a different work_key
        # and run -- protects the terminal pane there.
        driver = FakeDriver()
        self.forged_terminal_read(driver)
        product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Edit the code",
            task_id="other-writer", mode="write", write_scope=("a.txt",),
            observation=RuntimeObservation.empty(), config=self.config,
            env=self.env, registry_db=self.db, wait_ms=0)
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertGreaterEqual(hygiene["counts"]["protected_active"], 1)
        self.assertEqual(hygiene["counts"]["eligible"], 0)

    def test_historical_team_result_is_not_deemed_accepted(self):
        # A previous team attempt's task record says complete, but the team has
        # advanced to a later stage. The task's own terminal status is not
        # durable team acceptance, so the pane is held.
        driver = FakeDriver()
        historical = team._execution_id("squad", "a", "work", 0)
        current = team._execution_id("squad", "a", "review", 0)
        self.forged_terminal_read(
            driver, task_id=historical, mode="WRITE", scope=["a.txt"])
        spec = {
            "schema": team.SPEC_SCHEMA, "team_id": "squad",
            "roles": {"dev": {"instruction": "Implement precise edits."},
                      "reviewer": {"instruction": "Review the commits."}},
            "tasks": [{"id": "a", "role": "dev", "reviewer": "reviewer",
                       "repo": str(self.repo), "mode": "write",
                       "write_scope": ["a.txt"], "prompt": "Implement it.",
                       "max_corrections": 1}],
        }
        team.initialize(spec, env=self.env)
        record = team._required("squad", self.env)
        record["slots"]["a"].update(
            status="complete", phase="review", round=0, execution_id=current,
            prompt="Review it.")
        team._save(record, self.env)
        task = product_state.read_task(historical, self.env)
        task["run_id"] = "cao-team-squad"
        product_state.write_task(task, self.env)
        daemon = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        daemon.start()
        daemon.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertGreaterEqual(hygiene["counts"]["protected_unknown"], 1)
        self.assertEqual(hygiene["counts"]["eligible"], 0)

    # ------------------------------------------------------------------
    # Historical leftover: a REALISTIC replay of the pre-close-first build.
    #
    # The fixture below is NOT the adversarial ``forged_terminal_read`` above.
    # It replays what the real old build actually persisted: the governed
    # Registry really proposes, claims and RELEASES the task's work through its
    # own API, and the task record carries the legacy shape -- a released claim
    # disposition with NO recorded pane close -- because that build released its
    # claim without ever closing the pane (see the git history of
    # ``product_task._finish`` before commit 6d2f271). That state is the
    # positive canary for the corroborated-leftover gate; the modern close-first
    # lifecycle cannot produce it.
    # ------------------------------------------------------------------

    def legacy_released_read(self, driver, task_id="legacy-read", *,
                             run_id=None, mode="READ", scope=("a.txt",),
                             release_outcome="complete",
                             record_outcome="complete",
                             register_agent=True, agent_status="done",
                             label=None, claim=True, release=True):
        run_id = run_id or product_config.DEFAULT_RUN_ID
        created = driver.workspace_create("cao-review-" + task_id,
                                          cwd=str(self.repo))
        pane_id = created["pane_id"]
        if label is not None:
            driver._workspaces[created["workspace_id"]]["label"] = label
        agent_name = "cao-" + task_id
        if register_agent:
            driver._agents[agent_name] = pane_id
        driver._panes[pane_id]["agent"] = "opencode"
        driver._panes[pane_id]["agent_status"] = agent_status
        claim_id = None
        conn = registry.connect(self.db)
        try:
            registry.propose(conn, run_id, task_id, str(self.repo),
                             paths=list(scope))
            if claim:
                result = registry.claim(conn, registry.ClaimSpec(
                    run_id=run_id, work_key=task_id, owner=product_task.OWNER,
                    mode=mode, issue="",
                    worktree=str(self.repo) if mode == registry.WRITE else "",
                ))
                claim_id = result.get("claim_id")
                if release:
                    registry.release(conn, run_id, product_task.OWNER, task_id,
                                     issue="", outcome=release_outcome)
        finally:
            conn.close()
        stamp = "2026-01-01T00:00:00Z"
        product_state.write_task({
            "schema": product_state.TASK_SCHEMA,
            "task_id": task_id,
            "run_id": run_id,
            "work_key": task_id,
            "repo": str(self.repo),
            "prompt": "Review this repository",
            "mode": mode, "scope": list(scope),
            "agent_kind": "opencode", "agent_args": [],
            "workspace_label": "cao-review-" + task_id,
            "status": "complete",
            "checkpoint": {
                "workspace_id": created["workspace_id"],
                "tab_id": created["tab_id"], "pane_id": pane_id,
                "agent_name": agent_name, "agent_started": True,
                "start_attempted": True, "prompt_issued": True,
                "prompt_ack": "accepted", "stage": "prompted",
            },
            "created_at": stamp, "updated_at": stamp,
            "claim": {"claim_id": claim_id, "status": "RELEASED",
                      "released": True, "outcome": record_outcome},
            "output": None, "output_truncated": False, "output_bounds": None,
            "output_observations": [], "observation": None, "error": None,
            "refusal": None, "pane_closed": None,
        }, self.env)
        return pane_id

    def test_corroborated_historical_leftover_plans_then_closes_once(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(driver, task_id="legacy-close")
        # Default OFF + dry run: the corroborated leftover is reported eligible
        # and NOTHING is closed. Activation is never a source change.
        plan = self.build(driver)
        plan.start()
        plan.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(hygiene["mode"], "plan")
        self.assertEqual(hygiene["counts"]["eligible"], 1)
        self.assertEqual(hygiene["counts"]["closed"], 0)
        self.assertIn(pane_id, driver._panes)
        # Enabled AND dry-run cleared: the exact owned pane is closed once and
        # its verified close is recorded on the shared task record.
        applied = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        applied.start()
        applied.tick()
        self.assertEqual(driver.names().count("pane_close"), 1)
        self.assertNotIn(pane_id, driver._panes)
        record = product_state.read_task("legacy-close", self.env)
        self.assertEqual(record["pane_closed"], pane_id)
        # Exactly one final classification for the applied pane: a successful
        # close is ``closed``, never both ``eligible`` and ``closed``.
        applied_hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(applied_hygiene["counts"]["closed"], 1)
        self.assertEqual(applied_hygiene["counts"]["eligible"], 0)
        self.assertEqual(sum(applied_hygiene["counts"].values()), 1)
        # Idempotent across a restart: the pane is gone, so nothing more closes.
        restarted = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        restarted.tick()
        self.assertEqual(driver.names().count("pane_close"), 1)
        restarted_hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(restarted_hygiene["counts"]["closed"], 0)
        self.assertEqual(restarted_hygiene["counts"]["eligible"], 0)
        self.assertEqual(sum(restarted_hygiene["counts"].values()), 0)

    def test_historical_leftover_without_a_released_row_holds(self):
        # A released boolean the ledger does not corroborate is never authority.
        driver = FakeDriver()
        pane_id = self.legacy_released_read(driver, task_id="no-row",
                                            claim=False)
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertGreaterEqual(report["counts"]["protected_unknown"], 1)
        self.assertIn(pane_id, driver._panes)

    def test_historical_leftover_row_mismatch_holds(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(driver, task_id="row-mismatch")
        record = product_state.read_task("row-mismatch", self.env)
        record["work_key"] = "a-different-work-key"
        product_state.write_task(record, self.env)
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertIn(pane_id, driver._panes)

    def test_historical_leftover_outcome_mismatch_holds(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(
            driver, task_id="outcome-mismatch",
            release_outcome="abandon", record_outcome="complete")
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertIn(pane_id, driver._panes)

    def test_reappeared_pane_with_a_real_released_row_still_holds(self):
        # PR #31 protection, preserved: a recorded verified close plus a present
        # pane is held even when the ledger corroborates the released row.
        driver = FakeDriver()
        pane_id = self.legacy_released_read(driver, task_id="legacy-reappear")
        record = product_state.read_task("legacy-reappear", self.env)
        record["pane_closed"] = pane_id
        product_state.write_task(record, self.env)
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertEqual(report["counts"]["closed"], 0)
        self.assertGreaterEqual(report["counts"]["protected_unknown"], 1)
        self.assertIn(pane_id, driver._panes)

    def test_active_worker_historical_leftover_holds(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(
            driver, task_id="legacy-active", agent_status="working")
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertGreaterEqual(report["counts"]["protected_active"], 1)
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertIn(pane_id, driver._panes)

    def test_foreign_agent_historical_leftover_holds(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(
            driver, task_id="legacy-foreign", register_agent=False)
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertGreaterEqual(report["counts"]["protected_active"], 1)
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertIn(pane_id, driver._panes)

    def test_foreign_workspace_label_historical_leftover_is_unowned(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(
            driver, task_id="legacy-label", label="someone-elses-workspace")
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertGreaterEqual(report["counts"]["unowned"], 1)
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertIn(pane_id, driver._panes)

    def test_dirty_checkout_historical_leftover_holds(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(driver, task_id="legacy-dirty")
        (self.repo / "untracked.txt").write_text("wip\n", encoding="utf-8")
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertGreaterEqual(report["counts"]["protected_unknown"], 1)
        self.assertIn(pane_id, driver._panes)

    def test_reused_pane_historical_leftover_holds(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(driver, task_id="legacy-reuse")
        task = product_state.read_task("legacy-reuse", self.env)
        active = dict(task)
        active.update(task_id="legacy-reuse-active",
                      work_key="legacy-reuse-active", status="blocked")
        active["checkpoint"] = dict(task["checkpoint"])
        product_state.write_task(active, self.env)
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertGreaterEqual(report["counts"]["protected_active"], 1)
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertIn(pane_id, driver._panes)

    def test_live_claim_on_the_checkout_historical_leftover_holds(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(driver, task_id="legacy-conflict")
        product_task.run_task(
            driver=driver, repo=str(self.repo), prompt="Edit the code",
            task_id="another-writer", mode="write", write_scope=("a.txt",),
            observation=RuntimeObservation.empty(), config=self.config,
            env=self.env, registry_db=self.db, wait_ms=0)
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertGreaterEqual(report["counts"]["protected_active"], 1)
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertIn(pane_id, driver._panes)

    def test_open_claim_from_another_owner_historical_leftover_holds(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(driver, task_id="legacy-multiowner")
        conn = registry.connect(self.db)
        try:
            registry.propose(conn, "another-run", "other-work", str(self.repo),
                             paths=["a.txt"])
            registry.claim(conn, registry.ClaimSpec(
                run_id="another-run", work_key="other-work",
                owner="another-owner", mode=registry.WRITE, issue="",
                branch="main", worktree=str(self.repo)),
                RuntimeObservation.empty())
        finally:
            conn.close()
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertGreaterEqual(report["counts"]["protected_active"], 1)
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertIn(pane_id, driver._panes)

    def test_unaccepted_team_historical_leftover_holds(self):
        driver = FakeDriver()
        task_id = team._execution_id("squad", "a", "work", 0)
        pane_id = self.legacy_released_read(
            driver, task_id=task_id, run_id="cao-team-squad")
        spec = {
            "schema": team.SPEC_SCHEMA, "team_id": "squad",
            "roles": {"analyst": {"instruction": "Read only."}},
            "tasks": [{"id": "a", "role": "analyst", "repo": str(self.repo),
                       "mode": "read", "prompt": "Analyze source"}],
        }
        team.initialize(spec, env=self.env)
        record = team._required("squad", self.env)
        record["slots"]["a"].update(status="unknown", execution_id=task_id,
                                    prompt="Analyze source")
        team._save(record, self.env)
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertGreaterEqual(report["counts"]["protected_unknown"], 1)
        self.assertIn(pane_id, driver._panes)

    def team_read_leftover(self, driver, *, task_id, slot_status="complete"):
        """A durable team-owned historical READ leftover, in the governed shape.

        The team is initialized through its own public API, then its current work
        slot is written with the exact execution identity, the checkout's real
        source revision and a substituted durable status. The result is a
        well-formed checkpoint, never a forged partial one: with
        ``slot_status="complete"`` the owning team has genuinely accepted the
        result (``team._required`` validates it), and any other status is a
        well-formed but unaccepted slot. Returns the leftover pane id.
        """

        pane_id = self.legacy_released_read(
            driver, task_id=task_id, run_id="cao-team-squad")
        spec = {
            "schema": team.SPEC_SCHEMA, "team_id": "squad",
            "roles": {"analyst": {"instruction": "Read only."}},
            "tasks": [{"id": "a", "role": "analyst", "repo": str(self.repo),
                       "mode": "read", "prompt": "Analyze source"}],
        }
        team.initialize(spec, env=self.env)
        record = team._required("squad", self.env)
        record["slots"]["a"].update(
            status=slot_status, phase="work", round=0, execution_id=task_id,
            prompt="Analyze source", head=team._git_head(str(self.repo)))
        team._save(record, self.env)
        return pane_id

    def test_accepted_team_historical_leftover_plans_then_closes_once(self):
        # The same governed leftover as the unaccepted test above, but the
        # owning team slot is a genuinely ACCEPTED, durably ``complete`` result
        # carrying the checkout's real source revision. A team-owned historical
        # READ task is eligible only on that durable team acceptance: it plans
        # first, then closes exactly once and records the verified close, and
        # stays idempotent across a daemon restart.
        driver = FakeDriver()
        task_id = team._execution_id("squad", "a", "work", 0)
        pane_id = self.team_read_leftover(driver, task_id=task_id)
        # Default OFF + dry run: the accepted team leftover is reported eligible
        # and NOTHING is closed.
        plan = self.build(driver)
        plan.start()
        plan.tick()
        self.assertNotIn("pane_close", driver.names())
        hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(hygiene["mode"], "plan")
        self.assertEqual(hygiene["counts"]["eligible"], 1)
        self.assertEqual(hygiene["counts"]["closed"], 0)
        self.assertEqual(hygiene["counts"]["protected_unknown"], 0)
        self.assertIn(pane_id, driver._panes)
        # Enabled AND dry-run cleared: the exact owned pane is closed once and
        # its verified close is recorded on the shared task record.
        applied = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        applied.start()
        applied.tick()
        self.assertEqual(driver.names().count("pane_close"), 1)
        self.assertNotIn(pane_id, driver._panes)
        record = product_state.read_task(task_id, self.env)
        self.assertEqual(record["pane_closed"], pane_id)
        applied_hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(applied_hygiene["counts"]["closed"], 1)
        self.assertEqual(applied_hygiene["counts"]["eligible"], 0)
        self.assertEqual(sum(applied_hygiene["counts"].values()), 1)
        # Idempotent across a restart: the pane is gone, so nothing more closes.
        restarted = self.build(
            driver, config=self.hygiene_config(enabled=True, dry_run=False))
        restarted.tick()
        self.assertEqual(driver.names().count("pane_close"), 1)
        restarted_hygiene = product_state.read_daemon(self.env)["hygiene"]
        self.assertEqual(restarted_hygiene["counts"]["closed"], 0)
        self.assertEqual(restarted_hygiene["counts"]["eligible"], 0)
        self.assertEqual(sum(restarted_hygiene["counts"].values()), 0)

    def test_well_formed_but_unaccepted_team_leftover_still_holds(self):
        # The ONLY difference from the accepted test above is the slot's durable
        # status: a well-formed but not-yet-complete team slot is not acceptance.
        # The identical leftover holds, so the gate is the team's own durable
        # acceptance -- not merely a parseable (non-CORRUPT) checkpoint.
        driver = FakeDriver()
        task_id = team._execution_id("squad", "a", "work", 0)
        pane_id = self.team_read_leftover(
            driver, task_id=task_id, slot_status="running")
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertGreaterEqual(report["counts"]["protected_unknown"], 1)
        self.assertIn(pane_id, driver._panes)

    def test_unreadable_ledger_historical_leftover_holds(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(driver, task_id="legacy-noledger")
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver,
            registry_db=str(self.root / "missing.sqlite3"), apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertEqual(report["registry"], "UNAVAILABLE")
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertIn(pane_id, driver._panes)

    def test_absent_pane_historical_leftover_is_skipped(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(driver, task_id="legacy-gone")
        driver.pane_close(pane_id)  # the leftover resolved itself: no orphan
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertEqual(report["counts"]["closed"], 0)
        self.assertEqual(report["counts"]["protected_unknown"], 0)

    def test_absent_pane_with_unaccepted_team_is_not_protected_unknown(self):
        # The pane is genuinely absent. An unaccepted historical team is an
        # unrelated predicate: it must not turn a non-existent leftover into a
        # held count.
        driver = FakeDriver()
        task_id = team._execution_id("squad", "a", "work", 0)
        pane_id = self.legacy_released_read(
            driver, task_id=task_id, run_id="cao-team-squad")
        spec = {
            "schema": team.SPEC_SCHEMA, "team_id": "squad",
            "roles": {"analyst": {"instruction": "Read only."}},
            "tasks": [{"id": "a", "role": "analyst", "repo": str(self.repo),
                       "mode": "read", "prompt": "Analyze source"}],
        }
        team.initialize(spec, env=self.env)
        record = team._required("squad", self.env)
        record["slots"]["a"].update(status="unknown", execution_id=task_id,
                                    prompt="Analyze source")
        team._save(record, self.env)
        driver.pane_close(pane_id)  # the test removes the pane itself
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        # Only the test setup closed a pane; hygiene closed nothing.
        self.assertEqual(driver.names().count("pane_close"), 1)
        self.assertEqual(report["counts"]["protected_unknown"], 0)
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertEqual(report["counts"]["closed"], 0)

    def test_absent_pane_with_dirty_checkout_is_not_protected_unknown(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(driver, task_id="legacy-gone-dirty")
        driver.pane_close(pane_id)  # the test removes the pane itself
        (self.repo / "untracked.txt").write_text("wip\n", encoding="utf-8")
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        # Only the test setup closed a pane; hygiene closed nothing.
        self.assertEqual(driver.names().count("pane_close"), 1)
        self.assertEqual(report["counts"]["protected_unknown"], 0)
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertEqual(report["counts"]["closed"], 0)

    def test_absent_pane_with_unknown_team_lane_is_not_protected_unknown(self):
        driver = FakeDriver()
        task_id = team._execution_id("ghost", "a", "work", 0)
        pane_id = self.legacy_released_read(
            driver, task_id=task_id, run_id="cao-team-ghost")
        driver.pane_close(pane_id)  # the test removes the pane itself
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        # Only the test setup closed a pane; hygiene closed nothing.
        self.assertEqual(driver.names().count("pane_close"), 1)
        self.assertEqual(report["counts"]["protected_unknown"], 0)
        self.assertEqual(report["counts"]["eligible"], 0)

    def test_absent_pane_without_released_row_is_not_protected_unknown(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(
            driver, task_id="legacy-gone-norow", claim=False)
        driver.pane_close(pane_id)  # the test removes the pane itself
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        # Only the test setup closed a pane; hygiene closed nothing.
        self.assertEqual(driver.names().count("pane_close"), 1)
        self.assertEqual(report["counts"]["protected_unknown"], 0)
        self.assertEqual(report["counts"]["eligible"], 0)

    def test_absent_pane_with_unreadable_runtime_still_holds(self):
        # An unreadable runtime proves nothing about pane absence: the task is
        # held UNKNOWN rather than silently skipped as if the pane were gone.
        driver = FakeDriver()
        self.legacy_released_read(driver, task_id="legacy-dark")
        driver.readable = False
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertEqual(report["runtime"], "UNAVAILABLE")
        self.assertEqual(report["counts"]["protected_unknown"], 1)
        self.assertEqual(report["counts"]["eligible"], 0)

    def test_registry_change_between_observation_and_close_holds(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(driver, task_id="legacy-race")
        observed = product_daemon._claim_rows(self.db)
        self.assertIsNotNone(observed)
        conflicting = {
            "claim_id": 999, "run_id": "another-run", "work_key": "another-work",
            "issue": "", "owner": "someone-else", "mode": "WRITE",
            "worktree": str(self.repo), "claimed_at": "2026-01-01T00:00:00Z",
            "released_at": None, "outcome": None,
        }
        calls = {"n": 0}

        def racing_rows(db):
            # The observation read sees the released row; the revalidation read
            # immediately before the close sees a new conflicting claim.
            calls["n"] += 1
            return observed if calls["n"] == 1 else observed + [conflicting]

        with mock.patch.object(product_daemon, "_claim_rows",
                               side_effect=racing_rows):
            report = product_daemon.hygiene_report(
                env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertEqual(report["counts"]["closed"], 0)
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertEqual(report["counts"]["protected_unknown"], 1)
        self.assertEqual(sum(report["counts"].values()), 1)
        self.assertIn(pane_id, driver._panes)

    def test_multi_owner_claim_racing_the_close_holds(self):
        # A second owner's OPEN claim appears only at the revalidation read,
        # after the first observation saw the exact released row. The full
        # Registry predicate is re-proved immediately before the effect.
        driver = FakeDriver()
        pane_id = self.legacy_released_read(driver, task_id="legacy-race-owner")
        observed = product_daemon._claim_rows(self.db)
        self.assertIsNotNone(observed)
        foreign = {
            "claim_id": 777, "run_id": "other-run", "work_key": "other-work",
            "issue": "", "owner": "another-owner", "mode": "WRITE",
            "worktree": str(self.repo), "claimed_at": "2026-01-01T00:00:00Z",
            "released_at": None, "outcome": None,
        }
        calls = {"n": 0}

        def racing_rows(db):
            calls["n"] += 1
            return observed if calls["n"] == 1 else observed + [foreign]

        with mock.patch.object(product_daemon, "_claim_rows",
                               side_effect=racing_rows):
            report = product_daemon.hygiene_report(
                env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertEqual(report["counts"]["closed"], 0)
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertEqual(report["counts"]["protected_unknown"], 1)
        self.assertEqual(sum(report["counts"].values()), 1)
        self.assertIn(pane_id, driver._panes)

    def test_worker_became_active_at_final_close_holds(self):
        # The first pane check proves the own worker stopped. Immediately before
        # the close it is active again, so the closer must not knowingly close
        # an active worker.
        driver = FakeDriver()
        pane_id = self.legacy_released_read(
            driver, task_id="legacy-active-race")
        original = product_daemon._owned_pane_check
        calls = {"n": 0}

        def racing(driver_arg, task, labels):
            calls["n"] += 1
            if calls["n"] > 1:
                driver_arg._panes[pane_id]["agent_status"] = "working"
            return original(driver_arg, task, labels)

        with mock.patch.object(product_daemon, "_owned_pane_check",
                               side_effect=racing):
            report = product_daemon.hygiene_report(
                env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertEqual(report["counts"]["protected_active"], 1)
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertEqual(report["counts"]["closed"], 0)
        self.assertEqual(sum(report["counts"].values()), 1)
        self.assertIn(pane_id, driver._panes)

    def test_worker_became_foreign_at_final_close_holds(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(
            driver, task_id="legacy-foreign-race")
        original = product_daemon._owned_pane_check
        calls = {"n": 0}

        def racing(driver_arg, task, labels):
            calls["n"] += 1
            if calls["n"] > 1:
                driver_arg._agents.pop("cao-legacy-foreign-race", None)
            return original(driver_arg, task, labels)

        with mock.patch.object(product_daemon, "_owned_pane_check",
                               side_effect=racing):
            report = product_daemon.hygiene_report(
                env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertEqual(report["counts"]["protected_active"], 1)
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertEqual(report["counts"]["closed"], 0)
        self.assertEqual(sum(report["counts"].values()), 1)
        self.assertIn(pane_id, driver._panes)

    def test_task_becoming_non_terminal_before_close_holds(self):
        # The task record is mutable too: a record that becomes non-terminal
        # between observation and effect is never closed.
        driver = FakeDriver()
        pane_id = self.legacy_released_read(
            driver, task_id="legacy-task-race")
        original = product_daemon._revalidate_historical_close

        def racing(**kwargs):
            record = product_state.read_task("legacy-task-race", self.env)
            record["status"] = "running"
            product_state.write_task(record, self.env)
            return original(**kwargs)

        with mock.patch.object(product_daemon, "_revalidate_historical_close",
                               side_effect=racing):
            report = product_daemon.hygiene_report(
                env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn("pane_close", driver.names())
        self.assertEqual(report["counts"]["protected_active"], 1)
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertEqual(report["counts"]["closed"], 0)
        self.assertEqual(sum(report["counts"].values()), 1)
        self.assertIn(pane_id, driver._panes)

    def test_verified_close_that_cannot_be_recorded_is_not_reclaimed(self):
        # Irreducible crash window: the pane close is verified but persisting
        # ``pane_closed`` fails. The tick reports close_failed (never closed),
        # and a later pass sees the pane absent and claims nothing.
        driver = FakeDriver()
        pane_id = self.legacy_released_read(
            driver, task_id="legacy-unrecorded")
        real_update = product_state.update_task

        def failing_update(task_id, patch, env=None):
            if patch.get("pane_closed"):
                raise OSError("state write unavailable")
            return real_update(task_id, patch, env)

        with mock.patch.object(product_state, "update_task",
                               side_effect=failing_update):
            report = product_daemon.hygiene_report(
                env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertNotIn(pane_id, driver._panes)
        self.assertEqual(report["counts"]["close_failed"], 1)
        self.assertEqual(report["counts"]["closed"], 0)
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertEqual(sum(report["counts"].values()), 1)
        self.assertIsNone(
            product_state.read_task("legacy-unrecorded", self.env)["pane_closed"])
        second = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertEqual(sum(second["counts"].values()), 0)
        self.assertEqual(driver.names().count("pane_close"), 1)

    def test_partial_close_failure_is_reported_and_pane_remains(self):
        driver = FakeDriver()
        pane_id = self.legacy_released_read(driver, task_id="legacy-fail")

        def boom(pane_id):
            driver._note("pane_close", pane_id)
            raise product_driver.DriverTransportError("close unavailable")

        driver.pane_close = boom
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        self.assertIn(pane_id, driver._panes)
        self.assertEqual(report["counts"]["closed"], 0)
        self.assertEqual(report["counts"]["eligible"], 0)
        self.assertEqual(report["counts"]["close_failed"], 1)
        self.assertEqual(sum(report["counts"].values()), 1)
        self.assertIsNone(
            product_state.read_task("legacy-fail", self.env)["pane_closed"])

    def test_per_tick_close_bound_limits_a_large_cohort(self):
        driver = FakeDriver()
        total = product_daemon._MAX_HYGIENE_CLOSES + 1
        for index in range(total):
            self.legacy_released_read(driver, task_id="legacy-bound-%02d" % index)
        report = product_daemon.hygiene_report(
            env=self.env, driver=driver, registry_db=self.db, apply=True)
        # One final classification per terminal pane: the applied cohort is
        # ``closed`` only, while the cap-skipped remainder stays ``eligible``.
        self.assertEqual(report["counts"]["closed"],
                         product_daemon._MAX_HYGIENE_CLOSES)
        self.assertEqual(report["counts"]["eligible"],
                         total - product_daemon._MAX_HYGIENE_CLOSES)
        self.assertEqual(sum(report["counts"].values()), total)
        self.assertEqual(driver.names().count("pane_close"),
                         product_daemon._MAX_HYGIENE_CLOSES)


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


class HandoffReconciliationTests(DaemonCase):
    """A B6 handoff READ team is reconciled only by the existing daemon."""

    def _dispatch(self) -> tuple[str, str]:
        document = {
            "schema": intake.REQUEST_SCHEMA,
            "item_id": "daemon-handoff",
            "body": ("Read-only audit of the scoped source. Revalidate the "
                     "default branch and report evidence."),
            "title": "synthetic",
            "source": "operator",
        }
        sid = intake.submit(document, env=self.env)["submission"]["submission_id"]
        intake_triage.triage(sid, env=self.env)
        preflight = intake_handoff.handoff_plan(
            sid, env=self.env, repo=str(self.repo), dry_run=True
        )
        plan = preflight["plan"]
        grant = {
            "plan_digest": intake_handoff._binding_digest(plan),
            "read_only": True,
            "owner": "operator-one",
            "scope": {"repo": str(self.repo), "paths": ["a.txt"]},
            "role": "reader",
            "model": "opencode-go/deepseek-v4.1-flash#high",
            "privacy_qualified": True,
            "granted_at": "2026-10-15T00:00:00Z",
        }
        authority = mock.Mock(return_value={
            "authenticated": True, "operator": "operator-one", "grant": grant,
        })
        dispatched = intake_handoff.handoff_plan(
            sid, env=self.env, repo=str(self.repo),
            operator_authority=authority,
        )
        self.assertEqual(dispatched["status"], "ADMITTED")
        return sid, dispatched["plan"]["binding"]["team_id"]

    def test_handoff_team_is_reconciled_by_the_existing_daemon(self) -> None:
        _sid, team_id = self._dispatch()
        driver = FakeDriver()
        daemon = self.build(driver)
        daemon.start()
        with (
            mock.patch("cao.product_task.run_task",
                       side_effect=AssertionError("handoff work must not be "
                                                  "regenerated as a product task")) as legacy,
            mock.patch("cao.team.tick_all",
                       return_value=[{"team_id": team_id,
                                      "status": "PENDING"}]) as team_tick,
        ):
            report = daemon.tick()
        legacy.assert_not_called()
        team_tick.assert_called_once()
        self.assertTrue(report["ticked"])
        self.assertTrue(any(item.get("action") == "team_reconcile"
                            for item in report["effects"]))

    def test_a_held_handoff_creates_no_team_or_claim(self) -> None:
        document = {
            "schema": intake.REQUEST_SCHEMA,
            "item_id": "daemon-hold",
            "body": "Read-only audit then one squash merge of the branch.",
            "title": "synthetic",
            "source": "operator",
        }
        sid = intake.submit(document, env=self.env)["submission"]["submission_id"]
        intake_triage.triage(sid, env=self.env)
        result = intake_handoff.handoff_plan(sid, env=self.env, repo=str(self.repo))
        self.assertEqual(result["status"], "HOLD")
        self.assertFalse(result["plan"]["dispatch"])
        self.assertEqual(team.list_teams(env=self.env), [])


class StandingAuthorityDaemonTests(DaemonCase):
    """The daemon runs the B8 pipeline and supplies the standing authority."""

    def _config(self, **daemon):
        return product_config.validate_config({
            "schema": product_config.SCHEMA,
            "provider": {"cli": "opencode", "model": "m"},
            "daemon": daemon,
        })

    def _authority(self, verdict="SUPPORTED", adapter=None):
        record = {"verdict": verdict, "code": None, "reason": "synthetic",
                  "envelope": ({"scope": {"repo": str(self.repo), "paths": []},
                                "source": {"ref": None}} if verdict == "SUPPORTED"
                               else None),
                  "adapter": adapter}
        return lambda env, clock: record

    def test_pipeline_is_disabled_by_default(self) -> None:
        self.assertFalse(product_config.daemon_defaults(self._config()).intake_pipeline)
        daemon = self.build(FakeDriver(), config=self._config())
        daemon.start()
        with mock.patch("cao.intake_pipeline.tick") as pipeline, \
                mock.patch("cao.intake_queue.tick") as queue:
            report = daemon.tick()
        pipeline.assert_not_called()
        queue.assert_not_called()
        self.assertFalse(any(
            item.get("action", "").startswith("intake_")
            for item in report["effects"]))

    def test_enabled_pipeline_without_authority_holds_and_passes_none(self) -> None:
        daemon = self.build(
            FakeDriver(), config=self._config(intake_pipeline=True),
            authority_reader=self._authority("UNAVAILABLE"))
        daemon.start()
        report_view = {"schema": "x", "status": "IDLE", "items": [],
                       "advanced": 0, "effects": [], "exceptions": []}
        with mock.patch("cao.intake_queue.tick",
                        return_value=report_view) as queue:
            report = daemon.tick()
        queue.assert_called_once()
        self.assertNotIn("operator_authority", queue.call_args.kwargs)
        authority = [e for e in report["effects"]
                     if e.get("action") == "intake_authority"]
        self.assertEqual(authority[0]["status"], "UNAVAILABLE")
        pipeline = [e for e in report["effects"]
                    if e.get("action") == "intake_pipeline"]
        self.assertEqual(pipeline[0]["status"], "HOLD")

    def test_enabled_pipeline_with_authority_passes_the_adapter(self) -> None:
        adapter = object()
        daemon = self.build(
            FakeDriver(), config=self._config(intake_pipeline=True),
            authority_reader=self._authority("SUPPORTED", adapter))
        daemon.start()
        report_view = {"schema": "x", "status": "IDLE", "items": [],
                       "advanced": 0, "effects": [], "exceptions": []}
        with mock.patch("cao.intake_queue.tick",
                        return_value=report_view) as queue:
            daemon.tick()
        queue.assert_called_once()
        self.assertIs(queue.call_args.kwargs.get("operator_authority"), adapter)

    def test_pipeline_flag_must_be_boolean(self) -> None:
        with self.assertRaises(product_config.ProductConfigError):
            product_config.daemon_defaults(self._config(intake_pipeline="yes"))

    def test_b7_queue_only_configuration_remains_undisturbed(self) -> None:
        # A B7-only daemon (queue enabled, pipeline flag absent) still runs
        # exactly one queue pass and still supplies no operator authority.
        daemon = self.build(FakeDriver(), config=self._config(intake_queue=True))
        daemon.start()
        report_view = {"schema": "x", "status": "IDLE", "items": [],
                       "advanced": 0, "effects": [], "exceptions": []}
        with mock.patch("cao.intake_queue.tick",
                        return_value=report_view) as queue, \
                mock.patch("cao.intake_pipeline.tick") as pipeline:
            report = daemon.tick()
        queue.assert_called_once()
        self.assertNotIn("operator_authority", queue.call_args.kwargs)
        pipeline.assert_not_called()

    def test_enabled_pipeline_a_refused_authority_supplies_no_adapter(self) -> None:
        daemon = self.build(
            FakeDriver(), config=self._config(intake_pipeline=True),
            authority_reader=self._authority("REFUSED"))
        daemon.start()
        report_view = {"schema": "x", "status": "IDLE", "items": [],
                       "advanced": 0, "effects": [], "exceptions": []}
        with mock.patch("cao.intake_queue.tick",
                        return_value=report_view) as queue:
            report = daemon.tick()
        queue.assert_called_once()
        self.assertIsNone(queue.call_args.kwargs.get("operator_authority"))
        pipeline = [e for e in report["effects"]
                    if e.get("action") == "intake_pipeline"]
        self.assertEqual(pipeline[0]["status"], "HOLD")


if __name__ == "__main__":
    unittest.main()
